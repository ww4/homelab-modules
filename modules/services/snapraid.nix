# snapraid — file-level parity for a MergerFS pool's member disks.
#
# How it composes with mergerfs:
#
#   /mnt/disks/D1  xfs  ┐
#   /mnt/disks/D2  xfs  ├─→ mergerfs → /mnt/media   (apps see this)
#   /mnt/disks/D3  xfs  ┘
#   /mnt/parity1   xfs  ────→ snapraid.parity       (NOT in the pool)
#
# SnapRAID reads the member disks directly (never through the pool mount)
# and writes parity into one file per parity disk. Any single data disk
# (per parity disk) is recoverable file-by-file with `snapraid fix` once a
# replacement is in place; the surviving disks stay readable throughout.
# It is parity for data that changes slowly — media — not a substitute for
# a backup of anything that changes daily (that is the `backup` module).
#
# The data disks are derived from the pool: `homelab.pools.<pool>.memberDir`
# + `members`, the same values the auto-remounter uses, so adding a member
# to the pool adds it to parity and to the content-file copies in one edit.
#
# ACTIVATION is deliberate (`homelab.snapraid.enable` defaults to false):
#   1. Mount the parity disk(s) — each ≥ the largest member disk.
#   2. Deploy with enable = true; the timers are installed but the FIRST
#      sync must be run by hand (`snapraid sync` as root): it builds parity
#      from scratch and takes hours on a TB-scale array.
#   3. Scrub covers `scrub.plan` percent per run; at the defaults (12 %,
#      weekly) the whole array is verified every ~8 weeks.
#
# Adding a member later: it must be ≤ the parity disk; add it to the pool,
# deploy, `snapraid sync`. Upgrading parity to a bigger disk: copy the
# parity file over (`cp -p`, `cmp`), remount at the same path, `snapraid
# scrub` — the bytes are the same, nothing is recalculated.
#
# Failures surface through the monitoring module's failed-unit rule; there
# is no per-unit onFailure wiring here.
{ config, lib, ... }:

let
  cfg = config.homelab.snapraid;
  pool = config.homelab.pools.${cfg.pool} or null;
  memberPath = m: "${pool.memberDir}/${m}";
in
{
  imports = [ ../options.nix ];

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pool != null;
        message = "homelab.snapraid.pool = \"${cfg.pool}\" is not a pool in homelab.pools.";
      }
      {
        assertion = pool == null || (pool.memberDir != null && pool.members != [ ]);
        message = "homelab.snapraid: pool \"${cfg.pool}\" must declare memberDir and members (the data disks).";
      }
      {
        assertion = cfg.parityFiles != [ ];
        message = "homelab.snapraid.parityFiles must name at least one parity file on a disk outside the pool.";
      }
    ];

    services.snapraid = {
      enable = true;
      # d1 = /mnt/disks/D1 … — the key is the member name, lowercased.
      dataDisks = lib.listToAttrs (map (m: lib.nameValuePair (lib.toLower m) (memberPath m)) pool.members);
      parityFiles = cfg.parityFiles;
      # Content (database) copies: one on persistent local storage plus one
      # per data disk — more copies than parity disks, so no single loss
      # takes the database with it.
      contentFiles = [ "${cfg.contentDir}/snapraid.content" ]
        ++ map (m: "${memberPath m}/snapraid.content") pool.members;
      # The content files themselves are always excluded, last.
      exclude = cfg.exclude ++ cfg.extraExclude ++ [ ".snapraid.content*" ];
      sync.interval = cfg.sync.interval;
      scrub = {
        interval = cfg.scrub.interval;
        plan = cfg.scrub.plan;
        olderThan = cfg.scrub.olderThan;
      };
      touchBeforeSync = cfg.touchBeforeSync;
    };

    systemd.tmpfiles.rules = [ "d ${cfg.contentDir} 0755 root root - -" ];
  };
}
