#!/usr/bin/env bash
# publish-cache — push the configurator's closure to the public binary cache
# on DigitalOcean Spaces, signed, so the installer ISO can `nix run` the
# newest configurator at start instead of compiling it (or needing a new ISO).
#
#   tools/publish-cache.sh            (from configurator/)
#
# Needs: the Spaces key as an env file (DO_SPACES_KEY_ID, DO_SPACES_SECRET,
# DO_SPACES_ENDPOINT — the same one publish-iso.sh reads) and the cache
# signing key (nix key generate-secret). The matching public key is in iso.nix.
set -euo pipefail
env_file=${DO_ENV_FILE:-/run/secrets/digitalocean-iso}
# The signing key: the sops-managed copy on the reference box, else a local one.
key_file=${CACHE_KEY_FILE:-/run/secrets/homelab-cache-key}
[ -r "$key_file" ] || key_file=$HOME/.config/homelab-cache/secret-key
bucket=${BUCKET:-homelab-installer}
set -a; . "$env_file"; set +a
: "${DO_SPACES_KEY_ID:?}" "${DO_SPACES_SECRET:?}" "${DO_SPACES_ENDPOINT:?}"
[ -f "$key_file" ] || { echo "no signing key at $key_file" >&2; exit 1; }
endpoint=${DO_SPACES_ENDPOINT#https://}
here=$(cd "$(dirname "$0")/.." && pwd); cd "$here"

out=$(nix build '.#default' --no-link --print-out-paths)
rev=$(git -C "$here" rev-parse --short HEAD)
echo "configurator $rev → $out"
# Nix's S3 store speaks Spaces with the AWS credential variables.
export AWS_ACCESS_KEY_ID=$DO_SPACES_KEY_ID AWS_SECRET_ACCESS_KEY=$DO_SPACES_SECRET
store="s3://${bucket}/cache?endpoint=${endpoint}&region=us-east-1&scheme=https"
nix store sign --recursive --key-file "$key_file" "$out"
nix copy --to "$store" "$out"
# Spaces objects are private by default and Nix sets no ACL: make the cache
# prefix readable by anyone (the ISO fetches it anonymously).
acl_log=$(mktemp)
nix shell nixpkgs#s3cmd -c s3cmd --access_key="$DO_SPACES_KEY_ID" --secret_key="$DO_SPACES_SECRET" \
  --host="$endpoint" --host-bucket="%(bucket)s.$endpoint" \
  setacl --acl-public --recursive "s3://${bucket}/cache/" > "$acl_log" 2>&1 || { cat "$acl_log" >&2; rm -f "$acl_log"; echo "s3cmd setacl failed" >&2; exit 1; }
echo "$(grep -c 'ACL set' "$acl_log" || true) objects made public"
rm -f "$acl_log"
echo "pushed to https://${bucket}.${endpoint}/cache (nix-cache-info + narinfos public-read)"
