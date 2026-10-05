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
# ~/.config/ww4-bot/github-ww4-pat.env, variable GITHUB_BOT_TOKEN). The tag is
# installer-<date>-<rev>, on the commit the ISO was built from, which the
# mirror must already carry (it does within a minute of a merge).
set -euo pipefail
repo=${GH_REPO:-ww4/homelab-modules}
build=1
[ "${1:-}" = --no-build ] && build=0
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
iso=$(find result-iso/iso -name "*.iso" | head -1)
[ -f "$iso" ] || { echo "no ISO at result-iso/iso" >&2; exit 1; }

tag="installer-$(date +%Y%m%d)-${rev}"
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
  gh release create "$tag" --repo "$repo" --target "$full_rev" --latest --title "Installer $(date +%Y-%m-%d) ($rev)" --notes "$notes" \
    "$work/homelab-installer.iso" "$work/homelab-installer.iso.sha256"
fi
echo "published:"
echo "  https://github.com/$repo/releases/tag/$tag"
echo "  https://github.com/$repo/releases/latest/download/homelab-installer.iso   (redirects to the newest)"
echo "  https://github.com/$repo/releases/latest/download/homelab-installer.iso.sha256"
