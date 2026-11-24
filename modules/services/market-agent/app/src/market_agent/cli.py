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
from .pipeline import rank, run
from .report import Section, render
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


def _print_ranked(cands: list) -> None:
    for c in rank(cands):
        if c.excluded:
            continue
        e = c.evaluation
        head = f"{e.verdict:8} {e.score:4.1f}" if e else "unjudged     "
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
        print(json.dumps({"summary": summary, "candidates": [c.model_dump() for c in rank(result.candidates)]},
                         indent=1, default=str))
    else:
        _print_ranked(result.candidates)
        print(f"\n{summary['seen']} seen, {summary['new']} new, {summary['excluded']} excluded, "
              f"{summary['judged']} judged ({summary['judge_failures']} judge failures, "
              f"{summary['detail_failures']} unreadable listings, "
              f"{len(summary['searches_failed'])} failed searches)", file=sys.stderr)
    if a.report:
        cmd_page(s, argparse.Namespace(brief=[a.brief], out=a.report, title="Marketplace Watch"))
    return 1 if summary["searches_failed"] or summary["judge_failures"] else 0


def cmd_page(s: Settings, a: argparse.Namespace) -> int:
    names = a.brief or [p.stem for p in sorted(s.briefs_dir.glob("*.toml"))]
    if not names:
        return _err(f"no briefs in {s.briefs_dir}")
    store = Store(s.db_path)
    try:
        sections = []
        for name in names:
            b = briefs.load(briefs.resolve(name, s.briefs_dir))
            sections.append(Section(b, store.all(b.name), store.last_run(b.name)))
    finally:
        store.close()
    html = render(sections, s.photos_dir, title=a.title)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"wrote {out} ({len(html) // 1024} KB, {len(sections)} briefs)", file=sys.stderr)
    return 0


def _notify(cmd: str | None, title: str, body: str) -> None:
    if not cmd:
        return
    try:
        subprocess.run([*shlex.split(cmd), title, body], check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"market-agent: notify command failed: {e}", file=sys.stderr)


def cmd_watch(s: Settings, a: argparse.Namespace) -> int:
    """Run every brief, rebuild the page, notify about new top listings."""
    names = [p.stem for p in sorted(s.briefs_dir.glob("*.toml"))]
    if not names:
        return _err(f"no briefs in {s.briefs_dir}")
    broken: list[str] = []
    failed: list[str] = []
    loaded = []
    for name in names:
        try:
            b = briefs.load(briefs.resolve(name, s.briefs_dir))
        except Exception as e:  # noqa: BLE001 - one bad brief must not stop the others
            failed.append(f"{name}: invalid brief ({e})")
            continue
        loaded.append(b)
        try:
            result = asyncio.run(run(s, b))
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

    out = Path(a.page)
    out.parent.mkdir(parents=True, exist_ok=True)
    store = Store(s.db_path)
    try:
        sections = [Section(b, store.all(b.name), store.last_run(b.name)) for b in loaded]
        tmp = out.with_suffix(".tmp")
        tmp.write_text(render(sections, s.photos_dir, title=a.title))
        os.chmod(tmp, 0o644)
        tmp.replace(out)
        news: list[str] = []
        for b in loaded:
            tops = [c for c in store.all(b.name) if not c.excluded and c.evaluation and c.evaluation.verdict == "top"]
            fresh = store.unnotified(b.name, [c.listing.id for c in tops])
            for c in tops:
                if c.listing.id in fresh:
                    news.append(f"{b.title}: {c.listing.title} {c.listing.price_text} "
                                f"({c.evaluation.score:.1f}) {c.listing.url}")
            store.mark_notified(b.name, fresh)
    finally:
        store.close()

    link = f"\n{a.page_url}" if a.page_url else ""
    if news:
        _notify(a.notify_cmd, f"market-agent: {len(news)} new worth contacting", "\n".join(news) + link)
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
        if a.cmd == "watch":
            return cmd_watch(s, a)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        return _err(str(e))
    return _err("unknown command")
