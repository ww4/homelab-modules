# backup — restic snapshots of the small, irreplaceable state: a local
# repository on the storage pool (fast restores, survives the system disk)
# and an optional offsite one (survives fire, theft, ransomware). Both jobs
# take the same paths, the same passphrase and the same retention, so the two
# repositories are interchangeable at restore time.
#
# Scope is deliberately "critical tier": application state, databases,
# keys, documents. Bulk media is a different tier with a different tool
# (an rsync mirror to a second pool), not a restic job — restic's dedup and
# encryption are wasted on terabytes of video, and the prune would take days.
#
# Values a consumer sets (see options.nix, `homelab.backup`):
#   paths / exclude          what the critical tier is
#   passwordFile             the repository passphrase — restic has no
#                            recovery; store a copy OFF this machine
#   local.repository         a directory on the pool; requiresMountsFor
#                            keeps restic from writing into a bare
#                            mountpoint when the pool failed to mount
#   remote.repository        a restic backend URL (b2:, s3:, sftp:, rest:)
#                            with its credentials in environmentFile as the
#                            backend's restic environment variables
#
# Optional second machine (`sftpPush`): another host pushes its own restic
# snapshots over SFTP into the local repository, so one repo holds both. The
# repository is made group-writable for a dedicated system user whose
# PRIMARY group is `restic`. That detail is load-bearing: on a mergerfs/FUSE
# pool with default_permissions on kernel 6.x, supplementary groups are not
# honoured, so adding the admin to the group would not be enough. A
# oneshot service re-applies the group, setgid bits and a default ACL on
# every activation so files the push creates stay reachable by root's own
# jobs and vice versa.
#
# Restore drill (do it once, before you need it):
#   restic -r <local.repository> --password-file <passwordFile> snapshots
#   restic -r <local.repository> --password-file <passwordFile> \
#     restore latest --target /tmp/restore-test --include <one small path>
{ config, lib, pkgs, utils, ... }:

let
  cfg = config.homelab.backup;

  pruneOpts = [
    "--keep-daily ${toString cfg.keep.daily}"
    "--keep-weekly ${toString cfg.keep.weekly}"
    "--keep-monthly ${toString cfg.keep.monthly}"
  ];

  job = extra: {
    passwordFile = cfg.passwordFile;
    paths = cfg.paths;
    exclude = cfg.exclude;
    initialize = true;
    inherit pruneOpts;
    checkOpts = cfg.checkOpts;
  } // extra;

  mountUnits = map (m: "${utils.escapeSystemdPath m}.mount") cfg.local.requiresMountsFor;
