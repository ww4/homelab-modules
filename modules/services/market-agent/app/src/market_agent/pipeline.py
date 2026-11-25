"""One run of a brief: search, read, check, look up, judge, store."""

from __future__ import annotations

import asyncio
import hashlib
import time
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import checks as general
from . import judge as judge_mod
from .brief import Brief
from .config import Settings
from .facebook.client import ListingGone, Marketplace
from .facebook.transport import Pace, RateLimited, Transport
from .geo import driving_miles, miles_between
from .models import Candidate, Coordinates, Detail, Listing, Signal, readable
from .policy import effective
from .store import Store
from .vehicles import checks as vchecks
from .vehicles import configs as vconfigs
from .vehicles import photo_transmission
from .vehicles import knowledge
from .vehicles.extract import vehicle_facts

log = logging.getLogger(__name__)
GEO_CACHE_SECONDS = 180 * 86400
RECHECK_SECONDS = 20 * 3600  # tracked listings: confirm they're still for sale about daily


@dataclass
class RunResult:
    brief: str
    searches: list[dict[str, Any]] = field(default_factory=list)
    seen: int = 0
    new: int = 0
    detail_failures: int = 0
    unread: int = 0  # new listings skipped because max_details was reached
    rate_limited: int = 0  # listings not read because Facebook rate-limited the run
    unscored: int = 0  # candidates left for the next run because the judge budget ran out
    excluded: int = 0
    judged: int = 0
    judge_failures: int = 0
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def broken(self) -> bool:
        """Every search failed: the scraper is broken, not the market quiet."""
        return bool(self.searches) and not any(s["ok"] for s in self.searches)

    def summary(self) -> dict[str, Any]:
        return {
            "searches_ok": sum(1 for s in self.searches if s["ok"]),
            "searches_failed": [s for s in self.searches if not s["ok"]],
            "seen": self.seen, "new": self.new, "detail_failures": self.detail_failures, "unread": self.unread, "rate_limited": self.rate_limited, "unscored": self.unscored,
            "excluded": self.excluded, "judged": self.judged, "judge_failures": self.judge_failures,
        }


Progress = Callable[[str], None]


async def resolve_place(mp: Marketplace, store: Store, place: str) -> Coordinates:
    key = f"geo:{place.strip().lower()}"
    cached = store.cache_get(key, max_age=GEO_CACHE_SECONDS)
    if cached:
        return Coordinates.model_validate(cached)
    coords = await mp.coordinates(place)
    if not coords:
        raise ValueError(f"Facebook couldn't find {place!r}; try 'City, ST'")
    store.cache_put(key, coords.model_dump())
    return coords


def _eval_key(brief: Brief, c: Candidate) -> str:
    material = {
        "brief": brief.source_hash,
        "judge": judge_mod.JUDGE_VERSION,
        "price": c.listing.price,
        "title": c.detail.title if c.detail else c.listing.title,
        "description": c.detail.description if c.detail else "",
        "checks": [(x.name, x.status) for x in c.checks],
        "refs": len(c.references),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:20]


async def _download_photos(t: Transport, c: Candidate, dest: Path, limit: int) -> list[Path]:
    if not c.detail or limit <= 0:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for n, url in enumerate(c.detail.images[:limit]):
        path = dest / f"photo-{n + 1:02d}.jpg"
        if not path.exists():
            try:
                data, ctype = await t.download(url)
            except httpx.HTTPError as e:
                log.info("photo %d for %s failed: %s", n + 1, c.listing.id, e)
                continue
            if not ctype.startswith("image/") or data[:3] != b"\xff\xd8\xff":
                # Facebook's CDN serves JPEG; anything else is an error page.
                continue
            path.write_bytes(data)
        paths.append(path)
    return paths


