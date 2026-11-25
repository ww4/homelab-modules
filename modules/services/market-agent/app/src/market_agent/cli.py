"""market-agent command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import brief as briefs
from .config import Settings
from .facebook.transport import Pace
from .pipeline import run
from .models import awaiting_read
from .policy import effective, rank
from .report import Section, render, sections_from_store
from .store import Store


def _err(msg: str) -> int:
    print(f"market-agent: {msg}", file=sys.stderr)
    return 2


def cmd_brief(s: Settings, a: argparse.Namespace) -> int:
    s.ensure_dirs()
    if a.action == "new":
        path = briefs.new_brief(a.name, s.briefs_dir, a.example)
        print(f"wrote {path}\nedit it, then: market-agent brief check {a.name}")
        return 0
    if a.action == "list":
        for p in sorted(s.briefs_dir.glob("*.toml")):
            try:
                b = briefs.load(p)
                print(f"{b.name:24} {b.kind:8} {b.title}")
            except Exception as e:  # noqa: BLE001 - show every brief, broken or not
                print(f"{p.stem:24} INVALID  {e}")
        return 0
    if a.action == "check":
        b = briefs.load(briefs.resolve(a.name, s.briefs_dir))
        print(json.dumps(b.model_dump(exclude={"source_hash"}), indent=2))
        print(f"\nok: {b.name} is valid ({len(b.search.queries)} queries)")
        return 0
    if a.action == "path":
        print(briefs.resolve(a.name, s.briefs_dir))
        return 0
    return _err(f"unknown brief action {a.action}")


def _print_ranked(cands: list, brief) -> None:
    for c in rank(cands, brief):
        if c.excluded:
            continue
        e = c.evaluation
        eff = effective(c, brief)
        head = f"{eff.verdict:8} {eff.score:4.1f}" if eff else "unjudged     "
        miles = c.facts.get("miles_from_home")
        dist = f"{miles:>3} mi" if miles is not None else "  ? mi"
        print(f"{head}  ${(c.listing.price or 0):>7,.0f}  {dist}  {c.listing.title[:48]:48}  {c.listing.url}")
        if e:
            print(f"{'':15}{e.headline}")


def cmd_run(s: Settings, a: argparse.Namespace) -> int:
    b = briefs.load(briefs.resolve(a.brief, s.briefs_dir))
    if a.no_judge:
        s = s.model_copy(update={"judge": "none"})
    progress = (lambda m: print(f"  {m}", file=sys.stderr)) if not a.quiet else (lambda _m: None)
    result = asyncio.run(run(s, b, judge=not a.no_judge, refresh=a.refresh, progress=progress))
    summary = result.summary()
    if result.broken:
        print(json.dumps(summary, indent=2), file=sys.stderr)
        return _err("every search failed - Facebook is refusing or the doc_ids changed; nothing was evaluated")
    if a.json:
        print(json.dumps({"summary": summary, "candidates": [c.model_dump() for c in rank(result.candidates, b)]},
                         indent=1, default=str))
    else:
        _print_ranked(result.candidates, b)
        print(f"\n{summary['seen']} seen, {summary['new']} new, {summary['excluded']} excluded, "
              f"{summary['judged']} judged ({summary['judge_failures']} judge failures, "
              f"{summary['detail_failures']} unreadable listings, "
              f"{len(summary['searches_failed'])} failed searches)", file=sys.stderr)
    if a.report:
        write_page(s, Path(a.report), "Marketplace Watch", [a.brief])
    return 1 if summary["searches_failed"] or summary["judge_failures"] else 0


def write_page(s: Settings, out: Path, title: str, names: list[str] | None = None, interactive: bool = False,
               inline_photos: bool = False) -> int:
    """Render briefs (default: all) from the store to `out` atomically, readable by nginx."""
    names = names or [p.stem for p in sorted(s.briefs_dir.glob("*.toml"))]
    loaded = [briefs.load(briefs.resolve(n, s.briefs_dir)) for n in names]
    store = Store(s.db_path)
    try:
        html = render(sections_from_store(store, loaded, s.home / "requests"), s.photos_dir, title=title,
                      interactive=interactive,
                      photo_files=None if inline_photos else out.parent / "photos")
    finally:
        store.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(html)
    os.chmod(tmp, 0o644)
    tmp.replace(out)
    return len(html)


def cmd_page(s: Settings, a: argparse.Namespace) -> int:
    if not any(s.briefs_dir.glob("*.toml")) and not a.brief:
        return _err(f"no briefs in {s.briefs_dir}")
    size = write_page(s, Path(a.out), a.title, a.brief, interactive=a.interactive, inline_photos=a.inline_photos)
    print(f"wrote {a.out} ({size // 1024} KB)", file=sys.stderr)
    return 0


def cmd_serve(s: Settings, a: argparse.Namespace) -> int:
    from .web import serve

    serve(s, a.port, lambda: write_page(s, Path(a.page), a.title, interactive=True))
    return 0


def cmd_probe(s: Settings, a: argparse.Namespace) -> int:
    """Measure Facebook's rate limit (see market_agent.probe)."""
    from .probe import run_auto

    res = run_auto(s, allow_direct=a.allow_direct, interval=a.interval, max_requests=a.max_requests)
    print(json.dumps(res, indent=1))
    return 1 if res.get("aborted") else 0


