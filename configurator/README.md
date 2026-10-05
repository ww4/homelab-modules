# homelab-configure

Turn a set of answers into a private consumer flake for this library: the
modules you chose, your values, your secrets (minted or supplied, encrypted
with sops), a disk layout, and an install command. Headless and
machine-readable, so a terminal UI, a web form, or an agent can all drive it
the same way. Nothing lives only in a UI.

```sh
nix run 'git+https://git.rosemaryacres.com/ww4/homelab-modules.git?dir=configurator' -- --help   # leak-scan-ok: this repo's own home
```

## The three commands

| command | does |
|---|---|
| `schema [--modules a,b]` | the question set: every module, the `homelab.*` options it reads (type, default, required?), the secrets it needs and whether each is *generate*, *supply* or *first-boot* |
| `generate --out DIR [--answers FILE] [--add m,…] [--remove m,…] [--set OPT=JSON] [--secret OPT=@file …]` | write the flake, mint keys and secrets, then evaluate the result; on a directory that already holds `answers.json`, reconfigure it (see below) |
| `validate DIR [--build]` | evaluate (or build) a generated flake's toplevel |
| `tui [--profile FILE] [--answers FILE] [--out DIR]` | the installer's front end, shaped like Ubuntu Server's subiquity: Welcome → Kit → Storage → Profile → SSH → Domain and certificates → Modules → Review → Install → Finished, one question per screen with Back/Continue, the memory verdict on the way, the admin password typed, GitHub keys added (never replacing), and on the live USB the last screen runs `generate` and `install` itself and shows the addresses to open; elsewhere it writes the configuration and prints the install command. Starts by itself on the ISO's console |
| `install DIR [--yes] [--dry-run] [--keep-at PATH]` | the local install, from a live USB on the machine itself: writes this machine's `hardware.nix`, then `disko` (partition, format, mount) and `nixos-install` into `/mnt` (so the system downloads straight onto the new disk, not into live-USB RAM; EFI entries included), the pre-generated host key into `/etc/ssh`, and the whole flake directory onto the new system at `--keep-at` (default `/root/homelab`) — because the live USB is RAM |
| `dns DIR [--ip ADDR] [--token-file FILE] [--dry-run]` | create the A records the chosen modules claim at Cloudflare, with the ACME token: run it on the installed box once Tailscale is up (it points the names at the tailnet address, proxied off), or pass `--ip`; idempotent |

Every report carries a `memory` line: the catalog's rough resident figure for each chosen module, summed with a 1 GB base and compared with this machine's `MemTotal` (a warning when the box is short). `--json` on any of them gives structured output. Exit codes: 0 ok, 2 answers
rejected (every problem listed), 3 validation failed, 1 anything else.

The schema is baked into the binary from the library checkout it was built
from, so questions and modules cannot disagree.

## Answers

```json
{
  "host": {
    "name": "box",
    "timeZone": "Europe/Amsterdam",
    "disk": "/dev/disk/by-id/nvme-…",
    "dataDisks": [ { "name": "d1", "device": "/dev/disk/by-id/ata-…" } ],
    "sshAuthorizedKeys": [ "ssh-ed25519 AAAA… you@laptop" ]
  },
  "modules": [ "acme", "nginx-access", "monitoring", "ntfy", "alertmanager-ntfy", "jellyfin", "vaultwarden" ],
  "values": {
    "homelab.domain": "example.com",
    "homelab.adminUser": "alice",
    "homelab.acme.email": "alice@example.com",
    "homelab.ntfy.baseUrl": "http://box.example.com:8090"
  },
  "sops": { "adminRecipient": "age1…" }
}
```

- `modules` — `requires` are added for you and reported.
- `values` — any `homelab.*` option; JSON maps to Nix (objects → attrsets,
  lists → lists). Required options without a value are listed in the rejection.
- Modules gated by an `enable` option are enabled automatically.
- `sops.adminRecipient` — your existing age public key. Omit it and a key is
  generated into `keys/admin-age-key.txt` (git-ignored; move it out).
- `library` — the flake reference for the library input (a local `path:` works,
  and is what validation then uses too).

Secrets are **never** in the answers file. Supplied ones come in by
`--secret homelab.acme.credentialsFile=@/path/to/file` or
`--secret …=env:VAR`; the file's contents are used verbatim (the schema says
what lines it must carry).

## What comes out

```
DIR/
├── flake.nix              inputs + nixosConfigurations.<host>
├── homelab-values.nix     the homelab.* values + one sops declaration per secret
├── hosts/<host>/          default.nix (admin user, ssh, sops), disko.nix, hardware.nix
├── secrets/*.yaml         sops-encrypted to the host key AND the admin key
├── .sops.yaml
├── README.md              the install command, the DNS list, the secrets table
├── FIRST-LOGIN.md         show-once plaintext (git-ignored, mode 600) — read, store, delete
├── PHASE-2.md             only if some secret can exist only after a first boot
├── extra-files/etc/ssh/   the host's SSH key, pre-generated so sops works on first boot (git-ignored)
└── keys/                  a generated admin age key, if any (git-ignored)
```

Then, the one supported install path:

```sh
nixos-anywhere --flake .#<host> --extra-files ./extra-files \
  --generate-hardware-config nixos-generate-config ./hosts/<host>/hardware.nix root@<target>
```

## From a live USB, on the machine itself

Boot the NixOS installer ISO on the target, get it on the network, and:

```sh
nix run 'git+https://git.rosemaryacres.com/ww4/homelab-modules.git?dir=configurator' -- tui   # leak-scan-ok: this repo's own home
```