async def run(
    settings: Settings,
    brief: Brief,
    *,
    judge: bool = True,
    refresh: bool = False,
    progress: Progress = lambda _msg: None,
    pace: Pace | None = None,
) -> RunResult:
    """pace: shared by the watches of one job, so a slowdown carries over."""
    settings.ensure_dirs()
    store = Store(settings.db_path)
    via = "proxy" if settings.proxy else "direct"
    transport = Transport(
        proxy=settings.proxy, pace=pace or Pace(interval=settings.fb_interval, job=f"run-{brief.name}-{int(time.time())}"),
        on_request=lambda rec: store.log_request(brief.name, via, rec))
    mp = Marketplace(transport)
    http = httpx.AsyncClient(follow_redirects=True)
    result = RunResult(brief=brief.name)
    carried: set[str] = set()
    try:
        home = await resolve_place(mp, store, brief.location.home)
        dests = [(p.name, await resolve_place(mp, store, p.place)) for p in brief.location.destinations]

        # 1. Search every query, once per price band. Ask Facebook for a little
        # past the ceiling so price near misses can be found at all.
        found: dict[str, Listing] = {}
        for q in brief.search.queries:
            for lo, hi in price_bands(brief):
                label = q if (lo, hi) == (brief.price.min, brief.price.max) else f"{q} [{_money(lo)}-{_money(hi)}]"
                progress(f"search: {label}")
                out = await mp.search(
                    q, brief.location.home, home,
                    radius_miles=brief.location.search_radius_miles,
                    min_price=lo, max_price=hi,
                    listed_within_days=brief.search.listed_within_days or None,
                )
                result.searches.append({"query": label, "ok": out.ok, "count": len(out.listings),
                                        "source": out.source, "error": out.error})
                for listing in out.listings:
                    found.setdefault(listing.id, listing)
        result.seen = len(found)
        if result.broken:
            store.record_run(brief.name, result.summary())
            return result

        # Anything an earlier run found and nobody has seen sold stays tracked:
        # Facebook's results vary run to run, and a listing that drops out of
        # one search is not gone. Its page is re-read below, which is also how
        # a sale is noticed.
        cutoff = time.time() - brief.search.keep_days * 86400
        starred_ids = store.favorites(brief.name)
        # Starred listings, and any listing that was ever worth a look, are
        # tracked however old until they sell or vanish: a bookmark, not a
        # search result. "Not interested" ones are dropped for good.
        kept = store.kept(brief.name)
        if not kept:
            # First run with bookmarks: everything already scored worth a look counts.
            for prior in store.all(brief.name):
                e = effective(prior, brief) if prior.evaluation else None
                if not prior.excluded and e and e.verdict in ("top", "consider"):
                    store.keep(brief.name, prior.listing.id)
            kept = store.kept(brief.name)
        bookmarked = starred_ids | kept
        dismissed = store.dismissed(brief.name)
        for lid in dismissed:
            found.pop(lid, None)
        for prior in store.recent(brief.name, since=cutoff) + store.get_many(brief.name, bookmarked):
            if prior.listing.id in dismissed or (prior.detail and prior.detail.is_sold):
                continue
            # Only what is still in play: a listing already ruled out by a hard
            # fact (FWD, rebuilt title) has nothing to track.
            in_play = not prior.excluded or prior.facts.get("near_miss") or prior.listing.id in bookmarked
            if prior.listing.id not in found and in_play:
                found[prior.listing.id] = prior.listing.model_copy(update={"query": "(tracked)"})
                carried.add(prior.listing.id)

        # 2. Cheap filter on the search-result price, then read listing pages.
        pending: list[Candidate] = []
        for listing in found.values():
            pc = general.price_check(brief, listing.price)
            c = Candidate(listing=listing, checks=[pc])
            # A price just over the ceiling is a near miss, judged later; only
            # bait below the floor and prices well past the ceiling stop here.
            over_but_near = (
                pc.status == "fail" and brief.price.max is not None and listing.price is not None
                and brief.price.max < listing.price <= brief.price.max * NEAR_MISS_PRICE
            )
            if pc.status == "fail" and not over_but_near:
                result.excluded += 1
                store.save(brief.name, c, None)
                continue
            pending.append(c)

        cands: list[Candidate] = []
        fetched = 0
        limited = False
        for n, c in enumerate(pending, 1):
            prior = store.get(brief.name, c.listing.id)
            if prior and not readable(prior[0].detail):
                # A page read by a run that never finished is in the cache.
                cached = store.cache_get(f"detail:{c.listing.id}", max_age=None)
                prior[0].detail = Detail.model_validate(cached) if cached else None
                if not readable(prior[0].detail):
                    prior[0].detail = None
            elif not prior and (cached := store.cache_get(f"detail:{c.listing.id}", max_age=None)):
                if readable(d := Detail.model_validate(cached)):
                    prior = (Candidate(listing=c.listing, detail=d), None, c.listing.price)
            # A tracked listing is re-read about once a day to see if it sold.
            stale = bool(prior and prior[0].detail and time.time() - (prior[0].detail.fetched_at or 0) > RECHECK_SECONDS)
            reread = refresh or (c.listing.id in carried and stale)
            have = prior[0].detail if prior else None
            if have and (limited or fetched >= brief.search.max_details
                         or (not reread and prior[2] == c.listing.price)):
                # Cached page: unchanged, or reads are spent/limited (stale beats missing).
                c.detail = have
            elif limited:
                # Facebook is rate-limiting: read next run, never judge without the page.
                result.rate_limited += 1
                continue
            elif fetched >= brief.search.max_details:
                # Never silently: counted, reported, and read first next run.
                result.unread += 1
                continue
            else:
                fetched += 1
                progress(f"read listing {n}/{len(pending)}: {c.listing.title}")
                try:
                    c.detail = await mp.detail(c.listing.id)
                    # Saved at once: a run that dies later must not cost these reads again.
                    store.cache_put(f"detail:{c.listing.id}", c.detail.model_dump())
                except ListingGone:
                    store.mark_gone(brief.name, c.listing.id)
                    continue
                except RateLimited as e:
                    log.warning("stopping listing reads for %s: %s", brief.name, e)
                    limited = True
                    if not have:
                        result.rate_limited += 1
                        continue
                    c.detail = have
                except Exception as e:  # noqa: BLE001 - keep going; count it
                    log.warning("couldn't read listing %s: %s", c.listing.id, e)
                    result.detail_failures += 1
                    if not have:
                        # Judging a listing without its page is how "the ad has no
                        # description" got written about ads that had one.
                        continue
                    c.detail = have
                    c.signals.append(Signal(name="detail unavailable", detail=f"re-read failed, using the earlier reading: {e}", severity="info"))
            cands.append(c)

        # 3. Facts and hard limits.
        kb = knowledge.load() if brief.kind == "vehicle" else None
        for c in cands:
            d = c.detail
            text = f"{c.listing.title}\n{d.description if d else ''}"
            c.checks = []
            if d and d.price is not None:
                c.listing.price = d.price
            c.checks.append(general.price_check(brief, c.listing.price))
            if d and (avail := general.availability_check(d.is_sold, d.is_pending)):
                c.checks.append(avail)
            if ex := general.exclude_words_check(brief, text):
                c.checks.append(ex)

            where = d.coords if d else None
            town = (d.location_text if d and d.location_text else None) or c.listing.location_text
            if where is None and town:
                # The listing page didn't say where; the search result named a town.
                # Town-level is plenty for a distance limit (and is cached per town).
                try:
                    where = await resolve_place(mp, store, town)
                    c.facts["location_source"] = f"town from search result ({town})"
                except Exception as e:  # noqa: BLE001 - stays "unknown", which the page shows
                    log.info("couldn't place %r: %s", town, e)
            if where is not None:
                async def measure(o: Coordinates) -> tuple[float, str]:
                    if brief.location.distance == "radius":
                        return miles_between(o.latitude, o.longitude, where.latitude, where.longitude), "radius"
                    return await driving_miles(
                        http, settings.osrm_url, o.latitude, o.longitude, where.latitude, where.longitude
                    )

                miles, method = await measure(home)
                c.facts["miles_from_home"] = round(miles)
                c.facts["distance_method"] = method
                measured, origin = miles, "home"
                for name, dc in dests:
                    dm, _ = await measure(dc)
                    c.facts[f"miles_from_{name.lower()}"] = round(dm)
                    if brief.location.measure_from == "nearest" and dm < measured:
                        measured, origin = dm, name
                c.checks.append(general.distance_check(brief, measured, method, origin))
            else:
                c.checks.append(general.distance_check(brief, None, "", "home"))

            c.signals.extend(general.dealer_signals(text))
            if d and (age := general.age_signal(d.created_at)):
                c.signals.append(age)

            if kb is not None and brief.vehicle is not None:
                c.facts.update(vehicle_facts(c.listing, d))
                vchecks.apply_knowledge(c, kb)

        general.shared_text_signals(cands)

        # 4. Look things up for the survivors (VIN may settle drivetrain), then vehicle checks.
        survivors = [c for c in cands if not c.excluded]
        if kb is not None and brief.vehicle is not None:
            # Look up anything not already ruled out by a hard fact; distance and
            # price may still leave it as a near miss.
            lookups = [c for c in cands if all(ch.name in ("distance", "price") for ch in c.failed_checks)]
            for c in lookups:
                await vchecks.apply_nhtsa(c, http, store)
                # What the model year was sold as (after the VIN, which can settle drivetrain).
                vconfigs.apply(c.facts, (c.detail.title if c.detail else None) or c.listing.title)
                # Still open (or "manual" from the text alone)? The photos often show the shifter.
                if judge and settings.judge != "none" and photo_transmission.needs_check(c.facts):
                    await photo_transmission.apply(
                        c, settings, store, lambda cand, dest, n: _download_photos(transport, cand, dest, n))
            for c in cands:
                c.checks.extend(vchecks.vehicle_checks(brief.vehicle, c.facts))
            survivors = [c for c in cands if not c.excluded]
        result.excluded += sum(1 for c in cands if c.excluded)
        near = [c for c in cands if c.excluded and near_miss(brief, c)]
        for c in near:
            c.facts["near_miss"] = near_miss(brief, c)

        # 5. Judge, best-looking first, reusing evaluations nothing has changed.
        def pre_rank(c: Candidate) -> tuple[float, float]:
            # Never order by unknown checks: an unknown is often our parser's
            # miss, and that pushed a strong listing out of the budget once.
            return (c.facts.get("miles_from_home") or 999, c.listing.price or 0)

        survivors.sort(key=pre_rank)
        near.sort(key=pre_rank)
        favs = store.favorites(brief.name)
        in_pool = {c.listing.id for c in survivors + near}
        starred = [c for c in cands if c.listing.id in favs and c.listing.id not in in_pool and c.detail]
        judging = judge and settings.judge != "none"
        keys: dict[str, str] = {}
        # A listing waiting for a new score keeps its last one on the page
        # (marked re-scoring) instead of vanishing into "not scored yet".
        fallback: dict[str, tuple[Any, str]] = {}

        # Stage 1: every candidate gets a quick screen (or keeps one nothing has invalidated).
        screen_q: list[Candidate] = []
        for pool, budget in ((survivors, brief.judge.max_candidates), (near, brief.judge.max_near_misses), (starred, None)):
            queued = 0
            for c in pool:
                key = _eval_key(brief, c)
                keys[c.listing.id] = key
                prior = store.get(brief.name, c.listing.id)
                if (prior and prior[0].evaluation and not refresh
                        and prior[1] == f"{prior[0].evaluation.depth}:{key}"):
                    c.evaluation = prior[0].evaluation
                    continue
                if prior and prior[0].evaluation and prior[1]:
                    fallback[c.listing.id] = (prior[0].evaluation, prior[1])
                if judging and (budget is None or queued < budget or c.listing.id in favs):
                    screen_q.append(c)
                    queued += 1
                elif judging:
                    result.unscored += 1  # over budget: scored next run (keeps its last score meanwhile)

        sem = asyncio.Semaphore(settings.judge_concurrency)

        async def one(c: Candidate, depth: str) -> None:
            async with sem:
                workdir = settings.photos_dir / c.listing.id
                n_photos = brief.judge.photos if depth == "full" else min(2, brief.judge.photos)
                photos = await _download_photos(transport, c, workdir, n_photos)
                progress(f"{depth}: {c.listing.title} ({len(photos)} photos)")
                for attempt in (1, 2):
                    try:
                        c.evaluation = await judge_mod.evaluate(settings, brief, c, photos, workdir, depth)
                        c.judge_error = None
                        result.judged += 1
                        return
                    except Exception as e:  # noqa: BLE001 - one bad call shouldn't sink the run
                        c.judge_error = str(e)
                        log.warning("%s attempt %d failed for %s: %s", depth, attempt, c.listing.id, e)
                result.judge_failures += 1

        await asyncio.gather(*(one(c, "screen") for c in screen_q))
        stale_keys: dict[str, str] = {}
        for c in survivors + near + starred:
            if c.evaluation is None and c.listing.id in fallback:
                c.evaluation, stale_keys[c.listing.id] = fallback[c.listing.id]
                c.facts["rescore_pending"] = True

        # Stage 2: the full read, only for contenders and starred listings.
        full_q: list[Candidate] = []
        for c in survivors + near + starred:
            if not (c.evaluation and c.evaluation.depth == "screen"):
                continue
            eff = effective(c, brief)
            contender = eff is not None and eff.verdict in ("top", "consider")
            if c.listing.id in favs or (contender and len(full_q) < brief.judge.max_full):
                full_q.append(c)
        await asyncio.gather(*(one(c, "full") for c in full_q))

        # Every card on the watch page gets a photo, scored or not.
        for c in survivors + near + starred:
            if not (settings.photos_dir / c.listing.id / "photo-01.jpg").exists():
                await _download_photos(transport, c, settings.photos_dir / c.listing.id, 1)

        for c in cands:
            k = None
            if c.evaluation:
                # A kept-over score keeps its old key, so the next run re-scores it.
                k = stale_keys.get(c.listing.id) or f"{c.evaluation.depth}:{keys.get(c.listing.id) or _eval_key(brief, c)}"
            if store.save(brief.name, c, k):
                result.new += 1
            eff = effective(c, brief) if c.evaluation else None
            if not c.excluded and eff and eff.verdict in ("top", "consider"):
                store.keep(brief.name, c.listing.id)
        result.candidates = cands
        store.record_run(brief.name, result.summary())
        return result
    finally:
        await http.aclose()
        await transport.aclose()
        store.close()


