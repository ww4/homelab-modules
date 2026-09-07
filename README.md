# homelab-modules

An option-driven NixOS module library for a self-hosted homelab, extracted
from a real, running fleet. Implementations live here; personal values —
domain, addresses, users, secrets — live in the consumer's own flake and reach
these modules through the `homelab.*` option set.

The modules carry their scars deliberately: comments explain not just what a
knob does but which failure taught us to set it. Lift anything useful.

## Using it

```nix
{
  inputs.homelab-modules.url = "git+https://git.rosemaryacres.com/ww4/homelab-modules.git"; # leak-scan-ok: this repo's own home

  outputs = { nixpkgs, homelab-modules, ... }: {
    nixosConfigurations.mybox = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        homelab-modules.nixosModules.system
        homelab-modules.nixosModules.boot
        homelab-modules.nixosModules.smart-dump
        homelab-modules.nixosModules.disk-io-watch
        # ... pick what you want ...
        ({ ... }: {
          # Your values — the interface between this library and your box.
          homelab.ntfy.url = "http://localhost:8090/my-alerts";
          homelab.quietHours = { start = 22; end = 7; };
          time.timeZone = "America/New_York";
        })
      ];
    };
  };
}
```

Some modules expect a sops secret to be declared in *your* flake (never in
this one) — each module's header comment says exactly what to declare.

## What's here so far

| Module | What it does |
|---|---|
| `system` | Locale, nix-ld, Nix GC/optimise settings |
| `boot` | systemd-boot, cmdline-editor lockdown, never-sleep for servers |
| `remote-desktop` | xrdp + XFCE, Tailscale-only (the reliable NixOS remote-GUI combo) |
| `meshagent` | MeshCentral endpoint agent — packaged and servicified (a nixpkgs gap) |
| `smart-dump` | Full SMART tables for every drive via a closed-vocabulary root wrapper |
| `disk-io-watch` | Per-device kernel I/O-error counters → Prometheus; catches the quiet fault shape before a drive drops |
| `nginx-log-paths-check` | Eval-time guard: an nginx log path outside its writable set fails the build instead of downing every vhost |
| `arr-missing-sweep` | Weekly missing-content search for Sonarr/Radarr (which have none), with indexer politeness built in |
| `qbit-vpn-watchdog` | Self-heals the qBittorrent-in-gluetun network-namespace wedge |
| `decluttarr` | Conservative dead-download reaper for the *arr queue |

More migrates in from the private flake as it gets parameterized — monitoring,
SSO, storage, backups are next.

## Design rules

- **One concern, one file, one import line.** The consumer's manifest should
  read top-to-bottom as a description of their machine.
- **No personal values in this repo, ever.** Not in modules, not in comments,
  not in commit messages. `tools/leak-scan.sh` enforces it before every push.
- **Secrets are declared by the consumer**, decrypted with the consumer's own
  host key (sops-nix). This library only ever references
  `config.sops.secrets.<name>.path`.
- **Privileged wrappers have closed vocabularies.** Where a module grants a
  scoped operator root capability (`smart-dump`), the wrapper accepts no
  arguments that reach the underlying tool.

## License

GPL-3.0 — see [LICENSE](LICENSE).
