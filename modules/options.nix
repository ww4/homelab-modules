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
        };
      });
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