def _money(v: float | None) -> str:
    return "any" if v is None else f"${v:,.0f}"


def price_bands(brief: Brief) -> list[tuple[float | None, float | None]]:
    """The price ranges each query runs over (see Search.price_bands)."""
    lo = brief.price.min
    hi = float(round(brief.price.max * NEAR_MISS_PRICE)) if brief.price.max is not None else None
    n = max(1, brief.search.price_bands)
    if n == 1 or hi is None:
        return [(lo, hi)]
    start = lo or 0.0
    step = (hi - start) / n
    edges = [round(start + step * i, -1) for i in range(n + 1)]
    edges[0], edges[-1] = lo, round(hi)
    return [(edges[i], edges[i + 1]) for i in range(n)]


NEAR_MISS_DISTANCE = 1.25  # up to 25% past the distance limit
NEAR_MISS_PRICE = 1.10     # up to 10% over the price ceiling


def near_miss(brief: Brief, c: Candidate) -> str | None:
    """Excluded only by distance and/or a price ceiling, and not by much.

    These are shown (and judged, after the real candidates) because a limit
    of "50 miles" or "$6,000" is usually softer than a clean title or AWD.
    """
    failed = c.failed_checks
    if not failed or any(ch.name not in ("distance", "price") for ch in failed):
        return None
    reasons = []
    for ch in failed:
        if ch.name == "distance":
            miles = c.facts.get("miles_from_home")
            limit = brief.location.max_miles
            if brief.location.measure_from == "nearest":
                miles = min(v for k, v in c.facts.items() if k.startswith("miles_from_") and v is not None)
            if miles is None or not limit or miles > limit * NEAR_MISS_DISTANCE:
                return None
            reasons.append(f"{miles:.0f} mi vs the {limit:.0f} mi limit")
        else:
            price, ceiling = c.listing.price, brief.price.max
            if price is None or not ceiling or price > ceiling * NEAR_MISS_PRICE:
                return None
            reasons.append(f"${price:,.0f} vs the ${ceiling:,.0f} ceiling")
    return "just outside: " + ", ".join(reasons)


__all__ = ["run", "RunResult", "miles_between"]
