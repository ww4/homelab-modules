"""The watch page: every brief's listings as cards, with photo, score and price.

One self-contained HTML file. Facebook's photo URLs are signed and expire, and
a published artifact can't load outside images, so each card's photo is a
small JPEG thumbnail embedded as a data: URI.

What the reader can do on it:
- sort (best match, price up/down, newest, make/model) and filter by model
- see new listings at a glance: a colored corner ("dogear") on cards this
  browser hasn't seen, until opened or "mark all seen" (kept in localStorage)
- star listings (interactive=True only): stars live on the server
  (market_agent.web), so everyone sees the same ones, and starred listings
  are always tracked, fully evaluated and shown in their own section

Model-wide facts (recalls, bulletins) are colored tags, not prose: when the
choice is between six Foresters, the tag says "oil consumption" once and the
writing is about this Forester.
"""

from __future__ import annotations

import base64
import io
import re
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from .brief import Brief
from .models import Candidate
from .policy import Effective, effective, rank

THUMB_WIDTH = 520
THUMB_QUALITY = 68
MAX_CARDS = 12  # per group; the rest collapse to a list (sorting/filtering still reach them)


@dataclass
class Section:
    brief: Brief
    candidates: list[Candidate]
    last_run: dict[str, Any] | None
    favorites: set[str] = field(default_factory=set)
    first_seen: dict[str, int] = field(default_factory=dict)


GALLERY_WIDTH = 800


class Photos:
    """Where the page's photos go.

    inline: data: URIs, one self-contained file (heavy: every photo is in the HTML).
    files:  resized JPEGs written to `out_dir` and referenced as "<rel>/<name>",
            so the browser fetches each one only when it is shown. Files the
            page no longer references are removed after a render (sweep()).
    """

    def __init__(self, photos_dir: Path, out_dir: Path | None = None, rel: str = "photos") -> None:
        self.src, self.out, self.rel = photos_dir, out_dir, rel
        self.used: set[str] = set()
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

    def url(self, listing_id: str, n: int, width: int) -> str | None:
        if self.out is None:
            return thumbnail(self.src, listing_id, n, width)
        src = self.src / listing_id / f"photo-{n:02d}.jpg"
        if not src.is_file():
            return None
        name = f"{listing_id}-{n}-{width}.jpg"
        dest = self.out / name
        if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
            data = _resized(src, width)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(data)
            tmp.chmod(0o644)
            tmp.replace(dest)
        self.used.add(name)
        return f"{self.rel}/{name}"

    def count(self, listing_id: str) -> int:
        return len(list((self.src / listing_id).glob("photo-*.jpg")))

    def sweep(self) -> None:
        if self.out is None:
            return
        for f in self.out.glob("*.jpg"):
            if f.name not in self.used:
                f.unlink(missing_ok=True)


def _resized(src: Path, width: int) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return src.read_bytes()
    with Image.open(src) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=THUMB_QUALITY if width <= THUMB_WIDTH else 74, optimize=True, progressive=True)
        return buf.getvalue()


def thumbnail(photos_dir: Path, listing_id: str, n: int = 1, width: int = THUMB_WIDTH) -> str | None:
    """data: URI for one of the listing's photos, or None if we never fetched it."""
    src = photos_dir / listing_id / f"photo-{n:02d}.jpg"
    if not src.is_file():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(_resized(src, width)).decode()


