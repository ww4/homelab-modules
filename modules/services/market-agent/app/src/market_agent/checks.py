"""Hard limits and signals that apply to every kind of brief."""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict

from .brief import Brief
from .models import Candidate, Check, Signal

DEALER_PATTERNS = [
    (re.compile(r"\bfinancing\b|\bfinance available\b|\bbuy here,? pay here\b|\bbhph\b", re.I), "offers financing"),
    (re.compile(r"\bse habla espa[ñn]ol\b|\bhablamos espa[ñn]ol\b", re.I), "dealer-style 'se habla español' line"),
    (re.compile(r"\bdown payment\b|\$\s?\d[\d,]*\s*down\b", re.I), "quotes a down payment"),
    (re.compile(r"\bstock\s*#|\bdealership\b|\bdealer fee|\bdoc fee", re.I), "names a dealership or stock number"),
    (re.compile(r"\bfresh trade\b|\btrade-?in\b special", re.I), "describes itself as a trade-in"),
]
CHECKMARK = re.compile(r"[✅✔☑]")


def price_check(brief: Brief, price: float | None) -> Check:
    lo, hi = brief.price.min, brief.price.max
    if price is None:
        return Check(name="price", status="unknown", detail="no price listed")
    if lo is not None and price < lo:
        return Check(name="price", status="fail", detail=f"${price:,.0f} is below the ${lo:,.0f} floor (often bait)")
    if hi is not None and price > hi:
        return Check(name="price", status="fail", detail=f"${price:,.0f} is over the ${hi:,.0f} limit")
    return Check(name="price", status="pass", detail=f"${price:,.0f}")


def distance_check(brief: Brief, miles: float | None, method: str, measured_from: str) -> Check:
    limit = brief.location.max_miles
    if miles is None:
        return Check(name="distance", status="unknown", detail="listing location unknown")
    how = {"radius": "", "osrm": " driving", "estimate": " est. driving"}.get(method, "")
    text = f"{miles:.0f} mi{how} from {measured_from}"
    if limit is not None and miles > limit:
        return Check(name="distance", status="fail", detail=f"{text}, over the {limit:.0f} mi limit")
    return Check(name="distance", status="pass", detail=text)


def availability_check(sold: bool, pending: bool) -> Check | None:
    if sold:
        return Check(name="available", status="fail", detail="marked sold")
    if pending:
        return Check(name="available", status="fail", detail="marked pending")
    return None


def exclude_words_check(brief: Brief, text: str) -> Check | None:
    low = text.lower()
    hit = next((w for w in brief.search.exclude_words if w.lower() in low), None)
    if hit:
        return Check(name="exclude words", status="fail", detail=f"listing says {hit!r}")
    return None


def dealer_signals(text: str) -> list[Signal]:
    reasons = [label for pattern, label in DEALER_PATTERNS if pattern.search(text)]
    checks = len(CHECKMARK.findall(text))
    if checks >= 4:
        reasons.append(f"{checks} checkmark bullets (lot-style template)")
    if not reasons:
        return []
    return [Signal(name="dealer or flipper", detail="; ".join(reasons), severity="caution")]


def age_signal(created_at: int | None, now: float | None = None) -> Signal | None:
    if not created_at:
        return None
    days = int(((now or time.time()) - created_at) / 86400)
    if days >= 60:
        return Signal(name="listing age", detail=f"listed {days} days ago - ask why it hasn't sold", severity="info")
    return None


def _fingerprint(text: str) -> str | None:
    words = re.findall(r"[a-z]{3,}", text.lower())
    if len(words) < 12:
        return None
    return hashlib.sha1(" ".join(words[:40]).encode()).hexdigest()


def shared_text_signals(candidates: list[Candidate]) -> None:
    """Same description on several listings: a dealer template or a scam ring."""
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        if c.detail and (fp := _fingerprint(c.detail.description)):
            groups[fp].append(c)
    for group in groups.values():
        if len(group) < 2:
            continue
        for c in group:
            others = [o.listing.title for o in group if o is not c][:3]
            c.signals.append(
                Signal(
                    name="shared description",
                    detail=f"same text as {len(group) - 1} other listing(s): {'; '.join(others)}",
                    severity="warning",
                )
            )
    # Short boilerplate ("great condition price is negotiable") repeated across sellers.
    short: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        if c.detail and c.detail.description and len(c.detail.description) < 120:
            short[re.sub(r"\W+", " ", c.detail.description.lower()).strip()].append(c)
    for text, group in short.items():
        if text and len(group) >= 2:
            for c in group:
                c.signals.append(
                    Signal(
                        name="boilerplate text",
                        detail=f"identical short description on {len(group)} listings - common scam pattern",
                        severity="warning",
                    )
                )
