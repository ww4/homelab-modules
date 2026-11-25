"""HTTP to Facebook's logged-out Marketplace endpoints.

Ported from secondhand-mcp (MIT, jlsookiki/secondhand-mcp, src/marketplaces/
facebook/transport.ts). Same retry budget, same split between "try again"
(timeouts, 5xx, 429) and "Facebook refused" (other 4xx, data-less GraphQL
errors), plus a minimum interval between requests so a long run stays polite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

MAX_ATTEMPTS = 3
PAGE_ATTEMPTS = 2
ATTEMPT_TIMEOUT_S = 8.0
TOTAL_BUDGET_S = 15.0
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
# Facebook rate-limits with HTTP 200 + a data-less GraphQL error. On the wall,
# retry the one request after growing waits (a block has cleared within ~7 min,
# 2026-11-25), and slow every later request down. The waits come out of one
# budget per job (Pace), so a wall can't stall each watch in turn.
RATE_LIMIT_WAITS_S = (60.0, 120.0, 240.0, 480.0, 960.0)
RATE_LIMIT_SLOWDOWN = 2.0
RATE_LIMIT_MAX_INTERVAL_S = 12.0
# Never a metronome: each gap is the interval times a random factor, and now
# and then there's a longer pause, the way a person stops to read a listing.
# (Exact spacing is a classic bot signature; whether Facebook checks for it is
# assumed, untested. The mean factor, ~1.15, makes the real pace a bit slower.)
JITTER = (0.7, 1.6)
PAUSE_EVERY = (15, 40)  # requests between longer pauses
PAUSE_S = (8.0, 30.0)


@dataclass
class Pace:
    """Request pacing shared by every watch in one job.

    Each watch used to start a fresh Transport at full speed, so the watch after
    one that hit the wall ran straight back into it. Between jobs the interval
    is learned (market_agent.pacing); the counters feed that and the log.
    """

    interval: float = 1.5
    backoff_left_s: float = sum(RATE_LIMIT_WAITS_S)
    job: str = ""  # shared by every request of one job, in the request log
    start_interval: float = 0.0
    walls: int = 0  # distinct walls hit (concurrent refusals of one wall count once)
    waited_s: float = 0.0  # backoff actually slept
    gave_up: bool = False  # backoff budget ran out; reads left for the next run
    seq: int = 0  # requests sent this job

    def __post_init__(self) -> None:
        if not self.start_interval:
            self.start_interval = self.interval


# on_request(record): one dict per request; see Store.log_request for the fields.
RequestHook = Callable[[dict[str, Any]], None]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

GRAPHQL_HEADERS = {
    "content-type": "application/x-www-form-urlencoded",
    "sec-fetch-site": "same-origin",
    "user-agent": _UA,
}

# The search page only renders results for browser-shaped requests.
SEARCH_PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": _UA,
}


class RefusalError(Exception):
    """Facebook answered with an error instead of data. Retrying won't help."""


class RateLimited(Exception):
    """Facebook said "Rate limit exceeded" and kept saying it after backing off.

    Not a RefusalError: the listing is fine, we asked too fast. Callers should
    stop reading for this run and try again next run, never treat the missing
    data as an answer.
    """


class _Retryable(Exception):
    pass


class _RateLimitReply(Exception):
    pass


def _is_rate_limit(message: str | None) -> bool:
    return "rate limit" in (message or "").lower()


