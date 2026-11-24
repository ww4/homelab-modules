"""What a judge's score is allowed to mean.

The judge reads a listing and gives a raw 0-10 score. This module turns that
into the verdict shown on the page and used for notifications, with rules the
judge does not get a vote on:

- The verdict follows the score: 7+ top, 5-6.9 consider, below 5 skip. (The
  judge used to pick its verdict separately and labelled 3.0-4.5 "consider".)
- Some findings cap the score: scam risk, a buy-here-pay-here or financing ad,
  text shared with other listings, a dealer or flipper. (The judge flagged
  "scam risk medium" and still scored the car 4.5.)
- A hard requirement the listing never confirms (by default: is it AWD/4WD at
  all?) keeps the car off the main cards and out of notifications. It goes on
  a "confirm first" list with the question to ask. (An unconfirmed pickup
  scored 5.5 and sat beside cars that were verified AWD.)

Applied when ranking and rendering, so stored evaluations are re-read under the
current rules without new judge calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .brief import Brief
from .models import Candidate

TOP = 7.0
CONSIDER = 5.0
BHPH = re.compile(r"financing|down payment|buy here|bhph", re.I)
SHARED = {"shared description", "boilerplate text"}


@dataclass
class Effective:
    score: float
    verdict: str  # top | consider | skip | confirm
    caps: list[str] = field(default_factory=list)
    confirm: list[str] = field(default_factory=list)  # hard limits the listing doesn't establish


def must_confirm(brief: Brief) -> set[str]:
    return set(brief.vehicle.must_confirm) if brief.vehicle else set()


def effective(c: Candidate, brief: Brief) -> Effective | None:
    e = c.evaluation
    if e is None:
        return None
    score = e.score
    caps: list[str] = []

    def cap(limit: float, why: str) -> None:
        nonlocal score
        if score > limit:
            score = limit
            caps.append(why)

    if e.scam_risk == "high":
        cap(1.0, "scam risk high")
    elif e.scam_risk == "medium":
        cap(3.0, "scam risk medium")
    dealer_text = " ".join(s.detail for s in c.signals if s.name == "dealer or flipper")
    if BHPH.search(dealer_text) or BHPH.search(c.listing.title):
        cap(3.0, "financing / down-payment ad")
    if any(s.name in SHARED for s in c.signals):
        cap(3.0, "same text as other listings")
    if dealer_text or e.seller_kind == "dealer_or_flipper":
        cap(TOP - 0.1, "dealer or flipper")

    confirm = [f"{ch.name}: {ch.detail}" for ch in c.unknown_checks if ch.name in must_confirm(brief)]
    if confirm:
        return Effective(score, "confirm", caps, confirm)
    verdict = "top" if score >= TOP else "consider" if score >= CONSIDER else "skip"
    return Effective(score, verdict, caps, confirm)


def rank(cands: list[Candidate], brief: Brief) -> list[Candidate]:
    order = {"top": 0, "consider": 1, "confirm": 2, "skip": 3}

    def key(c: Candidate) -> tuple[int, float]:
        if c.excluded and not c.facts.get("near_miss"):
            return (6, 0)
        eff = effective(c, brief)
        if c.excluded:
            return (5, -(eff.score if eff else 0))
        if eff is None:
            return (4, 0)
        return (order[eff.verdict], -eff.score)

    return sorted(cands, key=key)