CSS = """
:root{--bg:#f3f5f6;--surface:#fff;--sunk:#e8ecee;--fg:#192126;--muted:#5b6870;--line:#d8dfe3;--accent:#1f5f8b;
--good:#2a7848;--good-bg:#e2f2e8;--warn:#94590a;--warn-bg:#faefda;--bad:#a1352c;--bad-bg:#f7e2df;
--info:#4a5a66;--info-bg:#e6ebef;--new:#2c6fd6;--star:#d4a017;
--display:"Archivo","Helvetica Neue",Arial,sans-serif;--body:"Inter",system-ui,-apple-system,sans-serif;
--mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#111619;--surface:#192025;--sunk:#0d1114;
--fg:#e3e9ec;--muted:#95a3ab;--line:#2a343a;--accent:#7cb8e3;--good:#72c796;--good-bg:#17301f;--warn:#e3b25a;
--warn-bg:#33281a;--bad:#ee8b83;--bad-bg:#3a1f1c;--info:#aab7c0;--info-bg:#232c32;--new:#6fa8ff;--star:#f0c040;
color-scheme:dark}}
:root[data-theme="dark"]{--bg:#111619;--surface:#192025;--sunk:#0d1114;--fg:#e3e9ec;--muted:#95a3ab;--line:#2a343a;
--accent:#7cb8e3;--good:#72c796;--good-bg:#17301f;--warn:#e3b25a;--warn-bg:#33281a;--bad:#ee8b83;--bad-bg:#3a1f1c;
--info:#aab7c0;--info-bg:#232c32;--new:#6fa8ff;--star:#f0c040;color-scheme:dark}
body{background:var(--bg);color:var(--fg);font:15px/1.5 var(--body);margin:0}
.wrap{max-width:1680px;margin:0 auto;padding-block:32px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));
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
.toolbar{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;font-size:13px;color:var(--muted)}
.toolbar label{display:flex;gap:6px;align-items:center}
.toolbar select,.toolbar button{font:inherit;color:var(--fg);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
.toolbar button{cursor:pointer}
.group{display:flex;flex-direction:column;gap:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;min-width:0}
.card.is-new::after{content:"";position:absolute;top:0;right:0;width:0;height:0;border-style:solid;
border-width:0 34px 34px 0;border-color:transparent var(--new) transparent transparent;z-index:2;pointer-events:none}
.card.hidden-by-filter{display:none}
.photo{position:relative;aspect-ratio:4/3;max-width:100%;background:var(--sunk);display:block;overflow:hidden}
.photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.photo .none{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);font-size:13px}
.score{position:absolute;top:10px;left:10px;font:700 15px var(--mono);padding:4px 9px;border-radius:6px;
background:var(--surface);color:var(--fg);box-shadow:0 1px 3px rgba(0,0,0,.25)}
.score.top{background:var(--good);color:var(--surface)}.score.consider{background:var(--warn);color:var(--surface)}
.score.skip,.score.confirm{background:var(--bad);color:var(--surface)}
.price{position:absolute;bottom:10px;right:10px;font:700 16px var(--mono);font-variant-numeric:tabular-nums;
padding:3px 9px;border-radius:6px;background:rgba(15,20,23,.82);color:#fff}
.star{position:absolute;top:8px;right:40px;z-index:3;width:34px;height:34px;border-radius:50%;border:0;cursor:pointer;
background:rgba(15,20,23,.6);color:#fff;font-size:19px;line-height:34px;text-align:center;padding:0}
.star[aria-pressed="true"]{color:var(--star);background:rgba(15,20,23,.82)}
.star:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:7px;flex:1}
.title{font-weight:650;font-size:15px;line-height:1.3;color:var(--fg);text-decoration:none}
.title:hover{color:var(--accent);text-decoration:underline}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.facts{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.headline{font-size:13.5px}
.standout{margin:0;padding-left:18px;font-size:13px}.standout li{margin:1px 0}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-size:11.5px;padding:2px 7px;border-radius:4px;background:var(--warn-bg);color:var(--warn);text-decoration:none}
.chip.bad,.chip.recall{background:var(--bad-bg);color:var(--bad)}.chip.near,.chip.pattern{background:var(--info-bg);color:var(--info)}
.chip.bulletin{background:var(--warn-bg);color:var(--warn)}.chip.sold{background:var(--fg);color:var(--bg)}
details{font-size:13px;margin-top:auto}
details summary{cursor:pointer;color:var(--accent);padding-top:4px}
details ul{margin:4px 0 6px;padding-left:18px}details li{margin:2px 0}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-top:8px}
.more{font-size:13px;color:var(--muted)}
.more ul{padding-left:18px;margin:6px 0}.more a{color:var(--accent)}
.health{font-size:12.5px;color:var(--muted)}.health.broken{color:var(--bad);font-weight:600}
.legend{font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center}
footer{color:var(--muted);font-size:12.5px;max-width:80ch}
.card{cursor:pointer}
.card .full{display:none}
.open,.close,.copy{font:inherit;font-size:13px;color:var(--accent);background:none;border:0;padding:2px 0;cursor:pointer;text-align:left}
.open::after{content:" ▸"}
.close{border:1px solid var(--line);border-radius:6px;padding:4px 12px;color:var(--fg);background:var(--surface)}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:4px}
.fb{font-size:13px;font-weight:600;color:var(--surface);background:var(--accent);border-radius:6px;padding:5px 12px;text-decoration:none}
.fb:hover{filter:brightness(1.1)}
.photo{cursor:pointer}.card.expanded .photo{cursor:default}
.photo:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.copy{font-size:12px;border:1px solid var(--line);border-radius:5px;padding:1px 8px;margin-left:8px;color:var(--fg);
text-transform:none;letter-spacing:0;font-weight:500}
.card.expanded{grid-column:1/-1;display:flex;flex-direction:row;height:500px;cursor:default;
border-color:var(--accent);box-shadow:0 4px 18px rgba(0,0,0,.08)}
.card.expanded .media{flex:0 0 320px;display:flex;flex-direction:column;gap:6px;overflow:hidden}
.media .spec{display:none}
.card.expanded .media .spec{display:grid;margin:0 6px 6px;padding:8px 10px;gap:4px 12px}
.card.expanded .media .spec dd{font-size:12.5px}
.card.expanded .photo{border-radius:0}
.strip{display:none}
.card.expanded .strip{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:4px;padding:0 6px 6px}
.card.expanded .media .photo img{object-fit:cover}
.strip .shot{display:block;aspect-ratio:4/3;overflow:hidden;border-radius:5px;background:var(--sunk);padding:0;
border:2px solid transparent;cursor:pointer}
.strip .shot.active{border-color:var(--accent)}
.strip .shot:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.strip img{width:100%;height:100%;object-fit:cover;display:block}
/* The evaluation flows down a fixed height and then across: width, not height. */
.card.expanded .body{flex:1;min-width:0;display:block;height:100%;box-sizing:border-box;padding:16px 20px 54px;
column-width:230px;column-gap:26px;column-fill:auto;column-rule:1px solid var(--line);overflow-x:auto;overflow-y:hidden;
scrollbar-width:none;position:relative}
.card.expanded .body::-webkit-scrollbar{display:none}
.card.expanded.has-more .body{-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 48px),transparent);
mask-image:linear-gradient(to right,#000 calc(100% - 48px),transparent)}
.card.expanded .body > *{margin:0 0 10px}
.card.expanded .title{display:block;font-size:17px;font-family:var(--display);break-after:avoid}
.card.expanded .headline{font-size:14px}
.card.expanded .standout,.card.expanded .open,.card.expanded .facts{display:none}
.card.expanded > .body > .chips{break-inside:avoid}
.card.expanded .full{display:block}
.card.expanded .full > *,.card.expanded .panels > *{margin:0 0 12px}
.card.expanded .panels{display:block}
.spec{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 14px;margin:0;padding:10px 12px;
background:var(--sunk);border-radius:8px;break-inside:avoid}
.spec div{min-width:0}.spec dt{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.spec dd{margin:0;font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.capnote{margin:0;font-size:12.5px;color:var(--muted)}
.panel{min-width:0}.panel h4{margin:0 0 6px;font:700 11.5px var(--body);text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);display:flex;align-items:center;break-after:avoid}
.panel ul,.panel ol{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:5px;font-size:12.5px}
.panel li{position:relative;padding-left:20px;break-inside:avoid}
.panel ul li::before{position:absolute;left:0;top:0;width:15px;text-align:center;font-weight:700}
.panel.good li::before{content:"✓";color:var(--good)}
.panel.bad li::before{content:"!";color:var(--warn)}
.panel.unknown li::before{content:"?";color:var(--muted)}
.panel.photos li::before{content:"◉";color:var(--accent);font-size:10px;top:2px}
.panel.ask ol{counter-reset:q}.panel.ask li{counter-increment:q}
.panel.ask li::before{content:counter(q);position:absolute;left:0;top:1px;width:15px;height:15px;border-radius:50%;
background:var(--accent);color:var(--surface);font-size:10px;font-weight:700;text-align:center;line-height:15px}
.panel.tags li{padding-left:0}.panel.tags em{color:var(--muted)}
.panel.tags a{color:var(--accent)}
.actions{break-inside:avoid}
.cardbar{display:none}
.card.expanded > .cardbar{display:flex;position:absolute;right:12px;bottom:10px;gap:8px;align-items:center;z-index:4}
.pager{display:flex;gap:6px}.pager[hidden]{display:none}
.grow{font:inherit;font-size:13px;font-weight:600;color:var(--accent);background:var(--surface);border:1px solid var(--line);
border-radius:999px;padding:4px 12px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12)}
/* Full height: everything shown, columns balanced across the width, nothing to page through. */
.card.expanded.tall{height:auto;padding-bottom:44px}
.card.expanded.tall .body{height:auto;column-fill:balance;overflow:visible;-webkit-mask-image:none;mask-image:none}
.card.expanded.tall .pager{display:none}
.card.expanded:not(.tall) .panel.more{display:none}
.card.expanded.tall .panel.ask-top{display:none}
.pager button{width:30px;height:30px;border-radius:50%;border:1px solid var(--line);background:var(--surface);
color:var(--fg);cursor:pointer;font-size:14px;box-shadow:0 1px 3px rgba(0,0,0,.15)}
@media (max-width:720px){.card.expanded{flex-direction:column;height:auto}
.card.expanded .media{flex:none}.card.expanded .body{height:auto;column-width:auto;columns:1;overflow:visible}
.card.expanded > .cardbar{position:static;padding:0 14px 14px}}
@media (prefers-reduced-motion:no-preference){.card{transition:border-color .15s}.card:hover{border-color:var(--accent)}}
"""