class Transport:
    def __init__(self, proxy: str | None = None, min_interval: float = 1.5, *,
                 pace: Pace | None = None, on_request: RequestHook | None = None) -> None:
        self._http = httpx.AsyncClient(proxy=proxy, follow_redirects=True)
        self.pace = pace or Pace(interval=min_interval)
        self._on_request = on_request
        self._lock = asyncio.Lock()
        self._wall_lock = asyncio.Lock()
        self._walls = 0  # walls waited out so far; concurrent requests share one wait
        self._last = 0.0
        self._rng = random.Random()
        self._until_pause = self._rng.randint(*PAUSE_EVERY)

    def _record(self, kind: str, outcome: str, *, seq: int, gap: float | None, started: float,
                status: int | None = None, detail: str = "") -> None:
        if not self._on_request:
            return
        try:
            self._on_request({
                "job": self.pace.job, "seq": seq, "kind": kind, "outcome": outcome,
                "interval": round(self.pace.interval, 3), "gap": None if gap is None else round(gap, 3),
                "latency_ms": int((time.monotonic() - started) * 1000), "status": status,
                "detail": detail[:300],
            })
        except Exception as e:  # noqa: BLE001 - logging must never break a read
            log.warning("couldn't log a Facebook request: %s", e)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pace(self) -> tuple[int, float | None]:
        """Wait our turn. Returns (sequence number, actual seconds since the previous request)."""
        async with self._lock:
            target = self.pace.interval * self._rng.uniform(*JITTER)
            self._until_pause -= 1
            if self._until_pause <= 0:
                target += self._rng.uniform(*PAUSE_S)
                self._until_pause = self._rng.randint(*PAUSE_EVERY)
            wait = self._last + target - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            now = time.monotonic()
            gap = now - self._last if self._last else None
            self._last = now
            self.pace.seq += 1
            return self.pace.seq, gap

    async def graphql(self, doc_id: str, variables: dict[str, Any], kind: str = "") -> dict[str, Any]:
        waits = iter(RATE_LIMIT_WAITS_S)
        while True:
            seen = self._walls
            try:
                return await self._graphql_once(doc_id, variables, kind)
            except _RateLimitReply:
                pass
            async with self._wall_lock:
                if self._walls != seen:
                    continue  # another request already waited this wall out; retry now
                p = self.pace
                p.walls += 1
                p.interval = min(p.interval * RATE_LIMIT_SLOWDOWN, RATE_LIMIT_MAX_INTERVAL_S)
                wait = next(waits, None)
                if wait is None or wait > p.backoff_left_s:
                    p.gave_up = True
                    break
                p.backoff_left_s -= wait
                p.waited_s += wait
                log.warning("Facebook rate limit; waiting %.0fs, then one request per %.1fs "
                            "(%.0f min of backoff left this job)", wait, p.interval, p.backoff_left_s / 60)
                await asyncio.sleep(wait)
                self._walls += 1
        raise RateLimited("Facebook rate limit exceeded (still limited after backing off)")

    async def request_once(self, doc_id: str, variables: dict[str, Any], kind: str = "") -> bool:
        """One paced request, no rate-limit retry: True if answered, False if rate-limited.

        For the probe, which needs to see every refusal. Other refusals raise.
        """
        try:
            await self._graphql_once(doc_id, variables, kind)
            return True
        except _RateLimitReply:
            return False

    async def _graphql_once(self, doc_id: str, variables: dict[str, Any], kind: str = "") -> dict[str, Any]:
        kind = kind or doc_id
        body = {"variables": json.dumps(variables, separators=(",", ":")), "doc_id": doc_id}
        deadline = time.monotonic() + TOTAL_BUDGET_S
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            seq, gap = await self._pace()
            started = time.monotonic()
            meta: dict[str, Any] = {}  # per request: two run concurrently for each listing
            rec = {"seq": seq, "gap": gap, "started": started}
            try:
                payload = await asyncio.wait_for(
                    self._attempt(body, min(ATTEMPT_TIMEOUT_S, remaining), meta), timeout=remaining
                )
                self._record(kind, "ok", status=meta.get("status"), **rec)
                return payload
            except _RateLimitReply as e:
                self._record(kind, "rate_limit", status=meta.get("status"), detail=str(e), **rec)
                raise
            except RefusalError as e:
                self._record(kind, "refusal", status=meta.get("status"), detail=str(e), **rec)
                raise
            except (httpx.HTTPError, asyncio.TimeoutError, _Retryable, ValueError) as e:
                self._record(kind, "error", status=meta.get("status"), detail=f"{type(e).__name__}: {e}", **rec)
                last = e
                backoff = 2 ** (attempt - 1) * (0.5 + random.random())
                if attempt == MAX_ATTEMPTS or time.monotonic() + backoff >= deadline:
                    break
                await asyncio.sleep(backoff)
        raise last or RuntimeError("Facebook request failed")

    async def _attempt(self, body: dict[str, str], timeout: float, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        r = await self._http.post(GRAPHQL_URL, data=body, headers=GRAPHQL_HEADERS, timeout=timeout)
        if meta is not None:
            meta["status"] = r.status_code
        if r.status_code in RETRYABLE_STATUS:
            raise _Retryable(f"Facebook API returned status {r.status_code}")
        if r.status_code >= 400:
            raise RefusalError(f"Facebook API returned status {r.status_code}")
        payload = r.json()  # ValueError on an HTML interstitial -> retried
        errors = payload.get("errors") or []
        if errors and not payload.get("data"):
            if _is_rate_limit(errors[0].get("message")):
                raise _RateLimitReply(errors[0].get("message"))
            raise RefusalError(f"Facebook GraphQL error: {errors[0].get('message')}")
        if errors:
            log.warning("partial GraphQL error, continuing with data: %s", errors[0].get("message"))
        return payload

    async def search_page(self, url: str) -> str:
        last: Exception | None = None
        for _ in range(PAGE_ATTEMPTS):
            seq, gap = await self._pace()
            rec = {"seq": seq, "gap": gap, "started": time.monotonic()}
            try:
                r = await self._http.get(url, headers=SEARCH_PAGE_HEADERS, timeout=ATTEMPT_TIMEOUT_S)
                if r.status_code >= 400:
                    self._record("search_page", "refusal", status=r.status_code, detail="HTTP error", **rec)
                    raise RefusalError(f"Facebook page returned status {r.status_code}")
                html = r.text
                # A blocked or login-walled page is a 200 without the search payload.
                if "marketplace_search" not in html:
                    self._record("search_page", "refusal", status=r.status_code, detail="no results payload", **rec)
                    raise RefusalError("Facebook served the search page without results")
                self._record("search_page", "ok", status=r.status_code, **rec)
                return html
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self._record("search_page", "error", detail=f"{type(e).__name__}: {e}", **rec)
                last = e
        raise last or RuntimeError("Facebook search page failed")

    async def download(self, url: str, timeout: float = 10.0) -> tuple[bytes, str]:
        r = await self._http.get(url, headers={"user-agent": _UA}, timeout=timeout)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "")
