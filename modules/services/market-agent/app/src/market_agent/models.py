"""Records that move through the pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["pass", "fail", "unknown"]


class Coordinates(BaseModel):
    latitude: float
    longitude: float
    name: str = ""


class Listing(BaseModel):
    """One search result, before its detail page is read."""

    id: str
    title: str
    price_text: str
    price: float | None = None
    location_text: str | None = None
    image: str | None = None
    seller: str | None = None
    query: str = ""

    @property
    def url(self) -> str:
        return f"https://www.facebook.com/marketplace/item/{self.id}"


class Detail(BaseModel):
    """What the listing's own page says."""

    id: str
    title: str | None = None
    price: float | None = None
    description: str = ""
    images: list[str] = Field(default_factory=list)
    location_text: str | None = None
    coords: Coordinates | None = None
    created_at: int | None = None  # unix seconds
    is_sold: bool = False
    is_pending: bool = False
    condition: str | None = None
    # Every vehicle_* field Facebook returned, as-is.
    vehicle: dict[str, Any] = Field(default_factory=dict)
    missing_parts: list[str] = Field(default_factory=list)
    fetched_at: int | None = None  # unix seconds this page was read


class Check(BaseModel):
    """One hard limit from the brief, applied to one listing."""

    name: str
    status: CheckStatus
    detail: str


class Signal(BaseModel):
    """Something worth knowing that is not a hard limit (dealer ad, shared text, ...)."""

    name: str
    detail: str
    severity: Literal["info", "caution", "warning"] = "caution"


class Evaluation(BaseModel):
    """The judge's read of one candidate. Mirrors judge.EVALUATION_SCHEMA."""

    score: float
    verdict: Literal["top", "consider", "skip"]
    headline: str
    strengths: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    seller_questions: list[str] = Field(default_factory=list)
    photo_findings: list[str] = Field(default_factory=list)
    seller_kind: Literal["private", "dealer_or_flipper", "unclear"] = "unclear"
    scam_risk: Literal["low", "medium", "high"] = "low"


class Candidate(BaseModel):
    """A listing with everything we learned about it."""

    listing: Listing
    detail: Detail | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)  # recalls, bulletins, VIN decode
    evaluation: Evaluation | None = None
    judge_error: str | None = None

    @property
    def excluded(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def unknown_checks(self) -> list[Check]:
        return [c for c in self.checks if c.status == "unknown"]