def _price_value(c: Candidate) -> float | None:
    return c.detail.price if c.detail and c.detail.price is not None else c.listing.price


def _price(c: Candidate) -> str:
    p = _price_value(c)
    return f"${p:,.0f}" if p is not None else escape(c.listing.price_text)


def make_model(c: Candidate) -> str:
    """A grouping key: 'Honda CR-V', else the two words after the year in the title."""
    f = c.facts
    if f.get("make") and f.get("model"):
        return f"{f['make']} {f['model']}"
    title = re.sub(r"\s*·.*$", "", (c.detail.title if c.detail and c.detail.title else None) or c.listing.title)
    words = [w for w in re.sub(r"\b(19|20)\d\d\b", "", title).split() if w]
    return " ".join(w.capitalize() if w.islower() else w for w in words[:2]) or "Other"


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


def _ref_chips(c: Candidate) -> str:
    """Model-wide facts as colored tags: recall (red), bulletin (amber), unverified pattern (grey)."""
    chips: list[str] = []
    table_titles = " ".join(str(r.get("title", "")) for r in c.references if r.get("type") != "nhtsa_recall")
    nhtsa = []
    for r in c.references:
        kind = r.get("type")
        if kind == "nhtsa_recall":
            camp = str(r.get("title", "")).split(":")[0].replace("Recall ", "")
            if camp[:6].replace("V", "V-") in table_titles or camp[:6] in table_titles.replace("-", ""):
                continue  # already a named chip from the facts table
            nhtsa.append(r)
            continue
        if kind not in ("recall", "bulletin", "pattern"):
            continue
        label = r.get("chip") or str(r.get("title", "")).split("(")[0].strip()
        if not r.get("verified"):
            label += "?"
        tip = f"{r.get('title')}. {r.get('detail', '')} Ask: {r.get('ask', '')}"
        href = r.get("source")
        tag = "a" if href else "span"
        attrs = f' href="{escape(href)}" target="_blank" rel="noopener"' if href else ""
        chips.append(f'<{tag} class="chip {kind}" title="{escape(tip)}"{attrs}>{escape(label)}</{tag}>')
    if nhtsa:
        tip = "; ".join(str(r.get("title", "")).split(": ", 1)[-1] for r in nhtsa)
        chips.append(f'<span class="chip pattern" title="{escape(tip)}">{len(nhtsa)} NHTSA recalls (check VIN)</span>')
    return "".join(chips)


