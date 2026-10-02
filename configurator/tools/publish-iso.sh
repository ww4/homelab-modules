#!/usr/bin/env bash
# publish-iso — build the installer ISO and put it in a DigitalOcean Space,
# public-read, with a sha256 beside it and a `latest.txt` the guide links.
#
#   tools/publish-iso.sh [--env FILE] [--bucket NAME] [--no-build]
#
# Credentials come from an env file (default /run/secrets/digitalocean-iso, the
# sops-materialised copy) carrying DO_SPACES_KEY_ID, DO_SPACES_SECRET and
# DO_SPACES_ENDPOINT (https://<region>.digitaloceanspaces.com). Nothing is
# printed from it. The Space named by --bucket is created if missing. Uploads go through rclone with the S3 backend;
# objects: iso/homelab-installer-<date>-<rev>.iso, .sha256, iso/latest.txt, and the
# stable iso/homelab-installer-latest.iso (+ .sha256) the Quick start links
# naming the newest file.
set -euo pipefail
env_file=/run/secrets/digitalocean-iso; bucket=homelab-installer; build=1
while [ $# -gt 0 ]; do case "$1" in --env) env_file="$2"; shift 2 ;; --bucket) bucket="$2"; shift 2 ;; --no-build) build=0; shift ;; *) echo "unknown arg $1" >&2; exit 2 ;; esac; done
[ -r "$env_file" ] || { echo "no credentials at $env_file" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$env_file"
set +a
: "${DO_SPACES_KEY_ID:?}" "${DO_SPACES_SECRET:?}" "${DO_SPACES_ENDPOINT:?}"
endpoint=${DO_SPACES_ENDPOINT#https://}
region=${endpoint%%.*}

here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
if [ "$build" = 1 ]; then
  nix build '.#packages.x86_64-linux.iso' --out-link ./result-iso
fi
iso=$(find result-iso/iso -name "*.iso" | head -1)
[ -f "$iso" ] || { echo "no ISO at result-iso/iso" >&2; exit 1; }
rev=$(git -C "$here" rev-parse --short HEAD)
name="homelab-installer-$(date +%Y%m%d)-${rev}.iso"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
cp "$iso" "$work/$name"
( cd "$work" && sha256sum "$name" > "$name.sha256" )
printf '%s\n' "$name" > "$work/latest.txt"

# rclone config in a private temp file; the S3 backend speaks Spaces.
conf="$work/rclone.conf"; umask 077
cat > "$conf" <<CONF
[spaces]
type = s3
provider = DigitalOcean
access_key_id = $DO_SPACES_KEY_ID
secret_access_key = $DO_SPACES_SECRET
endpoint = $endpoint
acl = public-read
CONF
# A scoped key cannot CreateBucket, and rclone's upload path calls it as its
# bucket-exists check: skip that (the bucket must already exist).
RCLONE="rclone --config $conf --s3-no-check-bucket"
$RCLONE copy --progress "$work/$name" "spaces:${bucket}/iso/"
$RCLONE copy "$work/$name.sha256" "spaces:${bucket}/iso/"
$RCLONE copyto "$work/latest.txt" "spaces:${bucket}/iso/latest.txt"
# A stable name for a person following the guide (server-side copies, no re-upload).
$RCLONE copyto "spaces:${bucket}/iso/$name" "spaces:${bucket}/iso/homelab-installer-latest.iso"
$RCLONE copyto "spaces:${bucket}/iso/$name.sha256" "spaces:${bucket}/iso/homelab-installer-latest.iso.sha256"
base="https://${bucket}.${region}.cdn.digitaloceanspaces.com/iso"
echo "published:"
echo "  $base/$name"
echo "  $base/$name.sha256"
echo "  $base/latest.txt"
echo "  $base/homelab-installer-latest.iso  (stable name, same bytes)"
