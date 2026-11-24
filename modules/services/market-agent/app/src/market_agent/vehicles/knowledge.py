"""The model-year facts table (data/vehicles.toml)."""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field


class Generation(BaseModel):
    years: tuple[int, int]
    transmission: str
    transmission_type: str
    drivetrain: str
    engine: str | None = None


class ModelEntry(BaseModel):
    make: str
    model: str
    aliases: list[str]
    generations: list[Generation] = Field(default_factory=list)

    def generation(self, year: int | None) -> Generation | None:
        if year is None:
            return None
        return next((g for g in self.generations if g.years[0] <= year <= g.years[1]), None)


class Issue(BaseModel):
    make: str
    model: str
    years: tuple[int, int]
    kind: str
    title: str
    detail: str
    ask: str
    source: str | None = None
    verified: bool = False
    drivetrain: str | None = None  # only applies to this drivetrain

    def applies(self, year: int | None, drive: str | None) -> bool:
        if year is None or not (self.years[0] <= year <= self.years[1]):
            return False
        # An AWD-only issue still applies when drivetrain is unknown.
        return not (self.drivetrain and drive and drive != self.drivetrain)


class Knowledge(BaseModel):
    models: list[ModelEntry]
    issues: list[Issue]

    def identify(self, text: str, make_hint: str | None = None, model_hint: str | None = None) -> ModelEntry | None:
        if model_hint:
            hint = model_hint.lower()
            for m in self.models:
                if hint == m.model.lower() or hint in m.aliases:
                    if not make_hint or make_hint.lower() == m.make.lower():
                        return m
        low = f" {text.lower()} "
        for m in self.models:
            for alias in m.aliases:
                if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", low):
                    return m
        return None

    def issues_for(self, entry: ModelEntry, year: int | None, drive: str | None) -> list[Issue]:
        return [
            i
            for i in self.issues
            if i.make == entry.make and i.model == entry.model and i.applies(year, drive)
        ]


@lru_cache(maxsize=1)
def load() -> Knowledge:
    raw: dict[str, Any] = tomllib.loads(
        resources.files("market_agent").joinpath("data/vehicles.toml").read_text()
    )
    return Knowledge.model_validate(raw)
