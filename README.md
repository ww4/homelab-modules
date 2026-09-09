# homelab-modules

A NixOS module library for a self-hosted homelab, extracted from a running
fleet. The modules hold implementations; everything specific to a site —
domain, users, paths, secrets — comes in through the `homelab.*` option set,
so one library serves any number of machines.

## Using it

Add the flake input, import the modules you want, and set your values:

```nix
{
  inputs.homelab-modules.url = "git+https://git.rosemaryacres.com/ww4/homelab-modules.git"; # leak-scan-ok: this repo's own home
  # mirrored at github:ww4/homelab-modules

  outputs = { nixpkgs, homelab-modules, ... }: {
    nixosConfigurations.mybox = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        homelab-modules.nixosModules.acme
        homelab-modules.nixosModules.nginx-access
        homelab-modules.nixosModules.monitoring
        homelab-modules.nixosModules.jellyfin
        # ... pick what you want ...
        ({ ... }: {
          # Your values.
          homelab.domain = "example.com";
          homelab.adminUser = "alice";
          homelab.acme.email = "alice@example.com";
          homelab.acme.credentialsFile = "/run/secrets/dns-api-token";
          homelab.monitoring.enable = true;
          homelab.ntfy.url = "http://localhost:8090/alerts";
          time.timeZone = "America/New_York";
        })
      ];
    };
  };
}
```

Conventions that apply throughout:

- **Vhosts.** A service module named `foo` serves at `foo.<homelab.domain>`
  with TLS via the `acme` module's DNS-01 defaults. DNS records and the
  domain are yours to provide.
- **Importing a module enables it**, except where a module documents an
  `enable` option (`monitoring`, `authelia`, `deployDriftWatch`).
- **Secrets are yours.** This library never declares a sops secret. Where a
  module needs one, its header comment says exactly what to declare (name,
  keys, owner) and the module reads it through an option or by the
  documented name. Keep declarations and encrypted files in your own flake.
- **Options live in `modules/options.nix`.** Every `homelab.*` option has a
  description; that file is the reference.

## What's here

| Area | Modules |
|---|---|
| **Base** | `system`, `boot` |
| **Perimeter & SSO** | `nginx-access` (allow/deny inherited by every vhost from one place), `acme` (DNS-01 defaults), `authelia` (forward-auth + OIDC; one `protectedVhosts` list drives both the nginx wiring and the access rule) |
| **Storage** | `mergerfs-pools`, `pool-autoremount` (detects zombie mounts with real I/O, recovers stale superblocks safely, flap-capped), `smart-dump`, `drive-temps`, `disk-io-watch` |
| **Monitoring & alerting** | `monitoring` (Prometheus + Grafana + Alertmanager; alert rules, contact points, policies and quiet hours all provisioned from configuration), `alertmanager-ntfy` (webhook → phone notifications), `ntfy` (write-only anonymous access, self-provisioning subscriber), `deploy-drift-watch` (forge head vs deployed commit), `mirror-drift-watch` (source vs mirror heads), `nginx-log-paths-check` (build-time guard) |
| **Services** | `nextcloud`, `forgejo`, `vaultwarden`, `paperless`, `immich`, `jellyfin`, `audiobookshelf`, `tandoor`, `silverbullet`, `uptime-kuma`, `glances`, `metube`, `pinchflat`, `remote-desktop`, `meshagent` |
| **Download stack** | `arr` (Prowlarr/Sonarr/Radarr/Jellyseerr/qBittorrent inside a Gluetun VPN namespace), `recyclarr` (bring your own profile YAML), `unpackerr`, `decluttarr`, `lidarr`, `lazylibrarian`, `aurral`, `arr-missing-sweep`, `qbit-vpn-watchdog` |

Module headers carry the details: what each module does, which options it
reads, what you must declare, and the reasoning behind non-obvious settings.
Read the header before importing.

## Design rules

- **One concern, one file, one import line.** Your manifest should read top
  to bottom as a description of your machine.
- **No site values in this repo — ever.** Not in modules, comments, or
  commit messages. `tools/leak-scan.sh` runs before every push and fails on
  any identifier from the source fleet.
- **Watchers must fail loudly.** Exporters here never republish stale data
  as fresh: a failed read publishes an explicit failure signal or nothing,
  and each watcher has an alert on its own liveness. A monitor whose silence
  looks like health is worse than no monitor.
- **Privileged wrappers have closed vocabularies.** Where a module grants a
  scoped operator root capability (`smart-dump`), the wrapper accepts no
  arguments that reach the underlying tool.

## License

GPL-3.0 — see [LICENSE](LICENSE).
