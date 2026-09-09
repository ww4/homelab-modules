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
| `generate --answers FILE --out DIR [--secret OPT=@file …]` | write the flake, mint keys and secrets, then evaluate the result |
| `validate DIR [--build]` | evaluate (or build) a generated flake's toplevel |

`--json` on any of them gives structured output. Exit codes: 0 ok, 2 answers
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

## Secret classes

| class | examples | what happens |
|---|---|---|
| generate | admin passwords, Vaultwarden `ADMIN_TOKEN` (argon2), OIDC client secrets | minted, encrypted, shown once in `FIRST-LOGIN.md`; OIDC secrets also get their pbkdf2 digest for `homelab.authelia.oidcClients` |
| supply | DNS API token, WireGuard conf, MeshCentral `.msh` | `--secret` or the run is rejected with the exact flag to pass |
| first-boot | API keys the *arrs mint on first run | a `CHANGEME` placeholder is encrypted so the flake evaluates; `PHASE-2.md` says what to replace |

OIDC client secrets are only minted when `authelia` is among the modules;
otherwise the nullable `*OidcSecretFile` options stay unset (no SSO wiring).

## Development

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
