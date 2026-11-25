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
    # Hard limits a listing must actually establish before it is shown as a
    # candidate. "unknown" on these puts the car on a "confirm first" list
    # instead: an AWD search should not show cars that may be FWD.
    must_confirm: list[str] = Field(default_factory=lambda: ["drivetrain"])
    min_year: int | None = None
    max_year: int | None = None
    # Electric cars and plug-ins: EPA-rated electric range when new, in miles.
    # A used battery has less; the scorer weighs what the listing says about it.
    min_range: int | None = None


class Judge(Strict):
    # Cost caps, not filters. Anything over the cap still shows on the page as
    # "not scored yet", so a cap can never hide a listing.
    max_candidates: int = 100  # quick screens for candidates inside the limits
    max_near_misses: int = 50  # quick screens for near misses
    max_full: int = 30  # full evaluations, for the best screens (starred ones always)
    photos: int = 6


class Brief(Strict):
    name: str
    title: str
    kind: Literal["general", "vehicle"] = "general"
    notes: str = ""  # who it's for, what it's for - the judge reads this
    # Plain-English must-haves the judge holds hard ("fence included"): a listing
    # that clearly fails one scores 2 or below; one the listing doesn't settle
    # becomes a question. Structured limits (price, distance, [vehicle]) are
    # enforced in code instead.
    requirements: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    paused: bool = False  # kept, shown, not searched
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


# ---------------------------------------------------------------------------
# The "+ New" / Edit form on the watch page speaks this flat shape.

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return slug or "watch"


def _lines(v: object) -> list[str]:
    if isinstance(v, list):
        items = [str(x) for x in v]
    else:
        items = str(v or "").splitlines()
    return [x.strip() for x in items if x and x.strip()]


def _num(v: object) -> float | None:
    if v in (None, ""):
        return None
    return float(v)  # type: ignore[arg-type]


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def from_form(form: dict, name: str | None = None, base: "Brief | None" = None) -> Brief:
    """Build a Brief from the page's form fields (validated like a file).

    With `base` (editing), settings the form doesn't show - search radius,
    distance mode, judge budgets, listing age - are kept from the base.
    """
    title = str(form.get("title") or "").strip()
    if not title:
        raise ValueError("a watch needs a title")
    dests = []
    for line in _lines(form.get("destinations")):
        label, _, place = line.partition(":")
        if place.strip():
            dests.append({"name": label.strip() or "Work", "place": place.strip()})
        else:
            dests.append({"name": "Work", "place": label.strip()})
    data: dict = {
        "name": name or slugify(title),
        "title": title,
        "kind": "vehicle" if form.get("kind") == "vehicle" else "general",
        "notes": str(form.get("notes") or "").strip(),
        "requirements": _lines(form.get("requirements")),
        "preferences": _lines(form.get("preferences")),
        "paused": bool(form.get("paused")),
        "location": {
            "home": str(form.get("home") or "").strip(),
            "destinations": dests,
            "max_miles": _num(form.get("max_miles")),
        },
        "price": {"min": _num(form.get("price_min")), "max": _num(form.get("price_max"))},
        "search": {
            "queries": _lines(form.get("queries")),
            "exclude_words": _lines(form.get("exclude_words")),
        },
    }
    if not data["location"]["home"]:
        raise ValueError("where should distances be measured from? (home)")
    if data["kind"] == "vehicle":
        v = form.get("vehicle") or {}
        keep = base.vehicle.model_dump() if base is not None and base.vehicle is not None else {}
        data["vehicle"] = {
            "drivetrain": [d for d in (v.get("drivetrain") or []) if d in ("awd", "4wd", "fwd", "rwd")],
            "title": "clean" if v.get("title") == "clean" else "any",
            "transmission": v.get("transmission") if v.get("transmission") in (
                "any", "automatic", "conventional-automatic", "manual") else "any",
            "max_mileage": int(_num(v.get("max_mileage")) or 0) or None,
            "min_year": int(_num(v.get("min_year")) or 0) or None,
            "max_year": int(_num(v.get("max_year")) or 0) or None,
            "min_range": int(_num(v.get("min_range")) or 0) or None,
        }
        data["vehicle"] = {**{k: x for k, x in keep.items() if k not in data["vehicle"]}, **data["vehicle"]}
    if base is not None:
        data = _merge(base.model_dump(exclude={"source_hash"}), data)
        if data["kind"] != "vehicle":
            data["vehicle"] = None
    return Brief.model_validate(data)


def to_form(b: Brief) -> dict:
    """The form's view of a brief (what Edit pre-fills)."""
    v = b.vehicle
    return {
        "name": b.name, "title": b.title, "kind": b.kind, "notes": b.notes, "paused": b.paused,
        "queries": "\n".join(b.search.queries), "requirements": "\n".join(b.requirements),
        "preferences": "\n".join(b.preferences), "exclude_words": "\n".join(b.search.exclude_words),
        "home": b.location.home, "max_miles": b.location.max_miles,
        "destinations": "\n".join(f"{d.name}: {d.place}" for d in b.location.destinations),
        "price_min": b.price.min, "price_max": b.price.max,
        "vehicle": None if v is None else {
            "drivetrain": v.drivetrain, "title": v.title, "transmission": v.transmission,
            "max_mileage": v.max_mileage, "min_year": v.min_year, "max_year": v.max_year,
            "min_range": v.min_range,
        },
    }


def _toml_value(v: object) -> str:
    import json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) and not v.is_integer() else str(int(v))
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items() if x is not None) + " }"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def to_toml(b: Brief) -> str:
    """Serialize a brief (as the form saves it). Round-trips through load()."""
    d = b.model_dump(exclude={"source_hash"}, exclude_none=True)
    lines = ["# Written by the watch page's form; edit there or here.", ""]
    tables = {k: d.pop(k) for k in ("location", "price", "search", "vehicle", "judge") if k in d}
    for k, v in d.items():
        lines.append(f"{k} = {_toml_value(v)}")
    for name, table in tables.items():
        lines += ["", f"[{name}]"]
        for k, v in table.items():
            if v is not None:
                lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


def save(b: Brief, briefs_dir: Path) -> Path:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    dest = briefs_dir / f"{b.name}.toml"
    tmp = dest.with_suffix(".toml.tmp")
    tmp.write_text(to_toml(b))
    load(tmp)  # refuse to replace a good file with one that doesn't load
    tmp.replace(dest)
    return dest