def _status_chips(c: Candidate, eff: Effective | None) -> str:
    chips = []
    if c.detail and (c.detail.is_sold or c.detail.is_pending):
        chips.append(f'<span class="chip sold">{"sold" if c.detail.is_sold else "pending"}</span>')
    if c.facts.get("near_miss"):
        chips.append(f'<span class="chip near">{escape(c.facts["near_miss"])}</span>')
    for ch in c.checks:
        if ch.status == "unknown":
            chips.append(f'<span class="chip">{escape(ch.name)} unknown</span>')
    for s in c.signals:
        if s.severity == "warning" or s.name == "dealer or flipper":
            chips.append(f'<span class="chip bad">{escape(s.name)}</span>')
    if eff and eff.caps:
        chips.append(f'<span class="chip bad" title="judge gave {c.evaluation.score:.1f}">capped: {escape(", ".join(eff.caps))}</span>')
    return "".join(chips)


def card(c: Candidate, photos: "Photos | Path", brief: Brief, *, starred: bool, interactive: bool,
         first_seen: int | None) -> str:
    if isinstance(photos, Path):
        photos = Photos(photos)
    e = c.evaluation
    eff = effective(c, brief)
    thumb = photos.url(c.listing.id, 1, THUMB_WIDTH)
    title = escape((c.detail.title if c.detail and c.detail.title else None) or c.listing.title)
    img = f'<img src="{thumb}" alt="" loading="lazy">' if thumb else '<span class="none">no photo</span>'
    score = f'<span class="score {eff.verdict}">{eff.score:.1f}</span>' if eff else ""
    star = ""
    if interactive:
        star = (f'<button class="star" type="button" aria-pressed="{str(starred).lower()}" '
                f'aria-label="Star this listing" data-brief="{escape(brief.name)}" data-id="{escape(c.listing.id)}">'
                f'{"★" if starred else "☆"}</button>')
    words = ""
    if e:
        words = f'<div class="headline">{escape(e.headline)}</div>'
        if e.standout:
            words += '<ul class="standout">' + "".join(f"<li>{escape(x)}</li>" for x in e.standout) + "</ul>"
    elif c.judge_error:
        words = f'<div class="headline">Not scored: {escape(c.judge_error[:120])}</div>'
    opener = ""
    if e:
        label = "Full evaluation" if e.depth == "full" else "Details"
        opener = f'<button class="open" type="button" aria-expanded="false">{label}</button>'
    chips = _status_chips(c, eff) + _ref_chips(c)
    price = _price_value(c)
    hidden_extra = bool(e and e.depth == "full" and (e.unknowns or len(e.seller_questions) > 1 or e.photo_findings))
    data = (f' data-id="{escape(c.listing.id)}" data-score="{eff.score if eff else -1:.2f}"'
            f'{" data-more=1" if hidden_extra else ""}'
            f' data-price="{price if price is not None else ""}" data-model="{escape(make_model(c))}"'
            f' data-first-seen="{first_seen or 0}"')
    return (
        f'<article class="card"{data}>{star}<div class="media"><div class="photo" role="button" tabindex="0" '
        f'aria-label="Open {title}">{img}{score}<span class="price">{_price(c)}</span></div>'
        f'{_strip(c, photos)}{_spec(c, eff) if e else ""}</div>'
        f'<div class="body"><a class="title" href="{c.listing.url}" target="_blank" rel="noopener">{title}</a>'
        f'<div class="facts">{_facts(c)}</div>{words}'
        + (f'<div class="chips">{chips}</div>' if chips else "")
        + opener
        + (_full(c, eff, interactive) if e else "")
        + "</div></article>"
    )


STRIP_PHOTOS = 6  # gallery: up to this many of the listing's photos, one row
STRIP_WIDTH = 240


def _strip(c: Candidate, photos: "Photos") -> str:
    """A gallery for full reads: the listing's photos, click one to show it in the big frame."""
    if not (c.evaluation and c.evaluation.depth == "full"):
        return ""
    shots = []
    for n in range(1, STRIP_PHOTOS + 1):
        small = photos.url(c.listing.id, n, STRIP_WIDTH)
        if not small:
            break
        full = photos.url(c.listing.id, n, GALLERY_WIDTH)
        shots.append(f'<button type="button" class="shot{" active" if n == 1 else ""}" data-full="{full}" '
                     f'aria-label="Photo {n}"><img src="{small}" alt="" loading="lazy"></button>')
    if len(shots) < 2:
        return ""
    return '<div class="strip">' + "".join(shots) + "</div>"


