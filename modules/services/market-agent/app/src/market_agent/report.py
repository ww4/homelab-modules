"""The watch page: every brief's best listings, with a photo, score and price.

One self-contained HTML file. Facebook's photo URLs are signed and expire,
and a published artifact can't load outside images anyway, so each card's
photo is a small JPEG thumbnail embedded as a data: URI, made from the
photos the run already downloaded.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from .brief import Brief
from .models import Candidate
from .pipeline import rank

THUMB_WIDTH = 520
THUMB_QUALITY = 68


@dataclass
class Section:
    brief: Brief
    candidates: list[Candidate]
    last_run: dict[str, Any] | None


def thumbnail(photos_dir: Path, listing_id: str) -> str | None:
    """data: URI for the listing's first photo, or None if we never fetched one."""
    src = photos_dir / listing_id / "photo-01.jpg"
    if not src.is_file():
        return None
    try:
        from PIL import Image  # Pillow; optional at import time so tests stay light
    except ImportError:
        data = src.read_bytes()
    else:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > THUMB_WIDTH:
                im = im.resize((THUMB_WIDTH, round(im.height * THUMB_WIDTH / im.width)))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
            data = buf.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


CSS = """
:root{--bg:#f3f5f6;--surface:#fff;--sunk:#e8ecee;--fg:#192126;--muted:#5b6870;--line:#d8dfe3;--accent:#1f5f8b;
--good:#2a7848;--good-bg:#e2f2e8;--warn:#94590a;--warn-bg:#faefda;--bad:#a1352c;--bad-bg:#f7e2df;
--display:"Archivo","Helvetica Neue",Arial,sans-serif;--body:"Inter",system-ui,-apple-system,sans-serif;
--mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111619;--surface:#192025;--sunk:#0d1114;
--fg:#e3e9ec;--muted:#95a3ab;--line:#2a343a;--accent:#7cb8e3;--good:#72c796;--good-bg:#17301f;--warn:#e3b25a;
--warn-bg:#33281a;--bad:#ee8b83;--bad-bg:#3a1f1c;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#111619;--surface:#192025;--sunk:#0d1114;--fg:#e3e9ec;--muted:#95a3ab;--line:#2a343a;
--accent:#7cb8e3;--good:#72c796;--good-bg:#17301f;--warn:#e3b25a;--warn-bg:#33281a;--bad:#ee8b83;--bad-bg:#3a1f1c;
color-scheme:dark}
body{background:var(--bg);color:var(--fg);font:15px/1.5 var(--body);margin:0}
.wrap{max-width:1180px;margin:0 auto;padding-block:32px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));
padding-right:max(16px,env(safe-area-inset-right,0px));display:flex;flex-direction:column;gap:40px}
h1,h2,h3{font-family:var(--display);margin:0;text-wrap:balance;letter-spacing:-.01em}
h1{font-size:clamp(26px,4.5vw,34px);font-weight:800}h2{font-size:24px;font-weight:800}
h3{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.meta{font:12px var(--mono);color:var(--muted);letter-spacing:.03em}
header.page{display:flex;flex-direction:column;gap:6px}
nav{display:flex;flex-wrap:wrap;gap:8px}
nav a{font-size:13px;color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:var(--surface)}
.brief{display:flex;flex-direction:column;gap:18px;scroll-margin-top:16px}
.brief-head{display:flex;flex-direction:column;gap:4px;border-bottom:1px solid var(--line);padding-bottom:10px}
.limits{font-size:13px;color:var(--muted);max-width:80ch}
.group{display:flex;flex-direction:column;gap:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;min-width:0}
.photo{position:relative;aspect-ratio:4/3;max-width:100%;background:var(--sunk);display:block;overflow:hidden}
.photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.photo .none{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);font-size:13px}
.score{position:absolute;top:10px;left:10px;font:700 15px var(--mono);padding:4px 9px;border-radius:6px;
background:var(--surface);color:var(--fg);box-shadow:0 1px 3px rgba(0,0,0,.25)}
.score.top{background:var(--good);color:var(--surface)}.score.consider{background:var(--warn);color:var(--surface)}
.score.skip{background:var(--bad);color:var(--surface)}
.price{position:absolute;bottom:10px;right:10px;font:700 16px var(--mono);font-variant-numeric:tabular-nums;
padding:3px 9px;border-radius:6px;background:rgba(15,20,23,.82);color:#fff}
.body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:7px;flex:1}
.title{font-weight:650;font-size:15px;line-height:1.3;color:var(--fg);text-decoration:none}
.title:hover{color:var(--accent);text-decoration:underline}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.facts{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.headline{font-size:13.5px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-size:11.5px;padding:2px 7px;border-radius:4px;background:var(--warn-bg);color:var(--warn)}
.chip.bad{background:var(--bad-bg);color:var(--bad)}.chip.near{background:var(--sunk);color:var(--muted)}
details{font-size:13px;margin-top:auto}
details summary{cursor:pointer;color:var(--accent);padding-top:4px}
details ul{margin:4px 0 6px;padding-left:18px}details li{margin:2px 0}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-top:8px}
.refs a{color:var(--accent)}
.more{font-size:13px;color:var(--muted)}
.more ul{padding-left:18px;margin:6px 0}.more a{color:var(--accent)}
.health{font-size:12.5px;color:var(--muted)}.health.broken{color:var(--bad);font-weight:600}
footer{color:var(--muted);font-size:12.5px;max-width:80ch}
@media (prefers-reduced-motion:no-preference){.card{transition:border-color .15s}.card:hover{border-color:var(--accent)}}
"""