def _notify(cmd: str | None, title: str, body: str) -> None:
    if not cmd:
        return
    try:
        subprocess.run([*shlex.split(cmd), title, body], check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"market-agent: notify command failed: {e}", file=sys.stderr)


def cmd_watch(s: Settings, a: argparse.Namespace) -> int:
    """Run every brief, rebuild the page, notify about new top listings."""
    from .probe import facebook_lock

    with facebook_lock(s.home):  # waits out a probe session rather than racing it
        return _watch(s, a)


def _watch(s: Settings, a: argparse.Namespace) -> int:
    names = [p.stem for p in sorted(s.briefs_dir.glob("*.toml"))]
    if not names:
        return _err(f"no briefs in {s.briefs_dir}")
    # A run asked for from the page ("+ New", Edit, unpause) searches only those
    # watches; the timer's runs search every watch that isn't paused.
    reqdir = s.home / "requests"
    requested = sorted(p.name for p in reqdir.iterdir()) if reqdir.is_dir() else []
    for r in requested:
        (reqdir / r).unlink(missing_ok=True)
    only = {n for n in requested if n in names}
    broken: list[str] = []
    failed: list[str] = []
    loaded = []
    pace = Pace(interval=s.fb_interval)  # one slowdown and backoff budget for the whole job
    for name in names:
        try:
            b = briefs.load(briefs.resolve(name, s.briefs_dir))
        except Exception as e:  # noqa: BLE001 - one bad brief must not stop the others
            failed.append(f"{name}: invalid brief ({e})")
            continue
        loaded.append(b)
        if b.paused or (only and b.name not in only):
            continue
        try:
            result = asyncio.run(run(s, b, pace=pace))
        except Exception as e:  # noqa: BLE001
            failed.append(f"{b.title}: run failed ({e})")
            continue
        if result.broken:
            broken.append(b.title)
        sm = result.summary()
        if sm.get("unread"):
            failed.append(f"{b.title}: {sm['unread']} new listings not read (search.max_details reached)")
        if sm["searches_failed"] or sm["judge_failures"]:
            failed.append(f"{b.title}: {len(sm['searches_failed'])} searches and {sm['judge_failures']} scores failed")

    write_page(s, Path(a.page), a.title, [b.name for b in loaded], interactive=True)
    store = Store(s.db_path)
    try:
        news: list[str] = []
        drops: list[str] = []
        for b in loaded:
            tops = []
            dismissed = store.dismissed(b.name)
            for c in store.all(b.name):
                eff = effective(c, b)
                if (not c.excluded and not awaiting_read(c) and eff and eff.verdict == "top"
                        and c.listing.id not in dismissed):
                    tops.append((c, eff))
            fresh = store.unnotified(b.name, [c.listing.id for c, _ in tops])
            for c, eff in tops:
                if c.listing.id in fresh:
                    news.append(f"{b.title}: {c.listing.title} {c.listing.price_text} "
                                f"({eff.score:.1f}) {c.listing.url}")
            store.mark_notified(b.name, fresh)
            # Starred listings: tell us when the price drops (once per new price).
            for lid in store.favorites(b.name):
                hist = [p for _, p in store.price_history(b.name, lid) if p is not None]
                if len(hist) >= 2 and hist[-1] < hist[-2]:
                    key = f"price:{lid}:{hist[-1]:.0f}"
                    if store.unnotified(b.name, [key]):
                        got = store.get(b.name, lid)
                        title = got[0].listing.title if got else lid
                        drops.append(f"{title}: ${hist[-2]:,.0f} -> ${hist[-1]:,.0f} "
                                     f"https://www.facebook.com/marketplace/item/{lid}")
                        store.mark_notified(b.name, [key])
    finally:
        store.close()

    link = f"\n{a.page_url}" if a.page_url else ""
    if news:
        _notify(a.notify_cmd, f"market-agent: {len(news)} new worth contacting", "\n".join(news) + link)
    if drops:
        _notify(a.notify_cmd, f"market-agent: price drop on {len(drops)} starred", "\n".join(drops) + link)
    if broken:
        _notify(a.notify_cmd, "market-agent: Facebook search is failing",
                "Every search failed for: " + ", ".join(broken)
                + ". Facebook is refusing requests or its query ids changed; results are stale." + link)
    for line in failed:
        print(f"market-agent: {line}", file=sys.stderr)
    return 1 if broken or failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="market-agent", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("brief", help="create, list and check briefs")
    bsub = pb.add_subparsers(dest="action", required=True)
    bn = bsub.add_parser("new", help="start a brief from an example")
    bn.add_argument("name")
    bn.add_argument("--example", default="general", choices=briefs.EXAMPLES)
    bsub.add_parser("list")
    bc = bsub.add_parser("check", help="validate a brief and print what it means")
    bc.add_argument("name")
    bp = bsub.add_parser("path", help="print a brief's file path (to edit it)")
    bp.add_argument("name")

    pr = sub.add_parser("run", help="search, check, judge and store")
    pr.add_argument("brief")
    pr.add_argument("--no-judge", action="store_true", help="checks and lookups only")
    pr.add_argument("--refresh", action="store_true", help="re-read listings and re-judge everything")
    pr.add_argument("--report", metavar="FILE.html", help="also write the watch page for this brief")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("-q", "--quiet", action="store_true")

    pp = sub.add_parser("page", help="write the watch page (all briefs, or --brief ...) from stored results")
    pp.add_argument("out", metavar="FILE.html")
    pp.add_argument("--brief", action="append", help="limit to these briefs (repeatable)")
    pp.add_argument("--title", default="Marketplace Watch")
    pp.add_argument("--interactive", action="store_true", help="include star buttons (needs `serve` behind /api/)")
    pp.add_argument("--inline-photos", action="store_true",
                    help="embed photos in the HTML (one file, much heavier) instead of writing photos/ next to it")

    ps = sub.add_parser("serve", help="the /api/favorites endpoint for stars (loopback only)")
    ps.add_argument("--port", type=int, default=3341)
    ps.add_argument("--page", required=True, metavar="FILE.html", help="page to re-render after a star changes")
    ps.add_argument("--title", default="Marketplace Watch")

    pq = sub.add_parser("probe-limit", help="measure Facebook's rate limit: the next scheduled probe session")
    pq.add_argument("--interval", type=float, help="seconds between requests (default: the next in the schedule)")
    pq.add_argument("--max-requests", type=int, default=400)
    pq.add_argument("--allow-direct", action="store_true", help="probe without a proxy (from this machine's own IP)")
    pw = sub.add_parser("watch", help="run every brief, rebuild the page, notify about new top listings")
    pw.add_argument("--page", required=True, metavar="FILE.html")
    pw.add_argument("--page-url", help="link included in notifications")
    pw.add_argument("--notify-cmd", help="command run as: CMD <title> <body>")
    pw.add_argument("--title", default="Marketplace Watch")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        s = Settings.from_env()
        if a.cmd == "brief":
            return cmd_brief(s, a)
        if a.cmd == "run":
            return cmd_run(s, a)
        if a.cmd == "page":
            return cmd_page(s, a)
        if a.cmd == "probe-limit":
            return cmd_probe(s, a)
        if a.cmd == "watch":
            return cmd_watch(s, a)
        if a.cmd == "serve":
            return cmd_serve(s, a)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        return _err(str(e))
    return _err("unknown command")
