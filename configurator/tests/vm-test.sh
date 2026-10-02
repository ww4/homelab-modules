#!/usr/bin/env bash
# vm-test — the end-to-end proof for one canned profile: generate the flake,
# then let nixos-anywhere install it into a NixOS VM test (disko partitions a
# virtual disk, nixos-install runs, the result boots). Nothing touches real
# hardware; the test needs a nix daemon with the `kvm` and `nixos-test`
# features (local or a remote builder).
#
#   tests/vm-test.sh profiles/media-box.json [--library path:/checkout]
#
# Supplied secrets get placeholder contents here: this proves the install
# path, not the VPN or the DNS provider. Exit 0 = the VM test passed.
set -euo pipefail
profile=${1:?profile answers file}; shift
lib=""
while [ $# -gt 0 ]; do case "$1" in --library) lib="$2"; shift 2 ;; *) echo "unknown arg $1" >&2; exit 2 ;; esac; done

here=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d -t homelab-vm-test.XXXXXX)
trap 'rm -rf "$work"' EXIT
name=$(basename "$profile" .json)

# Placeholders for every `supply` secret the profile needs, named by option.
secrets=()
mk() { printf '%s\n' "$2" > "$work/$1.secret"; secrets+=(--secret "$1=@$work/$1.secret"); }
mk homelab.acme.credentialsFile 'CLOUDFLARE_DNS_API_TOKEN=placeholder'
mk homelab.arrStack.vpnEnvFile $'WIREGUARD_PRIVATE_KEY=placeholder\nWIREGUARD_ADDRESSES=10.0.0.2/32\nSERVER_COUNTRIES=Netherlands'
mk homelab.backup.remote.environmentFile $'B2_ACCOUNT_ID=placeholder\nB2_ACCOUNT_KEY=placeholder'
mk homelab.meshagent.mshFile 'placeholder'

cfg=("$here/target/debug/homelab-configure")
[ -x "${cfg[0]}" ] || cfg=(homelab-configure)
extra=()
[ -n "$lib" ] && extra=(--library "$lib")

"${cfg[@]}" generate --answers "$profile" --out "$work/$name" "${secrets[@]}" "${extra[@]}" --validate none
host=$(jq -r .host.name "$profile")
( cd "$work/$name" && git init -q && git add -A )   # a flake needs its files tracked

echo "== nixos-anywhere --vm-test ($name, host $host)"
nixos-anywhere --vm-test --flake "$work/$name#$host"
echo "== vm-test passed for $name"