def _spec(c: Candidate, eff: Effective | None) -> str:
    """The facts a buyer compares across cars, in one strip."""
    f, d, e = c.facts, c.detail, c.evaluation
    rows: list[tuple[str, str]] = [("Price", _price(c))]
    if f.get("year"):
        rows.append(("Year", str(f["year"])))
    if f.get("mileage"):
        src = " (seller's text)" if f.get("mileage_source") == "description" else ""
        rows.append(("Miles", f"{f['mileage']:,}{src}"))
    if f.get("drivetrain"):
        rows.append(("Drive", str(f["drivetrain"]).upper()))
    trans = f.get("transmission_spec") or f.get("transmission")
    if trans:
        rows.append(("Transmission", re.sub(r"\s*\(.*?\)", "", str(trans)).strip()))
    rows.append(("Title", str(f.get("title_status") or "not stated")))
    for k, v in f.items():
        if k.startswith("miles_from_") and v is not None:
            rows.append((f"To {k.removeprefix('miles_from_')}", f"{v} mi"))
    if d and d.created_at:
        days = int((time.time() - d.created_at) / 86400)
        rows.append(("Listed", "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"))
    if e:
        rows.append(("Seller", {"private": "private", "dealer_or_flipper": "dealer / flipper",
                                "unclear": "unclear"}[e.seller_kind]))
    return '<dl class="spec">' + "".join(f"<div><dt>{k}</dt><dd>{escape(v)}</dd></div>" for k, v in rows) + "</dl>"


def _panel(kind: str, label: str, items: list[str], ordered: bool = False) -> str:
    if not items:
        return ""
    tag = "ol" if ordered else "ul"
    return (f'<section class="panel {kind}"><h4>{escape(label)}</h4><{tag}>'
            + "".join(f"<li>{escape(i)}</li>" for i in items) + f"</{tag}></section>")


def _tags_explained(c: Candidate) -> str:
    rows = []
    for r in c.references:
        kind = r.get("type")
        if kind not in ("recall", "bulletin", "pattern"):
            continue
        label = r.get("chip") or str(r.get("title", "")).split("(")[0].strip()
        src = r.get("source")
        link = f' <a href="{escape(src)}" target="_blank" rel="noopener">source</a>' if src else ""
        rows.append(f'<li><span class="chip {kind}">{escape(label)}{"" if r.get("verified") else "?"}</span> '
                    f'{escape(str(r.get("detail", "")))} <em>Ask: {escape(str(r.get("ask", "")))}</em>{link}</li>')
    nhtsa = [r for r in c.references if r.get("type") == "nhtsa_recall"]
    if nhtsa:
        rows.append(f'<li><span class="chip pattern">{len(nhtsa)} NHTSA recalls</span> '
                    + escape("; ".join(str(r.get("title", "")).replace("Recall ", "") for r in nhtsa[:8]))
                    + (" …" if len(nhtsa) > 8 else "") + " <em>Ask for the VIN to see which are still open.</em></li>")
    if not rows:
        return ""
    return f'<section class="panel tags"><h4>Model tags</h4><ul>{"".join(rows)}</ul></section>'


def _full(c: Candidate, eff: Effective | None, interactive: bool) -> str:
    e = c.evaluation
    assert e is not None
    caps = ""
    if eff and eff.caps:
        caps = (f'<p class="capnote">The judge scored this {e.score:.1f}; the page caps it at {eff.score:.1f} '
                f'for: {escape(", ".join(eff.caps))}.</p>')
    if e.depth == "full":
        questions = e.seller_questions
        copy = ""
        if questions:
            text = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            copy = (f'<button class="copy" type="button" data-copy="{escape(text)}">Copy questions</button>')
        # Wide view: strengths, concerns and the one question to ask first.
        # Full view ("tall"): everything, including all questions with Copy.
        panels = (
            _panel("good", "Strengths", e.strengths)
            + _panel("bad", "Concerns", e.concerns)
            + (f'<section class="panel ask ask-top"><h4>Ask first</h4><ol><li>{escape(questions[0])}</li></ol></section>'
               if questions else "")
            + _panel("unknown more", "Not established", e.unknowns)
            + (f'<section class="panel ask more"><h4>Ask the seller {copy}</h4><ol>'
               + "".join(f"<li>{escape(q)}</li>" for q in questions) + "</ol></section>" if questions else "")
            + _panel("photos more", "From the photos", e.photo_findings)
        )
    else:
        star_hint = " Star it to get the full read." if interactive else ""
        panels = (_panel("good", "Quick screen", e.standout)
                  + f'<p class="capnote">This listing only had the quick screen; the full read goes to contenders.{star_hint}</p>')
    fb = (f'<a class="fb" href="{c.listing.url}" target="_blank" rel="noopener">View on Facebook ↗</a>')
    return (f'<div class="full">{caps}<div class="panels">{panels}</div>'
            f'<div class="actions">{fb}<button class="close" type="button">Close</button></div>'
            '<div class="cardbar">'
            + ('<button class="grow" type="button" aria-expanded="false">Full evaluation ▾</button>' if e.depth == "full" else "")
            + '<span class="pager" hidden><button type="button" data-page="-1" aria-label="Previous columns">◂</button>'
            '<button type="button" data-page="1" aria-label="More columns">▸</button></span></div></div>')


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


