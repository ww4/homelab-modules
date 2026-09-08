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

| Area | Modules |
|---|---|
| **Base** | `system`, `boot` |
| **Perimeter & SSO** | `nginx-access` (the source gate), `acme` (DNS-01 defaults), `authelia` (forward-auth + OIDC; ONE list drives both the nginx wiring and the access rule) |
| **Storage** | `mergerfs-pools`, `pool-autoremount` (zombie-mount aware, flap-capped), `smart-dump`, `drive-temps`, `disk-io-watch` |
| **Monitoring & alerting** | `monitoring` (Prometheus + Grafana + Alertmanager, alerting provisioned from files; quiet hours generated from one option), `deploy-drift-watch` (alerts when the forge is ahead of the deployed commit — the failure every applier-side gauge is blind to), `nginx-log-paths-check`, `ntfy` (write-only anonymous access, self-provisioning subscriber) |
| **Services** | `nextcloud`, `forgejo`, `vaultwarden`, `paperless`, `immich`, `jellyfin`, `audiobookshelf`, `tandoor`, `uptime-kuma`, `glances`, `metube`, `pinchflat`, `remote-desktop`, `meshagent` |
| **Download hygiene** | `arr-missing-sweep`, `qbit-vpn-watchdog`, `decluttarr` |

More migrates in from the private flake as it gets parameterized.

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