in
{
  config = {
    assertions = [
      {
        assertion = cfg.local.enable || cfg.remote.enable;
        message = "homelab.backup: enable at least one of local or remote.";
      }
      {
        assertion = !cfg.local.enable || cfg.local.repository != null;
        message = "homelab.backup.local.repository must be set when local backups are enabled.";
      }
      {
        assertion = !cfg.remote.enable || cfg.remote.repository != null;
        message = "homelab.backup.remote.repository must be set when remote backups are enabled.";
      }
      {
        assertion = !cfg.sftpPush.enable || cfg.local.enable;
        message = "homelab.backup.sftpPush needs the local repository (that is what it pushes into).";
      }
    ];

    # restic on the PATH for manual restore and inspection.
    environment.systemPackages = [ pkgs.restic ];

    services.restic.backups = lib.mkMerge [
      (lib.mkIf cfg.local.enable {
        ${cfg.local.name} = job {
          repository = cfg.local.repository;
          timerConfig = { OnCalendar = cfg.local.onCalendar; Persistent = true; };
        };
      })
      (lib.mkIf cfg.remote.enable {
        ${cfg.remote.name} = job ({
          repository = cfg.remote.repository;
          timerConfig = { OnCalendar = cfg.remote.onCalendar; Persistent = true; };
        } // lib.optionalAttrs (cfg.remote.environmentFile != null) {
          environmentFile = cfg.remote.environmentFile;
        });
      })
    ];

    # ⚠️ ORDERING ALONE DOES NOT COVER A BACKUP STARTED BY HAND.
    #
    # restic-repo-perms declares itself `before` each backup, which settles
    # the boot transaction: when both are starting, permissions go first. It
    # says nothing about `systemctl start restic-backups-…`, which is how a
    # person tests a backup, and that start does not pull the permissions
    # unit in at all. A file written then keeps the wrong group and the SFTP
    # push cannot read it.
    #
    # `wants`, not `requires`: a backup that runs with imperfect group
    # permissions beats a backup that does not run, and the permissions unit
    # is RemainAfterExit, so this costs nothing after the first start.
    #
    # One definition per job, because Nix refuses a dynamic attribute name
    # twice even on different sub-paths.
    systemd.services."restic-backups-${cfg.local.name}" = {
      # Never write into a bare mountpoint because the pool failed to mount.
      unitConfig.RequiresMountsFor =
        lib.mkIf (cfg.local.enable && cfg.local.requiresMountsFor != [ ]) cfg.local.requiresMountsFor;
      after = lib.mkIf (cfg.sftpPush.enable && cfg.local.enable) [ "restic-repo-perms.service" ];
      wants = lib.mkIf (cfg.sftpPush.enable && cfg.local.enable) [ "restic-repo-perms.service" ];
    };
    systemd.services."restic-backups-${cfg.remote.name}" = {
      after = lib.mkIf (cfg.sftpPush.enable && cfg.remote.enable) [ "restic-repo-perms.service" ];
      wants = lib.mkIf (cfg.sftpPush.enable && cfg.remote.enable) [ "restic-repo-perms.service" ];
    };

    # ── the SFTP push target ───────────────────────────────────────────────
    users.groups = lib.mkIf cfg.sftpPush.enable { restic = { }; };

    users.users = lib.mkIf cfg.sftpPush.enable {
      ${cfg.sftpPush.user} = {
        isSystemUser = true;
        group = "restic";                      # PRIMARY group — see the header
        home = "/var/lib/${cfg.sftpPush.user}";
        createHome = true;
        shell = pkgs.bashInteractive;          # nologin breaks SFTP via PAM
        openssh.authorizedKeys.keys = cfg.sftpPush.authorizedKeys;
      };
      # The admin can inspect the repo by hand (`sg restic -c "restic snapshots"`);
      # supplementary membership is fine for a direct shell, just not over FUSE.
      ${config.homelab.adminUser}.extraGroups = [ "restic" ];
    };

    # Own the repository permissions idempotently on every activation, after
    # the pool is mounted so the target exists.
    systemd.services.restic-repo-perms = lib.mkIf cfg.sftpPush.enable {
      description = "Apply group + ACL perms on the restic repo for the SFTP push";
      wantedBy = [ "multi-user.target" ];
      after = mountUnits;
      requires = mountUnits;
      # Ownership and the default ACL must be in place before anything can
      # create a file in the repository, or an entry written first keeps the
      # wrong group and the SFTP push cannot read it. Ordering only, so a
      # backup is not blocked if this unit is absent.
      before = lib.optional cfg.local.enable "restic-backups-${cfg.local.name}.service"
        ++ lib.optional cfg.remote.enable "restic-backups-${cfg.remote.name}.service";
      unitConfig.RequiresMountsFor = lib.mkIf (cfg.local.requiresMountsFor != [ ]) cfg.local.requiresMountsFor;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      path = with pkgs; [ coreutils acl findutils ];
      script = ''
        repo=${lib.escapeShellArg cfg.local.repository}
        [ -d "$repo" ] || exit 0
        chgrp -R restic "$repo"
        find "$repo" -type d -exec chmod 2770 {} +
        find "$repo" -type f -exec chmod 0660 {} +
        # config is conventionally read-only after init — keep group read.
        [ -f "$repo/config" ] && chmod 0640 "$repo/config" || true
        # Default ACL so any new entry (by any process) inherits group rwX.
        setfacl -R -d -m g:restic:rwX "$repo"
      '';
    };
  };
}
