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


@dataclass
class Pace:
    """Request pacing shared by every watch in one job.

    Each watch used to start a fresh Transport at full speed, so the watch after
    one that hit the wall ran straight back into it.
    """

    interval: float = 1.5
    backoff_left_s: float = sum(RATE_LIMIT_WAITS_S)


# on_request(kind, outcome, interval): outcome is "ok", "rate_limit", "refusal" or "error".
RequestHook = Callable[[str, str, float], None]

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

    def _record(self, kind: str, outcome: str) -> None:
        if self._on_request:
            try:
                self._on_request(kind, outcome, self.pace.interval)
            except Exception as e:  # noqa: BLE001 - logging must never break a read
                log.warning("couldn't log a Facebook request: %s", e)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pace(self) -> None:
        async with self._lock:
            wait = self._last + self.pace.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

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
                p.interval = min(p.interval * RATE_LIMIT_SLOWDOWN, RATE_LIMIT_MAX_INTERVAL_S)
                wait = next(waits, None)
                if wait is None or wait > p.backoff_left_s:
                    break
                p.backoff_left_s -= wait
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
            try:
                await self._pace()
                payload = await asyncio.wait_for(
                    self._attempt(body, min(ATTEMPT_TIMEOUT_S, remaining)), timeout=remaining
                )
                self._record(kind, "ok")
                return payload
            except _RateLimitReply:
                self._record(kind, "rate_limit")
                raise
            except RefusalError:
                self._record(kind, "refusal")
                raise
            except (httpx.HTTPError, asyncio.TimeoutError, _Retryable, ValueError) as e:
                self._record(kind, "error")
                last = e
                backoff = 2 ** (attempt - 1) * (0.5 + random.random())
                if attempt == MAX_ATTEMPTS or time.monotonic() + backoff >= deadline:
                    break
                await asyncio.sleep(backoff)
        raise last or RuntimeError("Facebook request failed")

    async def _attempt(self, body: dict[str, str], timeout: float) -> dict[str, Any]:
        r = await self._http.post(GRAPHQL_URL, data=body, headers=GRAPHQL_HEADERS, timeout=timeout)
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
            try:
                await self._pace()
                r = await self._http.get(url, headers=SEARCH_PAGE_HEADERS, timeout=ATTEMPT_TIMEOUT_S)
                if r.status_code >= 400:
                    self._record("search_page", "refusal")
                    raise RefusalError(f"Facebook page returned status {r.status_code}")
                html = r.text
                # A blocked or login-walled page is a 200 without the search payload.
                if "marketplace_search" not in html:
                    self._record("search_page", "refusal")
                    raise RefusalError("Facebook served the search page without results")
                self._record("search_page", "ok")
                return html
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self._record("search_page", "error")
                last = e
        raise last or RuntimeError("Facebook search page failed")

    async def download(self, url: str, timeout: float = 10.0) -> tuple[bytes, str]:
        r = await self._http.get(url, headers={"user-agent": _UA}, timeout=timeout)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "")
