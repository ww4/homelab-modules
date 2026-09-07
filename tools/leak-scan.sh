#!/usr/bin/env bash
# leak-scan — no personal identifiers may exist anywhere in this repo.
#
# Run from the repo root before EVERY push. Exit 0 = clean.
# A justified false positive can be waived by putting the literal string
#   leak-scan-ok
# in a comment on the same line (the waiver itself is then visible in review).
set -euo pipefail

PAT='rosemaryacres|saenzmail|broadlinc|chris|saenz|100\.(82\.117|66\.171|112\.10|71\.248)|2603:6013|/mnt/fusion|/mnt/backup|gromit|wallace\b|marcus\b|bub\b|github\.com/ww4|darkpeers|retrotoon|torrentleech|digitalcore|myanonamouse|anonamouse|dynamicSeedbox|lock3|driveonwood|kentucky|craigmyle|airvpn'

hits=$(grep -rniE "$PAT" . \
        --exclude-dir=.git \
        --exclude=LICENSE \
        --exclude=leak-scan.sh \
      | grep -v 'leak-scan-ok' || true)

if [ -n "$hits" ]; then
  echo "LEAK SCAN FAILED — personal identifiers found:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "leak-scan: clean"
