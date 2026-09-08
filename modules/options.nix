# The homelab.* option set — the single interface between this library and a
# consumer's flake. Implementations read these; the consumer's flake sets them.
# Grown as modules are parameterized; never given personal defaults.
{ lib, ... }:

{
  options.homelab = {
    domain = lib.mkOption {
      type = lib.types.str;
      example = "example.com";
      description = ''
        The base domain every vhost hangs off (services live at
        <name>.<domain>). No default — set it in your flake.
      '';
    };

    adminUser = lib.mkOption {
      type = lib.types.str;
      default = "admin";
      description = "Username of the human administrator (SSO seed user, etc.).";
    };

    adminDisplayName = lib.mkOption {
      type = lib.types.str;
      default = "Admin";
      description = "Display name for the administrator.";
    };

    ntfy = {
      url = lib.mkOption {
        type = lib.types.str;
        default = "http://localhost:8090/alerts";
        description = ''
          Full URL (server + topic) that library modules POST notifications
          to, in ntfy.sh format. Point it at your own ntfy instance/topic.
        '';
      };
      baseUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://localhost:8090";
        description = ''
          The URL clients (the phone app) use to reach the ntfy server —
          typically the host's tailnet IP + port.
        '';
      };
      topic = lib.mkOption {
        type = lib.types.str;
        default = "alerts";
        description = "The alert topic name (subscriber access is granted on it).";
      };
    };

    acme = {
      email = lib.mkOption {
        type = lib.types.str;
        description = "Contact email for Let's Encrypt.";
      };
      dnsProvider = lib.mkOption {
        type = lib.types.str;
        default = "cloudflare";
        description = "lego DNS provider name for DNS-01 challenges.";
      };
      credentialsFile = lib.mkOption {
        type = lib.types.str;
        description = "environmentFile with the DNS provider API credential (a sops secret).";
      };
    };

    nextcloud = {
      adminPasswordFile = lib.mkOption {
        type = lib.types.str;
        description = "Initial admin password file (sops; owner = nextcloud).";
      };
      oidcSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "OIDC client secret path (owner = nextcloud); null = no SSO wiring.";
      };
    };

    forgejo = {
      oidcSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "OIDC client secret path (owner = forgejo); null = no SSO wiring.";
      };
    };

    quietHours = {
      # Non-critical notifications are suppressed between start and end.
      # Metrics keep publishing either way — only the phone stays silent.
      start = lib.mkOption {
        type = lib.types.ints.between 0 23;
        default = 22;
        description = "Hour (local time) when non-critical notifications stop.";
      };
      end = lib.mkOption {
        type = lib.types.ints.between 0 23;
        default = 7;
        description = "Hour (local time) when non-critical notifications resume.";
      };
    };

    # ── download-stack shared values ─────────────────────────────────────────
    # One data tree (root) shared by the download client and every importer so
    # imports hardlink instead of copying; one uid:gid so ownership matches
    # across containers.
    arrStack = {
      root = lib.mkOption {
        type = lib.types.str;
        example = "/mnt/media/arr";
        description = "The shared /data tree (downloads + media subdirs).";
      };
      puid = lib.mkOption {
        type = lib.types.str;
        default = "1000";
        description = "uid the stack's containers run as.";
      };
      pgid = lib.mkOption {
        type = lib.types.str;
        default = "100";
        description = "gid the stack's containers run as.";
      };
      owner = lib.mkOption {
        type = lib.types.str;
        description = "Host user owning the stack's directories (matches puid).";
      };
      group = lib.mkOption {
        type = lib.types.str;
        default = "users";
        description = "Host group owning the stack's directories (matches pgid).";
      };
      scratchDir = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/mnt/scratch/qbittorrent-incomplete";
        description = ''
          Incomplete-download dir on a SEPARATE filesystem (spares the pool's
          IO; the client copies once on completion). null = incomplete stays
          inside the /data tree.
        '';
      };
      vpnProvider = lib.mkOption {
        type = lib.types.str;
        example = "mullvad";
        description = "gluetun VPN_SERVICE_PROVIDER for the download client's tunnel.";
      };
      keepersMovies = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = ''
          Optional long-term-keeper library mounted at /keepers/movies —
          add it as a second Radarr root folder and promote via Edit → Root
          Folder; the *arr moves the file + updates its DB.
        '';
      };
      keepersTv = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Optional keeper library mounted at /keepers/tv (Sonarr twin of keepersMovies).";
      };
    };

    arrMissingSweep = {
      user = lib.mkOption {
        type = lib.types.str;
        default = "root";
        description = ''
          User the weekly *arr missing-sweep runs as. Set it to whichever user
          owns the sops secret holding the *arr API keys.
        '';
      };
    };

    # ── mergerfs pools ────────────────────────────────────────────────────────
    # The pool DEFINITIONS live in the consumer's flake (they are values:
    # which branches, which policy); the shared option plumbing and the
    # reasoning behind it live in mergerfs-pools.nix.
    pools = lib.mkOption {
      default = { };
      description = "MergerFS pools to assemble. See mergerfs-pools.nix.";
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          mountpoint = lib.mkOption {
            type = lib.types.str;
            example = "/mnt/media";
            description = "Where the pooled filesystem mounts.";
          };
          branches = lib.mkOption {
            type = lib.types.str;
            example = "/mnt/disks/media-*";
            description = "Glob of member-branch mountpoints (mergerfs device string).";
          };
          createPolicy = lib.mkOption {
            type = lib.types.enum [ "mfs" "epmfs" "ff" "lfs" "rand" ];
            default = "mfs";
            description = ''
              Where NEW files land. `mfs` (most free space) balances writes.
              `epmfs` (existing path, most free space) keeps new files on the
              branch that already holds their parent directory — required if
              anything hardlinks across the tree (e.g. rsync --link-dest),
              because mergerfs hardlinks only function within a single branch.
            '';
          };
          minFreeSpace = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "100G";
            description = ''
              Reserve headroom: mergerfs won't place a NEW file on a branch
              with less than this free. Must exceed your largest single file
              so a create never hits ENOSPC mid-write — the 4 GiB default
              once let a mirror job fill a branch until a large temp file no
              longer fit. Hardlinks and growth of existing files are
              unaffected (link() co-locates with its target regardless).
            '';
          };
          fsname = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Optional fsname= shown in df/mount output.";
          };
          # The two below feed pool-autoremount; leave members empty if you
          # don't run it (the pool still mounts fine without them).
          memberDir = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "/mnt/disks";
            description = "Directory the member branches mount under (for the auto-remounter).";
          };
          members = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            example = [ "D1" "D2" ];
            description = "Member subdirectory names under memberDir (for the auto-remounter).";
          };
        };
      });
    };

    # ── drive-temps exporter ──────────────────────────────────────────────────
    driveTemps = {
      metricPrefix = lib.mkOption {
        type = lib.types.str;
        default = "drive_";
        description = ''
          Prefix for the exported metric names (<prefix>temp_celsius etc.).
          Keep whatever you already dashboard/alert on if migrating.
        '';
      };
      spindownDriveIds = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = ''
          /dev/disk/by-id names of drives that are allowed to spin down AND
          whose USB bridges misreport power state (so `smartctl -n standby`
          would wake them). These are SMART-read only while doing block I/O;
          an idle drive makes ~no heat, so there's nothing to monitor anyway.
        '';
      };
    };

    # ── monitoring stack ──────────────────────────────────────────────────────
    monitoring = {
      enable = lib.mkEnableOption "the Prometheus + Grafana + Alertmanager stack";

      extraScrapeConfigs = lib.mkOption {
        type = lib.types.listOf lib.types.attrs;
        default = [ ];
        description = "Additional Prometheus scrape configs (site-specific exporters).";
      };

      extraAlertmanagerRoutes = lib.mkOption {
        type = lib.types.listOf lib.types.attrs;
        default = [ ];
        description = ''
          Additional Alertmanager routes (matched before the catch-all). The
          "nights" mute time interval is available to reference.
        '';
      };

      extraAlertRuleFiles = lib.mkOption {
        type = lib.types.listOf lib.types.path;
        default = [ ];
        description = ''
          Extra Grafana alert-rule files (provisioning format, {apiVersion,
          groups}) merged after the library's generic rules. Site-specific
          rules — anything whose expressions reference your own exporters —
          live in your flake and merge in here.
        '';
      };

      extraDatasources = lib.mkOption {
        type = lib.types.listOf lib.types.attrs;
        default = [ ];
        description = "Additional Grafana datasources (site-specific).";
      };

      extraPlugins = lib.mkOption {
        type = lib.types.listOf lib.types.package;
        default = [ ];
        description = "Additional declarative Grafana plugins.";
      };

      alertWebhookUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:9095/alert";
        description = ''
          Webhook that both Alertmanager and Grafana alerting deliver to —
          typically a small local shim that forwards to ntfy.
        '';
      };

      grafanaOidcSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = ''
          Path to the plaintext OIDC client secret for Grafana's generic_oauth
          (e.g. a sops secret path). When set, a "Sign in with SSO" button is
          added, pointing at auth.<domain> (Authelia-style endpoints); the
          matching pbkdf2 HASH belongs in homelab.authelia.oidcClients.
          null disables OIDC login (anon viewer + admin form remain).
        '';
      };
    };

    # ── deploy-drift watch ────────────────────────────────────────────────────
    deployDriftWatch = {
      enable = lib.mkEnableOption "the forge-vs-deployed drift watcher";
      repoUrl = lib.mkOption {
        type = lib.types.str;
        example = "https://git.example.com/me/flakes.git";
        description = "The flake repo the GitOps applier deploys from.";
      };
      branch = lib.mkOption {
        type = lib.types.str;
        default = "main";
        description = "Branch the applier deploys.";
      };
      cominMetricsUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:4243/metrics";
        description = "comin's metrics endpoint (source of the deployed commit id).";
      };
    };

    # ── Authelia SSO ──────────────────────────────────────────────────────────
    authelia = {
      enable = lib.mkEnableOption "Authelia forward-auth + OIDC SSO";

      displayName = lib.mkOption {
        type = lib.types.str;
        default = "Homelab";
        description = "WebAuthn display name shown during passkey enrolment.";
      };

      protectedVhosts = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "prometheus" "glances" ];
        description = ''
          Bare subdomain names to put behind forward-auth (two-factor). ONE
          list drives BOTH the nginx auth_request wiring and the Authelia
          access-control rule — they must always agree, and making them two
          separate edits is exactly how a vhost ends up with the auth hook
          but no rule (result: a bare 403 instead of a login redirect).
        '';
      };

      oidcClients = lib.mkOption {
        type = lib.types.listOf lib.types.attrs;
        default = [ ];
        description = ''
          Authelia OIDC client definitions, passed through verbatim to
          identity_providers.oidc.clients. Client secrets in these attrsets
          must be one-way HASHES (authelia crypto hash generate pbkdf2) —
          the plaintext belongs in the consuming app's sops secret.
        '';
      };
    };
  };
}
