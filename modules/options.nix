# The homelab.* option set — the single interface between this library and a
# consumer's flake. Implementations read these; the consumer's flake sets them.
# Grown as modules are parameterized; never given personal defaults.
{ config, lib, ... }:

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
        default = "/mnt/media/arr";
        example = "/mnt/media/arr";
        description = "The shared /data tree (downloads + media subdirs). The default sits on the media pool, which is where a disk marked `data` at install is mounted.";
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
        default = config.homelab.adminUser;
        description = "Host user owning the stack's directories (matches puid). Defaults to homelab.adminUser: the account a fresh install is sure to have.";
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
      vpnEnvFile = lib.mkOption {
        type = lib.types.str;
        description = ''
          environmentFile with the WireGuard credentials for gluetun
          (WIREGUARD_PRIVATE_KEY / _PRESHARED_KEY / _ADDRESSES, SERVER_COUNTRIES,
          optionally FIREWALL_VPN_INPUT_PORTS). Read by docker --env-file as
          root; root:0400 is fine.
        '';
      };
      apiKeyEnvFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/run/secrets/arr-api-keys";
        description = ''
          Env file with SONARR_API_KEY / RADARR_API_KEY / PROWLARR_API_KEY
          (and LIDARR_API_KEY if lidarr is imported). When set, each app is
          seeded with its key before first start, so the keys are values the
          configuration owns rather than something copied out of a UI, and
          recyclarr renders its secrets from the same file. Read as root;
          root:0400 is fine. null = each *arr mints its own key on first run.
        '';
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
          owns the secret behind apiEnvFile.
        '';
      };
      apiEnvFile = lib.mkOption {
        type = lib.types.str;
        description = ''
          Shell-sourceable file exporting SONARR_API_KEY and RADARR_API_KEY
          (a sops secret owned by `user`).
        '';
      };
    };

    # ── per-service secret paths (download-stack helpers + meshagent) ────────
    # Each is an environmentFile read by docker --env-file (root:0400) unless
    # the module header says otherwise. The library never declares the sops
    # secret itself — see README "Secrets are yours".
    aurral.envFile = lib.mkOption {
      type = lib.types.str;
      description = "environmentFile with LIDARR_API_KEY.";
    };
    unpackerr.envFile = lib.mkOption {
      type = lib.types.str;
      description = "environmentFile with UN_SONARR_0_API_KEY and UN_RADARR_0_API_KEY.";
    };
    decluttarr.envFile = lib.mkOption {
      type = lib.types.str;
      description = "environmentFile with SONARR_API_KEY and RADARR_API_KEY.";
    };
    meshagent.mshFile = lib.mkOption {
      type = lib.types.str;
      description = ''
        The server-generated .msh identity file (server URL + MeshID + cert
        hash; enrollment-capable, so a sops secret). Read by root at start.
      '';
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
        default = "";
        example = "https://git.example.com/me/flakes.git";
        description = "The flake repo the GitOps applier deploys from. Empty (the default) means there is no repo yet: the check does nothing until you set it.";
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

    # ── snapraid (parity for a pool) ─────────────────────────────────────────
    snapraid = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Parity-protect a MergerFS pool's member disks with SnapRAID. Off by
          default on purpose: the first sync is a manual, hours-long step
          after the parity disk is mounted (see the module header).
        '';
      };
      pool = lib.mkOption {
        type = lib.types.str;
        default = "media";
        example = "media";
        description = "Name of the homelab.pools entry whose memberDir + members are the data disks.";
      };
      parityFiles = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "/mnt/parity1/snapraid.parity" ];
        description = ''
          One parity file per parity disk, each on a disk that is NOT a pool
          member and at least as large as the largest member. One file =
          any one member recoverable; two = any two.
        '';
      };
      contentDir = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/snapraid";
        description = "Persistent local directory for a copy of the content (database) file; every data disk also carries one.";
      };
      exclude = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "*.unrecoverable" "/tmp/" "lost+found/" ".pool-member" ];
        description = "Patterns SnapRAID skips (the pool-member sentinel the auto-remounter writes is here by default).";
      };
      extraExclude = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "/downloads/incomplete/" ];
        description = "Site-specific patterns appended to `exclude` — transient download scratch, caches.";
      };
      sync.interval = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 04:00:00";
        description = "OnCalendar for `snapraid sync` (a no-op when nothing changed; keep it clear of mirror jobs).";
      };
      scrub = {
        interval = lib.mkOption {
          type = lib.types.str;
          default = "Mon *-*-* 05:00:00";
          description = "OnCalendar for `snapraid scrub`.";
        };
        plan = lib.mkOption {
          type = lib.types.ints.between 0 100;
          default = 12;
          description = "Percent of the array verified per scrub run.";
        };
        olderThan = lib.mkOption {
          type = lib.types.ints.unsigned;
          default = 10;
          description = "Skip blocks scrubbed within this many days.";
        };
      };
      touchBeforeSync = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Run `snapraid touch` first so files with zero sub-second timestamps get unique ones (SnapRAID's own recommendation).";
      };
    };

    # ── backup (restic) ───────────────────────────────────────────────────────
    backup = {
      paths = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        example = [ "/var/lib/nextcloud" "/var/backup/postgresql" "/home/alice/Documents" ];
        description = ''
          The critical tier: every path whose loss could not be undone.
          Application state under /var/lib, database dumps, keys, documents.
          Not bulk media — that is a mirror job, not a restic job.
        '';
      };
      exclude = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "/var/lib/jellyfin/transcodes" "/var/lib/jellyfin/cache" ];
        description = "Regenerable subtrees of `paths` (caches, logs, transcode scratch) to leave out.";
      };
      passwordFile = lib.mkOption {
        type = lib.types.str;
        example = "/run/secrets/restic-password";
        description = ''
          File holding the restic repository passphrase, shared by the local
          and remote repositories. restic cannot recover a lost passphrase:
          keep a copy somewhere that is not this machine.
        '';
      };
      keep = {
        daily = lib.mkOption { type = lib.types.ints.positive; default = 7; description = "Daily snapshots to keep."; };
        weekly = lib.mkOption { type = lib.types.ints.positive; default = 4; description = "Weekly snapshots to keep."; };
        monthly = lib.mkOption { type = lib.types.ints.positive; default = 6; description = "Monthly snapshots to keep."; };
      };
      checkOpts = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "--with-cache" ];
        description = "Arguments to `restic check` after each run (structural integrity, using the local cache).";
      };

      local = {
        enable = lib.mkOption {
          type = lib.types.bool;
          default = true;
          description = "Keep a repository on local storage (fast restores; survives the system disk).";
        };
        name = lib.mkOption {
          type = lib.types.str;
          default = "critical-local";
          description = "Job name: the unit is restic-backups-<name>.";
        };
        repository = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = "/mnt/pool/restic";
          description = "Directory of the local repository, normally on a storage pool rather than the system disk.";
        };
        onCalendar = lib.mkOption {
          type = lib.types.str;
          default = "02:30";
          description = "systemd OnCalendar for the local job (missed runs are caught up).";
        };
        requiresMountsFor = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
          example = [ "/mnt/pool" ];
          description = ''
            Mountpoints that must be mounted before the local job (and the
            SFTP-push permission service) may run — so a pool that failed
            to mount yields a skipped run, not a repository written into the
            bare mountpoint on the system disk.
          '';
        };
      };

      remote = {
        enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Also push to an offsite repository (survives fire, theft and ransomware).";
        };
        name = lib.mkOption {
          type = lib.types.str;
          default = "critical-remote";
          description = "Job name: the unit is restic-backups-<name>.";
        };
        repository = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = "b2:my-bucket";
          description = "A restic backend URL: b2:, s3:, azure:, gs:, sftp:, rest:.";
        };
        environmentFile = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = "/run/secrets/restic-remote-env";
          description = ''
            File of the backend's credentials as restic environment variables
            (for B2: B2_ACCOUNT_ID and B2_ACCOUNT_KEY; for S3:
            AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY). Null for backends
            that need none, such as sftp: with a key.
          '';
        };
        onCalendar = lib.mkOption {
          type = lib.types.str;
          default = "03:00";
          description = "systemd OnCalendar for the remote job (missed runs are caught up).";
        };
      };

      sftpPush = {
        enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = ''
            Let a second machine push its own restic snapshots over SFTP
            into the local repository (one repo for the household). Creates
            a dedicated system user whose primary group owns the repository.
          '';
        };
        user = lib.mkOption {
          type = lib.types.str;
          default = "restic-push";
          description = "Name of the SFTP-only system user the other machine logs in as.";
        };
        authorizedKeys = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
          example = [ ''restrict,command="internal-sftp" ssh-ed25519 AAAA... backup@otherbox'' ];
          description = ''
            The pushing machine's public keys. Prefix each with
            restrict,command="internal-sftp" so the key can do nothing but
            SFTP, whatever the client asks for.
          '';
        };
      };
    };
  };
}