The Disks screen lists what the machine can see by stable id and lets you
mark the system disk, the data disks and a parity disk; the disk the live
USB booted from is not offered. On the Host screen a GitHub username pulls
your public keys in. On the Secrets screen `v` lets you type a value instead
of pointing at a file; it goes to a mode-600 file next to the answers, never
into the answers file. `g` on the Review screen runs `generate`, then:

```sh
sudo homelab-configure install ./my-homelab
```

which types back the host name as its confirmation, erases exactly the disks
you chose, installs, and carries the flake directory (your values, the
encrypted secrets, `keys/`, `FIRST-LOGIN.md`) to `/root/homelab` on the new
system — the live USB's filesystem is RAM and is gone at reboot. Move the
admin age key off the machine afterwards; read and delete `FIRST-LOGIN.md`.

## Guided secrets

The Secrets screen explains each supply-class secret where it is needed:
for the Cloudflare DNS token, the exact clicks to create one with the
"Edit zone DNS" template, and `v` takes the bare token, writes it as
`CLOUDFLARE_DNS_API_TOKEN=…` and checks it against your zone before you move
on; for the download stack's VPN, the steps for the provider you named in
`homelab.arrStack.vpnProvider` (which `.conf` fields map to which gluetun
variables, and what port forwarding changes); for the offsite backup, the
Backblaze key shape. A GitHub username on the Host screen pulls your public
keys in. After the install, `homelab-configure dns` turns the token into the
A records, so the whole TLS path — token, certificates, names — needs no
hand-edited DNS.

## Reconfiguring an existing install

`generate` writes a copy of the answers to `<out>/answers.json`. Run it again
on that directory and it starts from there instead of from a blank form:

```sh
homelab-configure generate --out ./my-homelab --add paperless --remove glances
homelab-configure generate --out ./my-homelab --set homelab.backup.keep.daily=14
homelab-configure generate --out ./my-homelab --answers new-answers.json   # replace wholesale
```

What a reconfigure keeps: every secret file already in `secrets/` (nothing is
re-minted; pass `--secret OPTION=@file` to replace one on purpose), the admin
age key and the host SSH key, the admin's console password (its hash is
recorded in `answers.json`), and `hosts/<host>/hardware.nix` once
nixos-anywhere has written the real one. `FIRST-LOGIN.md` appears only when
there is something new to show. The next step it prints is a rebuild, not an
install.

Removal is checked. A module another chosen module `requires` cannot go
(`--remove acme` while `jellyfin` is chosen is reported, and `acme` stays);
the foundation modules below are refused outright; removing `authelia` warns
which OIDC-wired apps it orphans. NixOS leaves a removed service's state under
`/var/lib` and its secret file in `secrets/` — the run says so, and deleting
them is yours to do.

## The foundation set

Some modules are not choices. `system` and `boot` are added to every plan.
`backup` must be chosen and configured before the first install (the restore
path has to exist before there is anything to restore), and a reconfigure
will not remove it; the same goes for `mergerfs-pools`, because the pool shape
is decided at install and changing it later is a data migration. A fresh
install without `monitoring` and `ntfy` is allowed, with a warning: a box with
no alerting is a box whose first failure is silent.

## Secret classes

| class | examples | what happens |
|---|---|---|
| generate | admin passwords, Vaultwarden `ADMIN_TOKEN` (argon2), OIDC client secrets | minted, encrypted, shown once in `FIRST-LOGIN.md`; OIDC secrets also get their pbkdf2 digest for `homelab.authelia.oidcClients` |
| supply | DNS API token, WireGuard conf, MeshCentral `.msh` | `--secret` or the run is rejected with the exact flag to pass |
| first-boot | API keys the *arrs mint on first run | a `CHANGEME` placeholder is encrypted so the flake evaluates; `PHASE-2.md` says what to replace |

OIDC client secrets are only minted when `authelia` is among the modules;
otherwise the nullable `*OidcSecretFile` options stay unset (no SSO wiring).

## Development

`tools/publish-iso.sh [--bucket NAME]` builds the ISO and uploads it (with a
sha256 and a `latest.txt`) to a DigitalOcean Space, public-read, with the
Spaces key in `/run/secrets/digitalocean-iso` (scoped to that bucket,
read/write/delete). The guide links the result.


```sh
cd configurator
nix develop
nix eval --json ..#catalog > /tmp/catalog.json
nix eval --raw .#optionsJson.x86_64-linux > /tmp/options.json
cargo test
cargo run -- --catalog /tmp/catalog.json --options /tmp/options.json schema
```

`nix build` produces the binary with the schema embedded and the tools it
delegates to (sops, age, ssh-to-age, ssh-keygen, authelia, mkpasswd, git,
nix) on its PATH.

### Releasing the installer

The ISO's console runs `nix run --max-jobs 0 'github:ww4/homelab-modules?dir=configurator' -- tui`, substituted from the signed binary cache on the Spaces bucket, so **a merge to `main` is the release**: on the reference box a user timer (`homelab-cache-sync`, every 5 minutes) notices the new commit and runs `tools/publish-cache.sh` (build, sign, `nix copy` to the S3 store, make the objects public). A stick picks it up at its next boot. `tools/publish-iso.sh` is only needed when `iso.nix` itself changes (the baked-in fallback copy and the console script); it publishes the image as a GitHub Release on the mirror (`installer-<date>-<commit>`), and `https://github.com/ww4/homelab-modules/releases/latest/download/homelab-installer.iso` redirects to the newest, the way distributions publish a current image.