def _price(c: Candidate) -> str:
    p = c.detail.price if c.detail and c.detail.price is not None else c.listing.price
    return f"${p:,.0f}" if p is not None else escape(c.listing.price_text)


def _facts(c: Candidate) -> str:
    f = c.facts
    bits = []
    loc = (c.detail.location_text if c.detail and c.detail.location_text else c.listing.location_text) or ""
    if loc:
        bits.append(escape(loc))
    if f.get("mileage"):
        bits.append(f"{f['mileage']:,} mi")
    if f.get("drivetrain"):
        bits.append(escape(str(f["drivetrain"]).upper()))
    for k, v in f.items():
        if k.startswith("miles_from_") and v is not None:
            bits.append(f"{v} mi to {escape(k.removeprefix('miles_from_'))}")
    return "".join(f"<span>{b}</span>" for b in bits)


def _items(label: str, items: list[str]) -> str:
    if not items:
        return ""
    return f'<div class="lbl">{escape(label)}</div><ul>' + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>"


def _chips(c: Candidate) -> str:
    chips = []
    if c.facts.get("near_miss"):
        chips.append(f'<span class="chip near">{escape(c.facts["near_miss"])}</span>')
    for ch in c.checks:
        if ch.status == "unknown":
            chips.append(f'<span class="chip">{escape(ch.name)} unknown</span>')
    for s in c.signals:
        if s.severity == "warning" or s.name == "dealer or flipper":
            chips.append(f'<span class="chip bad">{escape(s.name)}</span>')
    if c.evaluation and c.evaluation.scam_risk != "low":
        chips.append(f'<span class="chip bad">scam risk {c.evaluation.scam_risk}</span>')
    return f'<div class="chips">{"".join(chips)}</div>' if chips else ""


def card(c: Candidate, photos_dir: Path) -> str:
    e = c.evaluation
    thumb = thumbnail(photos_dir, c.listing.id)
    title = escape((c.detail.title if c.detail and c.detail.title else None) or c.listing.title)
    img = f'<img src="{thumb}" alt="" loading="lazy">' if thumb else '<span class="none">no photo</span>'
    score = f'<span class="score {e.verdict}">{e.score:.1f}</span>' if e else ""
    detail = ""
    if e:
        refs = [r for r in c.references if r.get("type") != "vin_decode"]
        ref_html = ""
        if refs:
            ref_html = '<div class="lbl">Recalls and known issues</div><ul class="refs">' + "".join(
                f"<li>{escape(str(r.get('title')))}{'' if r.get('verified') else ' (unverified)'}"
                + (f' <a href="{escape(r["source"])}" target="_blank" rel="noopener">source</a>' if r.get("source") else "")
                + "</li>"
                for r in refs
            ) + "</ul>"
        detail = (
            "<details><summary>Full evaluation</summary>"
            + _items("Strengths", e.strengths)
            + _items("Concerns", e.concerns)
            + _items("Not established", e.unknowns)
            + _items("Ask the seller", e.seller_questions)
            + _items("From the photos", e.photo_findings)
            + ref_html
            + "</details>"
        )
    head = f'<div class="headline">{escape(e.headline)}</div>' if e else ""
    if not e and c.judge_error:
        head = f'<div class="headline">Not scored: {escape(c.judge_error[:120])}</div>'
    return (
        f'<article class="card"><a class="photo" href="{c.listing.url}" target="_blank" rel="noopener" '
        f'aria-label="{title} on Facebook">{img}{score}<span class="price">{_price(c)}</span></a>'
        f'<div class="body"><a class="title" href="{c.listing.url}" target="_blank" rel="noopener">{title}</a>'
        f'<div class="facts">{_facts(c)}</div>{head}{_chips(c)}{detail}</div></article>'
    )