def _line(c: Candidate, why: str) -> str:
    return (f'<li><a href="{c.listing.url}" target="_blank" rel="noopener">{escape(c.listing.title)}</a> '
            f'{_price(c)}: {escape(why)}</li>')


def _collapsed(summary: str, rows: list[str]) -> str:
    if not rows:
        return ""
    return f'<details class="more"><summary>{escape(summary)}</summary><ul>{"".join(rows)}</ul></details>'


def section(s: Section, photos: "Photos", *, interactive: bool) -> str:
    b = s.brief
    ranked = rank(s.candidates, b)
    eff = {c.listing.id: effective(c, b) for c in ranked}

    def v(c: Candidate) -> str | None:
        x = eff[c.listing.id]
        return x.verdict if x else None

    def cards(items: list[Candidate]) -> str:
        return "".join(
            card(c, photos, b, starred=c.listing.id in s.favorites, interactive=interactive,
                 first_seen=s.first_seen.get(c.listing.id))
            for c in items
        )

    starred = [c for c in ranked if c.listing.id in s.favorites]
    live = [c for c in ranked if not c.excluded and c.listing.id not in s.favorites]
    near = [c for c in ranked if c.excluded and c.facts.get("near_miss") and c.listing.id not in s.favorites]
    groups = [
        ("Starred", starred, False),
        ("Worth contacting", [c for c in live if v(c) == "top"], True),
        ("Worth a question first", [c for c in live if v(c) == "consider"], True),
        ("Just outside the limits", [c for c in near if v(c) in ("top", "consider")], True),
    ]
    body = ""
    for name, items, cap in groups:
        if not items and name != "Starred":
            continue
        if name == "Starred" and not interactive and not items:
            continue
        shown = items[:MAX_CARDS] if cap else items
        rest = items[MAX_CARDS:] if cap else []
        empty = ('<p class="more" data-empty>Star a listing to keep it here. Starred listings are tracked until they '
                 'sell and always get the full evaluation.</p>' if name == "Starred" and not items else "")
        body += (f'<div class="group" data-group="{escape(name)}"><h3>{name} · <span class="count">{len(items)}</span></h3>'
                 f'{empty}<div class="grid">{cards(shown)}</div>'
                 + _collapsed(f"{len(rest)} more", [_line(c, f"{eff[c.listing.id].score:.1f}: {c.evaluation.headline}")
                                                   for c in rest if c.evaluation])
                 + "</div>")

    confirm = [c for c in live if v(c) == "confirm" and eff[c.listing.id].score >= 5.0]
    more = _collapsed(f"{len(confirm)} worth a look once one thing is confirmed",
                      [_line(c, "; ".join(eff[c.listing.id].confirm)) for c in confirm])
    unscored = [c for c in live if v(c) is None]
    more += _collapsed(f"{len(unscored)} not scored yet (next run)", [_line(c, c.judge_error or "waiting") for c in unscored])
    skipped = [c for c in live + near if v(c) == "skip" or (v(c) == "confirm" and c not in confirm)]
    more += _collapsed(f"{len(skipped)} scored and skipped",
                       [_line(c, f"{eff[c.listing.id].score:.1f}: {c.evaluation.headline}") for c in skipped])
    out = [c for c in ranked if c.excluded and not c.facts.get("near_miss") and c.detail and c.listing.id not in s.favorites]
    more += _collapsed(f"{len(out)} ruled out by the limits",
                       [_line(c, "; ".join(ch.detail for ch in c.failed_checks)) for c in out])

    models = sorted({make_model(c) for _, items, _ in groups for c in items})
    toolbar = (
        '<div class="toolbar">'
        f'<label>Sort <select data-sort><option value="score">Best match</option><option value="price-asc">Price: low to high</option>'
        '<option value="price-desc">Price: high to low</option><option value="new">Newest first</option>'
        '<option value="model">Make / model</option></select></label>'
        '<label>Show <select data-filter><option value="">All models</option>'
        + "".join(f'<option value="{escape(m)}">{escape(m)}</option>' for m in models)
        + '</select></label><button type="button" data-mark-seen>Mark all seen</button></div>'
    )
    legend = ('<div class="legend"><span class="chip recall">recall</span><span class="chip bulletin">service bulletin</span>'
              '<span class="chip pattern">reported pattern?</span><span>tags apply to every listing of that model; '
              'hover for the question to ask</span></div>')
    return (
        f'<section class="brief" id="{escape(b.name)}" data-brief="{escape(b.name)}"><div class="brief-head">'
        f'<h2>{escape(b.title)}</h2><div class="limits">{_limits(b)}</div>{_health(s.last_run)}</div>'
        f'{toolbar}{legend}{body or "<div class=more>Nothing matches yet.</div>"}{more}</section>'
    )


