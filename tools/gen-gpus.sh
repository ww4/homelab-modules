#!/usr/bin/env bash
# gen-gpus — build data/gpus.json: every graphics card the installer can
# recognise, with the memory it has.
#
#   tools/gen-gpus.sh > data/gpus.json
#
# ⚠️ WHAT COMES FROM WHERE. The PCI ids and the card names come from hwdata's
# pci.ids, which is authoritative, versioned and not ours. The memory comes
# from data/gpu-memory.tsv, by family, because pci.ids does not carry it and
# the installer cannot read it off the card. Splitting them that way means the
# only hand-written numbers in the result are the memory sizes.
#
# The installer fetches the result from the public mirror, and only when the
# machine actually has a graphics card. A server with none never asks.
set -euo pipefail
export LC_ALL=C
here=$(cd "$(dirname "$0")/.." && pwd)
ids=${PCI_IDS:-$(nix build nixpkgs#hwdata --no-link --print-out-paths 2>/dev/null | head -1)/share/hwdata/pci.ids}
[ -r "$ids" ] || { echo "no pci.ids at $ids" >&2; exit 1; }

python3 - "$ids" "$here/data/gpu-memory.tsv" <<'PY'
import json, re, sys, datetime, hashlib

ids_path, tsv_path = sys.argv[1], sys.argv[2]
VENDORS = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}

# families, in file order: the first pattern that matches a name wins
families = []
for line in open(tsv_path, encoding="utf-8"):
    if line.startswith("#") or not line.strip():
        continue
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 2 or not parts[1].strip():
        continue
    families.append((parts[0], int(parts[1]), (parts[2].strip() if len(parts) > 2 else "")))

cards, vendor = {}, None
for line in open(ids_path, encoding="utf-8", errors="replace"):
    if line.startswith("#") or not line.strip():
        continue
    if not line.startswith("\t"):
        vendor = line.split()[0] if line[:4].strip() else None
        continue
    if line.startswith("\t\t") or vendor not in VENDORS:
        continue
    m = re.match(r"\t([0-9a-f]{4})  (.+)", line.rstrip("\n"))
    if not m:
        continue
    device, name = m.group(1), m.group(2).strip()
    for pattern, gb, note in families:
        if pattern in name:
            e = {"name": name, "vendor": VENDORS[vendor], "vram_gb": gb}
            if note:
                e["note"] = note
            cards[f"{vendor}:{device}"] = e
            break

out = {
    "schema": 1,
    "updated": datetime.date.today().isoformat(),
    "names_from": "hwdata pci.ids, sha256 " + hashlib.sha256(open(ids_path, "rb").read()).hexdigest()[:16],
    "memory_from": "data/gpu-memory.tsv in this repository; corrections are a pull request",
    # What the installer tells a reader a card can comfortably run. Thresholds
    # live here so they can be revised without shipping a new installer.
    "tiers": [
        {"min_vram_gb": 24, "name": "large", "runs": "30B-class models at 4-bit, or a 70B at low quality"},
        {"min_vram_gb": 16, "name": "good", "runs": "14B-class models comfortably, 30B at 4-bit"},
        {"min_vram_gb": 10, "name": "fair", "runs": "7B and 8B models comfortably, 14B at 4-bit"},
        {"min_vram_gb": 6, "name": "small", "runs": "7B and 8B models at 4-bit"},
        {"min_vram_gb": 0, "name": "none", "runs": "nothing worth running on the card; models would fall back to the processor"},
    ],
    "cards": dict(sorted(cards.items())),
}
print(json.dumps(out, indent=1, sort_keys=False))
PY