def _limits(b: Brief) -> str:
    parts = []
    if b.price.min is not None or b.price.max is not None:
        lo = f"${b.price.min:,.0f}" if b.price.min is not None else "$0"
        hi = f"${b.price.max:,.0f}" if b.price.max is not None else "any"
        parts.append(f"{lo}–{hi}")
    if b.location.max_miles:
        parts.append(f"within {b.location.max_miles:.0f} mi of {escape(b.location.home)}")
    if b.vehicle:
        v = b.vehicle
        if v.drivetrain:
            parts.append("/".join(d.upper() for d in v.drivetrain))
        if v.title == "clean":
            parts.append("clean title")
        if v.transmission != "any":
            parts.append(v.transmission.replace("-", " "))
        if v.max_mileage:
            parts.append(f"under {v.max_mileage:,} mi")
    return " · ".join(parts)


def _health(last: dict[str, Any] | None) -> str:
    if not last:
        return '<div class="health">Not run yet.</div>'
    when = time.strftime("%b %-d, %-I:%M %p", time.localtime(last["ts"]))
    failed = last.get("searches_failed") or []
    if not last.get("searches_ok"):
        return f'<div class="health broken">Last run {when}: every search failed. The scraper is broken, not the market quiet.</div>'
    text = (f"Last run {when} · {last.get('seen', 0)} listings seen · {last.get('judged', 0)} newly scored"
            + (f" · {len(failed)} searches failed" if failed else "")
            + (f" · {last['unread']} new listings not read yet (read cap reached)" if last.get("unread") else "")
            + (f" · {last['unscored']} waiting to be scored next run" if last.get("unscored") else ""))
    return f'<div class="health">{text}</div>'


def section(s: Section, photos_dir: Path) -> str:
    b = s.brief
    ranked = rank(s.candidates)
    live = [c for c in ranked if not c.excluded]
    groups = [
        ("Worth contacting", [c for c in live if c.evaluation and c.evaluation.verdict == "top"]),
        ("Worth a question first", [c for c in live if c.evaluation and c.evaluation.verdict == "consider"]),
        ("Just outside the limits", [c for c in ranked if c.excluded and c.facts.get("near_miss")
                                     and not (c.evaluation and c.evaluation.verdict == "skip")]),
        ("Not scored yet", [c for c in live if not c.evaluation]),
    ]
    body = "".join(
        f'<div class="group"><h3>{name} · {len(items)}</h3><div class="grid">'
        + "".join(card(c, photos_dir) for c in items)
        + "</div></div>"
        for name, items in groups
        if items
    )
    skipped = [c for c in live if c.evaluation and c.evaluation.verdict == "skip"]
    excluded = [c for c in ranked if c.excluded and not (c.facts.get("near_miss")
                                                         and not (c.evaluation and c.evaluation.verdict == "skip"))]
    more = ""
    if skipped:
        more += (f'<details class="more"><summary>{len(skipped)} scored and skipped</summary><ul>'
                 + "".join(f'<li><a href="{c.listing.url}" target="_blank" rel="noopener">{escape(c.listing.title)}</a> '
                           f'{_price(c)}: {escape(c.evaluation.headline)}</li>' for c in skipped)
                 + "</ul></details>")
    if excluded:
        more += (f'<details class="more"><summary>{len(excluded)} ruled out by the limits</summary><ul>'
                 + "".join(f'<li><a href="{c.listing.url}" target="_blank" rel="noopener">{escape(c.listing.title)}</a> '
                           f'{_price(c)}: {escape("; ".join(ch.detail for ch in c.failed_checks) or "skipped")}</li>'
                           for c in excluded if c.detail)
                 + "</ul></details>")
    if not body:
        body = '<div class="more">Nothing matches yet.</div>'
    return (
        f'<section class="brief" id="{escape(b.name)}"><div class="brief-head"><h2>{escape(b.title)}</h2>'
        f'<div class="limits">{_limits(b)}</div>{_health(s.last_run)}</div>{body}{more}</section>'
    )


def render(sections: list[Section], photos_dir: Path, title: str = "Marketplace Watch") -> str:
    nav = ""
    if len(sections) > 1:
        nav = "<nav>" + "".join(f'<a href="#{escape(s.brief.name)}">{escape(s.brief.title)}</a>' for s in sections) + "</nav>"
    when = time.strftime("%b %-d, %Y %-I:%M %p")
    # Served as-is by nginx too, so it carries its own charset and viewport.
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{escape(title)}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@700;800&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header class="page"><div class="meta">FACEBOOK MARKETPLACE · UPDATED {escape(when.upper())}</div>
<h1>{escape(title)}</h1>{nav}</header>
{''.join(section(s, photos_dir) for s in sections)}
<footer>Scores are Claude's read of each listing against its brief, using the listing, its photos, NHTSA
recall data and a sourced facts table. Seller claims are the sellers'. Items marked unverified are leads to ask
about, not findings. Get anything with an engine inspected before buying.</footer>
</div>"""