SCRIPT = """
(() => {
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };
  const DAY = 86400;
  const now = Math.floor(Date.now() / 1000);
  const seen = new Set(store.get("ma-seen", []));
  const firstVisit = !store.get("ma-visited", false);

  // Dogears: cards this browser hasn't seen. On a first visit only the last day's are new.
  function markNew() {
    document.querySelectorAll(".card").forEach(c => {
      const id = c.dataset.id, fs = +c.dataset.firstSeen || 0;
      if (firstVisit && now - fs > DAY) seen.add(id);
      c.classList.toggle("is-new", !seen.has(id));
    });
    store.set("ma-seen", [...seen]);
    store.set("ma-visited", true);
  }
  function seeCard(c) { seen.add(c.dataset.id); c.classList.remove("is-new"); store.set("ma-seen", [...seen]); }
  document.addEventListener("click", e => {
    const c = e.target.closest(".card");
    if (c && e.target.closest("a, summary")) seeCard(c);
  });

  function apply(section) {
    section.querySelectorAll(".card.expanded").forEach(c => { c.classList.remove("expanded", "tall", "has-more"); settle(c); });
    const sort = section.querySelector("[data-sort]").value;
    const only = section.querySelector("[data-filter]").value;
    section.querySelectorAll(".grid").forEach(grid => {
      const cards = [...grid.children];
      const num = (c, k, d) => { const v = parseFloat(c.dataset[k]); return isNaN(v) ? d : v; };
      const by = {
        "score": (a, b) => num(b, "score", -1) - num(a, "score", -1),
        "price-asc": (a, b) => num(a, "price", 1e12) - num(b, "price", 1e12),
        "price-desc": (a, b) => num(b, "price", -1) - num(a, "price", -1),
        "new": (a, b) => num(b, "firstSeen", 0) - num(a, "firstSeen", 0),
        "model": (a, b) => a.dataset.model.localeCompare(b.dataset.model) || num(b, "score", -1) - num(a, "score", -1),
      }[sort];
      cards.sort(by).forEach(c => grid.appendChild(c));
      cards.forEach(c => c.classList.toggle("hidden-by-filter", !!only && c.dataset.model !== only));
    });
  }
  document.querySelectorAll("section.brief").forEach(sec => {
    sec.querySelectorAll("[data-sort],[data-filter]").forEach(el => el.addEventListener("change", () => apply(sec)));
    sec.querySelector("[data-mark-seen]").addEventListener("click", () => {
      sec.querySelectorAll(".card").forEach(seeCard);
    });
  });
  markNew();

  // Expand a card across the row: click anywhere on it except a link, star or control.
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  // An opened card spans the row, so it moves to the start of its row (between that row
  // and the one above) instead of leaving its row-mates stranded on a half-empty line.
  // It goes back to its own place when closed, so sorting is undisturbed.
  function lift(card) {
    if (card._home) return;
    const grid = card.parentElement;
    card._home = {grid, next: card.nextElementSibling};
    const top = card.offsetTop;
    const rowStart = [...grid.children].find(c => !c.classList.contains("hidden-by-filter") && c.offsetTop === top);
    if (rowStart && rowStart !== card) grid.insertBefore(card, rowStart);
  }
  function settle(card) {
    const home = card._home;
    if (!home) return;
    card._home = null;
    if (home.next && home.next.parentElement === home.grid) home.grid.insertBefore(card, home.next);
    else home.grid.appendChild(card);
  }
  function collapseAll(except) {
    document.querySelectorAll(".card.expanded").forEach(c => {
      if (c === except) return;
      c.classList.remove("expanded", "tall", "has-more");
      settle(c);
      const img = c.querySelector(".photo img");
      if (img && img.dataset.small) img.src = img.dataset.small;
      const o = c.querySelector(".open"); if (o) o.setAttribute("aria-expanded", "false");
    });
  }
  function setMode(card, mode) {
    // mode: null (small), "wide" (fixed height, columns across), "tall" (everything, full height)
    if (!card.querySelector(".full")) return;
    collapseAll(mode ? card : null);
    if (mode && !card.classList.contains("expanded")) {
      lift(card);
      const img = card.querySelector(".photo img"), first = card.querySelector(".strip .shot");
      if (img && first) { img.dataset.small = img.dataset.small || img.src; img.src = first.dataset.full; }
    }
    card.classList.toggle("expanded", !!mode);
    if (!mode) {
      settle(card);
      const img = card.querySelector(".photo img");
      if (img && img.dataset.small) img.src = img.dataset.small;
      card.querySelectorAll(".strip .shot").forEach((b, i) => b.classList.toggle("active", i === 0));
    }
    card.classList.toggle("tall", mode === "tall");
    const o = card.querySelector(".open"); if (o) o.setAttribute("aria-expanded", String(!!mode));
    const bar = card.querySelector(".cardbar");
    if (bar && bar.parentElement !== card) card.appendChild(bar);
    const grow = card.querySelector(".grow");
    if (grow) { grow.textContent = mode === "tall" ? "Show less ▴" : "Full evaluation ▾";
                grow.setAttribute("aria-expanded", String(mode === "tall")); }
    seeCard(card);
    if (!mode) return;
    card.scrollIntoView({block: mode === "tall" ? "start" : "nearest", behavior: reduce ? "auto" : "smooth"});
    const body = card.querySelector(".body"), pager = card.querySelector(".pager");
    const update = () => {
      const tall = card.classList.contains("tall");
      const overflow = !tall && body.scrollWidth > body.clientWidth + 8;
      card.classList.toggle("has-more", overflow && body.scrollWidth - body.clientWidth - body.scrollLeft > 8);
      if (pager) pager.hidden = !overflow || getComputedStyle(body).columnWidth === "auto";
      // "Full evaluation" only when the wide view is hiding something; tall keeps it as "Show less".
      if (grow) grow.hidden = !tall && !overflow && !card.dataset.more;
    };
    body.scrollLeft = 0;
    body.onscroll = update;
    requestAnimationFrame(update);
  }
  const modeOf = card => !card.classList.contains("expanded") ? null : card.classList.contains("tall") ? "tall" : "wide";

  document.addEventListener("click", async e => {
    const copy = e.target.closest(".copy");
    if (copy) {
      try { await navigator.clipboard.writeText(copy.dataset.copy); copy.textContent = "Copied"; }
      catch { const r = document.createRange(); r.selectNodeContents(copy.closest(".panel").querySelector("ol"));
              const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); copy.textContent = "Selected - copy it"; }
      setTimeout(() => { copy.textContent = "Copy questions"; }, 2000);
      return;
    }
    const shot = e.target.closest(".strip .shot");
    if (shot) {
      const card = shot.closest(".card");
      card.querySelector(".photo img").src = shot.dataset.full;
      card.querySelectorAll(".strip .shot").forEach(b => b.classList.toggle("active", b === shot));
      return;
    }
    const card = e.target.closest(".card");
    if (!card) return;
    if (e.target.closest(".open")) { setMode(card, "tall"); return; }       // small card's link: straight to everything
    if (e.target.closest(".close")) { setMode(card, null); return; }
    if (e.target.closest(".grow")) { setMode(card, modeOf(card) === "tall" ? "wide" : "tall"); return; }
    const pg = e.target.closest("[data-page]");
    if (pg) {
      const body = card.querySelector(".body");
      body.scrollBy({left: +pg.dataset.page * Math.max(240, body.clientWidth - 60), behavior: reduce ? "auto" : "smooth"});
      return;
    }
    if (e.target.closest("a, button, select, input, .full")) return;
    // An open card's photo is for looking at, not a close button.
    if (card.classList.contains("expanded") && e.target.closest(".media")) return;
    setMode(card, modeOf(card) ? null : "wide");
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { document.querySelectorAll(".card.expanded").forEach(c => setMode(c, null)); return; }
    const photo = e.target.closest && e.target.closest(".photo");
    if (photo && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      const card = photo.closest(".card");
      if (!card.classList.contains("expanded")) setMode(card, "wide");
    }
  });

  // Stars: shared on the server. A card starred here is copied into Starred at once;
  // the server re-renders the page so a reload agrees.
  async function setStar(btn, on) {
    const r = await fetch("api/favorites", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({brief: btn.dataset.brief, id: btn.dataset.id, starred: on}),
    });
    if (!r.ok) throw new Error(await r.text());
  }
  document.addEventListener("click", async e => {
    const btn = e.target.closest(".star");
    if (!btn) return;
    const on = btn.getAttribute("aria-pressed") !== "true";
    btn.disabled = true;
    try {
      await setStar(btn, on);
      const sec = btn.closest("section.brief");
      const id = btn.dataset.id;
      sec.querySelectorAll(`.star[data-id="${id}"]`).forEach(b => {
        b.setAttribute("aria-pressed", String(on)); b.textContent = on ? "\\u2605" : "\\u2606";
      });
      const group = sec.querySelector('[data-group="Starred"]');
      const grid = group.querySelector(".grid");
      if (on && !grid.querySelector(`.card[data-id="${id}"]`)) {
        grid.appendChild(btn.closest(".card").cloneNode(true));
      } else if (!on) {
        grid.querySelectorAll(`.card[data-id="${id}"]`).forEach(c => c.remove());
      }
      group.querySelector(".count").textContent = grid.children.length;
      const empty = group.querySelector("[data-empty]");
      if (empty) empty.hidden = grid.children.length > 0;
    } catch (err) {
      btn.title = "Could not save the star: " + err.message;
    } finally {
      btn.disabled = false;
    }
  });
})();
"""


