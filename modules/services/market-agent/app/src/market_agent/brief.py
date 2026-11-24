"""Search briefs: what someone wants, written down once, checked every run.

A brief is a TOML file. Hard limits exclude a listing outright (or mark it
"unknown" when the listing doesn't say); preferences only steer the judge.
See examples/winter-commuter.toml for a full vehicle brief.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class Strict(BaseModel):
    # A typo in a brief should be an error, not a silently ignored limit.
    model_config = ConfigDict(extra="forbid")


class Place(Strict):
    name: str
    place: str  # "Decatur, IL" - looked up on Facebook and cached


class Location(Strict):
    home: str
    destinations: list[Place] = Field(default_factory=list)
    max_miles: float | None = None  # None = no distance limit
    # "radius" = straight-line miles, how Facebook and most people mean "within
    # 50 miles". "driving" = road miles (OSRM if configured, else an estimate).
    distance: Literal["radius", "driving"] = "radius"
    measure_from: Literal["home", "nearest"] = "home"
    search_radius_miles: float = 60


class Price(Strict):
    min: float | None = None  # a floor drops "down payment" and bait prices
    max: float | None = None


class Search(Strict):
    queries: list[str]
    listed_within_days: int | None = None  # watch mode: only recent listings
    exclude_words: list[str] = Field(default_factory=list)
    max_details: int = 400  # cap on listing pages FETCHED per run; cached ones are free
    # Facebook returns only its first ~24 results per search (logged out, there
    # is no second page). Splitting the price range runs each query once per
    # band, and each band gets its own 24 - the cheapest way to see more.
    price_bands: int = 2
    # Listings found by an earlier run stay tracked this long even when this
    # run's search doesn't return them (results vary run to run).
    keep_days: int = 14

    @field_validator("queries")
    @classmethod
    def _queries(cls, v: list[str]) -> list[str]:
        v = [q.strip() for q in v if q.strip()]
        if not v:
            raise ValueError("at least one search query is required")
        return v


Drivetrain = Literal["awd", "4wd", "fwd", "rwd"]
Transmission = Literal["any", "automatic", "conventional-automatic", "manual"]
TitleRule = Literal["any", "clean"]


class Vehicle(Strict):
    drivetrain: list[Drivetrain] = Field(default_factory=list)  # empty = any
    title: TitleRule = "any"
    transmission: Transmission = "any"
    max_mileage: int | None = None  # hard limit
    min_year: int | None = None
    max_year: int | None = None


class Judge(Strict):
    # Cost caps, not filters. Anything over the cap still shows on the page as
    # "not scored yet", so a cap can never hide a listing.
    max_candidates: int = 100
    max_near_misses: int = 50
    photos: int = 6


class Brief(Strict):
    name: str
    title: str
    kind: Literal["general", "vehicle"] = "general"
    notes: str = ""  # who it's for, what it's for - the judge reads this
    preferences: list[str] = Field(default_factory=list)
    location: Location
    price: Price = Field(default_factory=Price)
    search: Search
    vehicle: Vehicle | None = None
    judge: Judge = Field(default_factory=Judge)

    # Set by load(); not part of the file.
    source_hash: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must be lowercase letters, digits and dashes")
        return v

    def model_post_init(self, _ctx: object) -> None:
        if self.kind == "vehicle" and self.vehicle is None:
            self.vehicle = Vehicle()


def load(path: Path) -> Brief:
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: not valid TOML: {e}") from e
    brief = Brief.model_validate(data)
    brief.source_hash = hashlib.sha256(raw).hexdigest()[:16]
    return brief


def resolve(name_or_path: str, briefs_dir: Path) -> Path:
    p = Path(name_or_path)
    if p.suffix == ".toml" and p.is_file():
        return p
    candidate = briefs_dir / f"{name_or_path}.toml"
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"no brief named {name_or_path!r} in {briefs_dir} (and no such .toml file)")


EXAMPLES = ("winter-commuter", "general")


def example_text(which: str) -> str:
    if which not in EXAMPLES:
        raise ValueError(f"unknown example {which!r}; choose from {', '.join(EXAMPLES)}")
    return resources.files("market_agent").joinpath(f"examples/{which}.toml").read_text()


def new_brief(name: str, briefs_dir: Path, example: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError("name must be lowercase letters, digits and dashes")
    dest = briefs_dir / f"{name}.toml"
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    text = example_text(example)
    text = re.sub(r'^name = ".*"$', f'name = "{name}"', text, count=1, flags=re.M)
    briefs_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    load(dest)  # the template must stay valid
    return dest
