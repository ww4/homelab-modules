#!/usr/bin/env bash
# publish-iso — build the installer ISO and publish it as a GitHub Release on
# the library's public mirror, the way distros do it: a dated, immutable
# release per build, and one stable address that redirects to the newest:
#
#   https://github.com/ww4/homelab-modules/releases/latest/download/homelab-installer.iso
#   https://github.com/ww4/homelab-modules/releases/latest/download/homelab-installer.iso.sha256
#
#   tools/publish-iso.sh [--no-build]        (from configurator/)
#
# Needs: a GitHub token with Contents (releases) write on the repo, in
# GH_TOKEN or the env file named by GH_TOKEN_FILE (default
# ~/.config/ww4-bot/github-ww4-pat.env, variable GITHUB_BOT_TOKEN), and the
# forge token for the mirror nudge.
#
# ⚠️ THE TAG MUST EXIST ON THE FORGE, NOT ONLY ON GITHUB. This is how both
# earlier releases died. `gh release create --target <sha>` creates the tag on
# GitHub. The public repository is a PUSH MIRROR of the forge, and a mirror
# push PRUNES refs the forge does not have, so the next sync deleted the tag.
# GitHub demotes a release whose tag has gone to a DRAFT, and a draft 404s for
# everyone without write access, which is everyone the ISO is for. The two
# releases published on 2026-10-05 were found in exactly that state, assets
# intact and unreachable, with the site's download link dead the whole time.
#
# So the order is: tag on the forge, push it, make the mirror carry it, wait
# until the public side really has it, and only then create the release
# against a tag that already exists.
set -euo pipefail
repo=${GH_REPO:-ww4/homelab-modules}
build=1
[ "${1:-}" = --no-build ] && build=0
# A stale result-iso is how a release gets the wrong bytes under a new tag
# (2026-10-05): without a build, prove the link matches this checkout.

if [ -z "${GH_TOKEN:-}" ]; then
  f=${GH_TOKEN_FILE:-$HOME/.config/ww4-bot/github-ww4-pat.env}
  GH_TOKEN=$(sed -n 's/^GITHUB_BOT_TOKEN=//p' "$f")
fi
export GH_TOKEN
: "${GH_TOKEN:?no GitHub token}"

here=$(cd "$(dirname "$0")/.." && pwd); cd "$here"
rev=$(git -C "$here" rev-parse --short HEAD)
full_rev=$(git -C "$here" rev-parse HEAD)
if [ "$build" = 1 ]; then
  nix build '.#packages.x86_64-linux.iso' --out-link ./result-iso
fi
iso=$(find result-iso/iso -name "*.iso" 2>/dev/null | head -1)
[ -f "$iso" ] || { echo "no ISO at result-iso/iso (run without --no-build)" >&2; exit 1; }
if [ "$build" = 0 ]; then
  want=$(nix path-info '.#packages.x86_64-linux.iso' 2>/dev/null || true)
  have=$(readlink -f result-iso)
  [ -z "$want" ] || [ "$want" = "$have" ] || { echo "result-iso is $have but this checkout builds $want; drop --no-build" >&2; exit 1; }
fi

tag="installer-$(date +%Y%m%d)-${rev}"

# ── the tag, on the forge first ────────────────────────────────────────────
if [ -z "${FORGEJO_BOT_TOKEN:-}" ] && [ -r "$HOME/.config/ww4-bot/forgejo-token.env" ]; then
  FORGEJO_BOT_TOKEN=$(sed -n 's/^FORGEJO_BOT_TOKEN=//p' "$HOME/.config/ww4-bot/forgejo-token.env")
fi
git -C "$here" rev-parse -q --verify "refs/tags/$tag" >/dev/null 2>&1 \
  || git -C "$here" tag -a "$tag" -m "Installer $(date +%Y-%m-%d) ($rev)" "$full_rev"
git -C "$here" push -q origin "refs/tags/$tag"
# The forge's API base, derived from the remote so no host name lives here.
origin_url=$(git -C "$here" remote get-url origin); origin_url=${origin_url%.git}
forge_api=$(printf '%s\n' "$origin_url" | sed -E 's#^(https?://[^/]+)/([^/]+)/([^/]+)$#\1/api/v1/repos/\2/\3#')
# Nudge the mirror rather than waiting out its interval.
curl -fsS -X POST -H "Authorization: token ${FORGEJO_BOT_TOKEN:-}" "$forge_api/push_mirrors-sync" >/dev/null 2>&1 \
  || echo "could not nudge the mirror; waiting for its own schedule" >&2
echo "waiting for $tag to reach the public mirror..."
for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "https://api.github.com/repos/$repo/git/ref/tags/$tag" && break
  sleep 10
done
curl -fsS -o /dev/null "https://api.github.com/repos/$repo/git/ref/tags/$tag" || {
  echo "the tag $tag has not reached the public mirror. A release made now would be pruned back to a draft, so stopping." >&2
  exit 1
}
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
# Constant asset names, so the /releases/latest/download/ redirect resolves.
cp "$iso" "$work/homelab-installer.iso"
( cd "$work" && sha256sum homelab-installer.iso > homelab-installer.iso.sha256 )
notes="Installer ISO built from \`$rev\` ($(date -u +%Y-%m-%d)). The stable address \`releases/latest/download/homelab-installer.iso\` redirects here while this is the newest. Check: \`sha256sum -c homelab-installer.iso.sha256\` (Windows: \`certutil -hashfile homelab-installer.iso SHA256\`). The ISO self-updates its installer from the binary cache at boot, so an older stick is only stale in its fallback copy."

gh() { nix shell nixpkgs#gh -c gh "$@"; }
if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
  echo "release $tag exists; replacing its assets"
  gh release upload "$tag" --repo "$repo" --clobber "$work/homelab-installer.iso" "$work/homelab-installer.iso.sha256"
else
  # --verify-tag, not --target: the tag is already on both sides, and this
  # refuses to invent one if something above went wrong.
  gh release create "$tag" --repo "$repo" --verify-tag --latest --title "Installer $(date +%Y-%m-%d) ($rev)" --notes "$notes" \
    "$work/homelab-installer.iso" "$work/homelab-installer.iso.sha256"
fi
# gh has left a release as a draft here (2026-10-05): a draft 404s for
# everyone without write access, which is everyone the ISO is for. Say so,
# fix it, and prove it by asking as a stranger would.
gh release edit "$tag" --repo "$repo" --draft=false --latest >/dev/null
code=$(curl -sS -o /dev/null -w '%{http_code}' "https://api.github.com/repos/$repo/releases/latest" -H 'Authorization:')
[ "$code" = 200 ] || { echo "the release is not public (anonymous GET /releases/latest gave $code)" >&2; exit 1; }
# And the thing a reader actually clicks, followed to the end, as a stranger.
dl=$(curl -sS -o /dev/null -w '%{http_code}' -L "https://github.com/$repo/releases/latest/download/homelab-installer.iso" -H 'Authorization:')
[ "$dl" = 200 ] || { echo "the download link is not public (anonymous GET gave $dl)" >&2; exit 1; }
echo "published:"
echo "  https://github.com/$repo/releases/tag/$tag"
echo "  https://github.com/$repo/releases/latest/download/homelab-installer.iso   (redirects to the newest)"
echo "  https://github.com/$repo/releases/latest/download/homelab-installer.iso.sha256"
