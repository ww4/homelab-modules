#!/usr/bin/env python3
"""Build data/vehicle_configs.json from the EPA fuel economy dataset.

    curl -LO https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip
    unzip vehicles.csv.zip
    python3 tools/build_vehicle_configs.py vehicles.csv src/market_agent/data/vehicle_configs.json

fueleconomy.gov lists every US-market configuration (engine x transmission x
drive) of every light-duty model, which is exactly what "was a manual ever
offered on a 2008 CR-V?" or "is every Murano a CVT?" needs. It is US
government data (public domain). It does NOT cover vehicles over 8,500 lb
GVWR: heavy-duty pickups (2500/3500, F-250+), the Hummer H2, big vans. Those
stay "unknown" and the listing has to say.

Output: {"source", "built", "first_year", "models": {"Make|Model": [[y0, y1, [[displ, drive, trans, spec, kind], ...]], ...]}}
(kind: "" | hybrid | phev | ev; drive: fwd rwd 2wd awd 4wd 4x4, where 4x4 is EPA's pre-2010 "4WD or AWD")
with consecutive years merged when the set of configurations is identical.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import defaultdict

FIRST_YEAR = 2000

DRIVE = {
    "Front-Wheel Drive": "fwd",
    "Rear-Wheel Drive": "rwd",
    "2-Wheel Drive": "2wd",
    "All-Wheel Drive": "awd",
    "4-Wheel or All-Wheel Drive": "4x4",  # EPA's pre-2010 label for both: shown as "4WD/AWD"
    "4-Wheel Drive": "4wd",
    "Part-time 4-Wheel Drive": "4wd",
}

# Builders of conversions / limited specials, not what turns up used.
SKIP_MAKES = {"Roush Performance", "Saleen Performance", "Tecstar, LP", "Shelby", "RUF Automobile",
              "Mobility Ventures LLC", "VPG", "Azure Dynamics", "Kandi", "London Taxi", "BMW Alpina",
              "Spyker", "Pagani", "Koenigsegg", "Bugatti", "Bugatti Rimac"}


def trans(trany: str) -> tuple[str, str] | None:
    """EPA transmission text -> (class, short spec). Classes: manual cvt auto dct single."""
    t = trany.strip()
    if not t:
        return None
    speeds = re.search(r"(\d+)", t)
    n = speeds.group(1) if speeds else ""
    if t.startswith("Manual"):
        return "manual", f"{n}-speed manual"
    if "variable gear" in t or re.search(r"\(AV", t):
        return "cvt", "CVT"
    if re.search(r"\(AM", t):
        return "dct", f"{n}-speed automated manual (dual-clutch)"
    if re.search(r"\(A1\)|\(A2\)", t):
        return "single", "single-speed (electric)"
    return "auto", f"{n}-speed automatic"


def build(csv_path: str) -> dict:
    by_model: dict[str, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for r in csv.DictReader(open(csv_path, encoding="utf-8")):
        year = int(r["year"])
        if year < FIRST_YEAR or r["make"] in SKIP_MAKES:
            continue
        drive = DRIVE.get(r["drive"])
        tr = trans(r["trany"])
        if not drive or not tr:
            continue
        displ = r["displ"] or ""
        displ = f"{float(displ):.1f}" if displ not in ("", "0", "0.0") else ""
        kind = {"Hybrid": "hybrid", "Plug-in Hybrid": "phev", "EV": "ev"}.get(r["atvType"], "")
        if kind == "ev":
            tr = ("single", "single-speed (electric)")  # EPA files some EVs as "variable gear ratios"
        model = re.sub(r"\s+Pickup$", "", r["baseModel"].strip())
        by_model[f"{r['make'].strip()}|{model}"][year].add((displ, drive, tr[0], tr[1], kind))
    models: dict[str, list] = {}
    for key, years in sorted(by_model.items()):
        ranges: list = []
        for y in sorted(years):
            configs = sorted(years[y])
            if ranges and ranges[-1][1] == y - 1 and ranges[-1][2] == configs:
                ranges[-1][1] = y
            else:
                ranges.append([y, y, configs])
        models[key] = [[a, b, [list(c) for c in cs]] for a, b, cs in ranges]
    return {
        "source": "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip",
        "built": time.strftime("%Y-%m-%d"),
        "first_year": FIRST_YEAR,
        "models": models,
    }


if __name__ == "__main__":
    out = build(sys.argv[1])
    with open(sys.argv[2], "w") as f:
        json.dump(out, f, separators=(",", ":"))
        f.write("\n")
    print(f"{len(out['models'])} models -> {sys.argv[2]}")