def render(sections: list[Section], photos_dir: Path, title: str = "Marketplace Watch", *,
           interactive: bool = False, photo_files: Path | None = None) -> str:
    """interactive=True adds star buttons, which need market_agent.web behind /api/.

    photo_files: write photos as files there (referenced as "photos/...") instead of
    embedding them; the page then needs that directory published next to it.
    """
    photos = Photos(photos_dir, photo_files)
    nav = ""
    if len(sections) > 1:
        nav = "<nav>" + "".join(f'<a href="#{escape(s.brief.name)}">{escape(s.brief.title)}</a>' for s in sections) + "</nav>"
    when = time.strftime("%b %-d, %Y %-I:%M %p")
    body = "".join(section(s, photos, interactive=interactive) for s in sections)
    photos.sweep()
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
{body}
<footer>Scores are Claude's read of each listing against its brief: every listing gets a quick screen, and
the contenders get the full evaluation. Colored tags are model-wide facts (recalls, bulletins); a "?" tag is a
reported pattern, not a verified one. Seller claims are the sellers'. Get anything with an engine inspected
before buying.</footer>
</div>
<script>{SCRIPT}</script>"""


def sections_from_store(store: Any, briefs_list: list[Brief]) -> list[Section]:
    return [
        Section(b, store.all(b.name), store.last_run(b.name), store.favorites(b.name), store.first_seen(b.name))
        for b in briefs_list
    ]


__all__ = ["Section", "render", "sections_from_store", "make_model"]
