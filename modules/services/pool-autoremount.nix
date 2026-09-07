# pool-autoremount — self-healing remount for storage pool members that drop
# off the USB bus. Covers every pool in homelab.pools that declares members.
#
# Members are typically USB externals that fall off the bus under load. A
# `nofail` mount goes inactive rather than "failed", so a unit-failure alert
# never sees it. This reconciler runs every 2 min, detects a missing member,
# and remounts it.
#
# A member can drop in one of TWO shapes, and only the first self-heals with a
# plain `systemctl start`:
#   1. clean  — the mount goes away; the mount table says no.
#   2. zombie — XFS shuts the filesystem down but the mount ENTRY SURVIVES.
#               /proc/mounts still lists it, `mountpoint` still says yes, and
#               statfs(2) still returns the cached superblock — so `df` and
#               node_exporter both report it present and fine. Only real I/O
#               (readdir/read) returns EIO. Nothing can mount over the corpse,
#               so `systemctl start` fails forever with "device not ready".
#
# Shape 2 is not hypothetical: it once produced 260 consecutive failed
# recovery attempts over 9 hours, took down every service with data on the
# pool, and never alerted — because the can't-fix path delegated to a
# dashboard rule that counted mount entries, and the zombie kept the count
# pinned at full strength. node_filesystem_device_error stayed 0 the whole
# time (statfs never errored). No node_exporter-derived metric can see this
# shape; hence this module probes with real I/O and publishes its own health
# metric.
#
# Why the backup pool matters as much as the working pool: after re-cabling,
# drives can enumerate fine and yet NONE mount — hot-plugging after boot does
# not trigger fstab mounts. mergerfs then falls back to the bare branch
# directories on the ROOT filesystem, and the pool silently reports the root
# disk's size instead of the array's. (mergerfs picks up a branch mounted
# underneath it live, so remounting the member is sufficient; the pool mount
# does not need touching.)
#
# Safety model:
#  - It calls `systemctl start <mount>` (mounting replays the XFS log — the
#    designed, non-destructive recovery), `touch`, and — only for a confirmed
#    zombie — `umount -l`. It NEVER runs xfs_repair or any destructive
#    command. If the filesystem is too damaged to mount, the start fails and
#    the drive is left down, with an escalation push rather than silence.
#  - `umount -l` is reached ONLY when the member is mounted AND a real read
#    returned a definite error. An ambiguous read (timeout — could be a slow
#    spin-up rather than a dead device) is explicitly NOT unmounted: never
#    take a destructive action on an ambiguous signal.
#  - Health is probed with `ls -A` (readdir), not `mountpoint`/statfs — the
#    zombie shape satisfies both of the cheap checks while every actual read
#    returns EIO.
#  - "Is it mounted?" is answered by `findmnt` (/proc/self/mountinfo), never
#    by `mountpoint`: `mountpoint` STATS the path, and on a shut-down XFS that
#    stat itself returns EIO — the detector would be defeated by the very
#    fault it was written to catch (this happened; findmnt reads a pure
#    kernel table and stays truthful when every I/O is failing).
#  - Write-test gate: success is only declared after recreating the
#    `.pool-member` sentinel succeeds, proving the remount is writable.
#  - Flap cap: at most `maxPerDay` auto-remounts per drive per rolling 24 h.
#    Past that it stops remounting — a disk that keeps dropping is failing
#    hardware, and silently remounting it would mask the warning.
#  - Notifications: `low` priority on a successful auto-remount (never wakes
#    anyone), and `default` priority ONCE per episode after `alertAfter`
#    consecutive failed recoveries. The escalation fires on the transition
#    only, not every 2 min. Delegating the can't-fix case to a dashboard rule
#    is what once produced 9 h of silence; this module reports its own
#    failures.
#  - It publishes `pool_member_healthy{pool,member}` to the node_exporter
#    textfile collector, derived from the same real-I/O probe. This is the
#    only trustworthy pool-health signal.
#  - Maintenance: `touch /run/pool-autoremount.hold` to pause without
#    stopping the timer (e.g. when intentionally unmounting a drive).
{ config, lib, pkgs, ... }:

let
  notify = import ../lib/notify.nix { inherit pkgs; url = config.homelab.ntfy.url; };

  # Pools come from homelab.pools — any pool that declares members is covered.
  # The mount-unit prefix is the systemd path-escape of the member directory.
  unitPrefixOf = dir: lib.replaceStrings [ "/" ] [ "-" ] (lib.removePrefix "/" dir);
  pools = lib.mapAttrsToList
    (id: p: {
      inherit id;
      base = p.memberDir;
      unitPrefix = unitPrefixOf p.memberDir;
      poolMount = p.mountpoint;
      members = p.members;
    })
    (lib.filterAttrs (_: p: p.members != [ ] && p.memberDir != null) config.homelab.pools);

  maxPerDay = 3;        # flap cap: auto-remounts per drive per rolling 24 h
  mountTimeout = 240;   # seconds allowed for a mount (covers XFS log replay)
  readTimeout = 20;     # seconds a mounted member gets to answer a readdir
  alertAfter = 4;       # consecutive failed recoveries (~8 min) before escalating

  # Shared with drive-temps; node_exporter reads *.prom from here.
  textfileDir = "/var/lib/node-exporter-textfile";

  # Flatten to "pool|base|unitPrefix|poolMount|drive" tokens so the shell loop
  # stays a plain `for` (no while-read, which competes for stdin with systemctl).
  entries = lib.concatMap
    (p: map (m: "${p.id}|${p.base}|${p.unitPrefix}|${p.poolMount}|${m}") p.members)
    pools;

  pool-autoremount = pkgs.writeShellApplication {
    name = "pool-autoremount";
    runtimeInputs = [ pkgs.util-linux pkgs.systemd pkgs.coreutils notify ];
    text = ''
      STATE=/var/lib/pool-autoremount
      mkdir -p "$STATE"

      if [ -e /run/pool-autoremount.hold ]; then
        echo "maintenance hold present (/run/pool-autoremount.hold) — skipping"
        exit 0
      fi

      now=$(date +%s)
      window=$(( 24 * 3600 ))

      # --- health metric ------------------------------------------------
      # Accumulated during the sweep and published atomically at exit, so
      # node_exporter never reads a half-written file. Skipped entirely when
      # nothing was probed (e.g. the maintenance-hold early exit), so a hold
      # leaves the last good sample in place instead of blanking the series.
      METRIC_TMP=""
      METRIC_N=0
      if [ -d "${textfileDir}" ]; then
        if METRIC_TMP=$(mktemp "${textfileDir}/.pool-autoremount.prom.XXXXXX"); then
          {
            echo "# HELP pool_member_healthy 1 if the pool member is mounted and answers real I/O, 0 otherwise."
            echo "# TYPE pool_member_healthy gauge"
          } > "$METRIC_TMP"
        else
          METRIC_TMP=""
        fi
      fi

      # Emit EXACTLY ONE sample per member per run: Prometheus rejects a
      # textfile with duplicate name+label pairs, and one bad file fails the
      # whole textfile collector. So this is called only at terminal outcomes —
      # healthy (1), successful remount (1), or note_failure (0) — never
      # speculatively on the way into a recovery attempt.
      record_health() {  # record_health <pool> <member> <0|1>
        [ -n "$METRIC_TMP" ] || return 0
        printf 'pool_member_healthy{pool="%s",member="%s"} %s\n' "$1" "$2" "$3" >> "$METRIC_TMP"
        METRIC_N=$(( METRIC_N + 1 ))
      }

      publish_health() {
        [ -n "$METRIC_TMP" ] || return 0
        if [ "$METRIC_N" -gt 0 ]; then
          chmod 0644 "$METRIC_TMP"
          # Same-directory rename => atomic swap for node_exporter's reader.
          mv -f "$METRIC_TMP" "${textfileDir}/pool-autoremount.prom" || rm -f "$METRIC_TMP"
        else
          rm -f "$METRIC_TMP"
        fi
      }
      trap publish_health EXIT

      # See the header: `mountpoint -q` stats the path and can itself return
      # EIO on a shut-down XFS; findmnt parses /proc/self/mountinfo, a pure
      # kernel table, and never touches the filesystem.
      is_mounted() {  # is_mounted <path>
        findmnt -rno TARGET "$1" > /dev/null 2>&1
      }

      # After `umount -l` the kernel may still hold the shut-down superblock:
      # the unmount is LAZY — it detaches the mountpoint but defers releasing
      # the superblock until the last reference drops, and the mergerfs process
      # holding the branch is enough to keep it alive. XFS then refuses to
      # mount the very same filesystem again:
      #     XFS (sdX1): Filesystem has duplicate UUID <uuid> - can't mount
      # That is neither corruption nor a real collision, and `-o nouuid` is the
      # documented remedy. Source and type come from FSTAB (`findmnt -s`), not
      # from `systemctl show -p What`: for an active unit the latter reports
      # the RESOLVED device (/dev/sdX1) rather than the stable by-label/by-uuid
      # path, and after a re-enumeration that letter is exactly what went
      # stale. The fstab entry always names the persistent symlink.
      #
      # ⚠️ The zombie gate alone is too narrow. A member can leave a pinned
      # superblock behind WITHOUT ever becoming a zombie: a clean drop off USB
      # (0 mountinfo entries), a re-enumeration under a new letter, and the
      # kernel STILL holds the old superblock — so the plain mount is refused
      # forever while the reconciler retries every 2 minutes. Case (b) below
      # detects that from the kernel's own refusal message.
      #
      # The gate exists for a real reason — never `-o nouuid` against a
      # GENUINELY duplicated filesystem, because that can mount the wrong
      # device. So the check that actually discriminates: does that UUID
      # appear on more than one block device? Exactly ONCE means the
      # "duplicate" the kernel complains about is its own stale in-memory
      # superblock — the case `-o nouuid` is for. Twice means a real collision
      # and we must NOT touch it.
      uuid_is_unique() {  # uuid_is_unique <device>  -> 0 if exactly one holder
        local u n
        u=$(blkid -s UUID -o value "$1" 2>/dev/null || true)
        [ -n "$u" ] || return 1          # no UUID readable -> fail closed
        n=$(blkid -t "UUID=$u" -o device 2>/dev/null | wc -l)
        [ "$n" -eq 1 ]
      }

      # Did the kernel actually refuse THIS device for duplicate UUID just now?
      # Belt and braces: without this we could nouuid-mount after some
      # unrelated mount failure. Scoped to this boot and the last few minutes.
      kernel_said_duplicate_uuid() {  # kernel_said_duplicate_uuid <device>
        local base
        base=$(basename "$(readlink -f "$1" 2>/dev/null || echo "$1")")
        journalctl -k -b --since "-5min" --no-pager 2>/dev/null \
          | grep -q "XFS ($base): Filesystem has duplicate UUID"
      }

      try_nouuid_mount() {  # try_nouuid_mount <pool> <member> <mp>
        local src fstype
        src=$(findmnt -sn -o SOURCE "$3" 2>/dev/null || true)
        fstype=$(findmnt -sn -o FSTYPE "$3" 2>/dev/null || true)
        [ -n "$src" ] || return 1
        [ "$fstype" = "xfs" ] || return 1
        [ -e "$src" ] || return 1
        if ! uuid_is_unique "$src"; then
          echo "$1/$2: REFUSING '-o nouuid' — that UUID is present on MORE THAN ONE block device, so this is a genuine collision, not a stale kernel superblock. Mounting could attach the wrong filesystem." >&2
          return 1
        fi
        echo "$1/$2: the kernel is holding a stale superblock for $src (UUID unique to this device); retrying with '-o nouuid'"
        mount -t xfs -o nouuid "$src" "$3"
      }

      # Count a failed recovery and escalate exactly once per episode. The
      # counter is cleared whenever the member is healthy again, so a new
      # outage gets a new notification.
      # Every give-up path funnels through here, so this is also the single
      # place an unhealthy sample is recorded — see the note on record_health.
      note_failure() {  # note_failure <pool> <member> <failfile> <reason>
        record_health "$1" "$2" 0
        n=1
        if [ -f "$3" ]; then n=$(( $(cat "$3" 2>/dev/null || echo 0) + 1 )); fi
        echo "$n" > "$3"
        echo "$1/$2: $4 (consecutive failed recoveries: $n)"
        if [ "$n" -eq ${toString alertAfter} ]; then
          notify "Pool member DOWN — auto-recovery is failing" \
            "$1/$2 has resisted $n consecutive automatic recovery attempts. Reason: $4. The pool is degraded and this needs hands — check 'journalctl -u pool-autoremount'." \
            default "warning,floppy_disk" || true
        fi
      }

      for entry in ${lib.escapeShellArgs entries}; do
        IFS='|' read -r pool base unitPrefix poolMount d <<< "$entry"

        mp="$base/$d"
        unit="$unitPrefix-$d.mount"
        log="$STATE/$pool-$d.remounts"
        fails="$STATE/$pool-$d.failures"

        # --- health gate ---------------------------------------------------
        # A mount entry is NOT proof the filesystem works (see the zombie case
        # in the header), so probe with a real readdir rather than trusting
        # `mountpoint`. Three outcomes, only one of which is destructive:
        #   healthy — mounted and answers   -> nothing to do
        #   zombie  — mounted, definite I/O error -> clear it, then remount
        #   slow    — mounted, no answer in time  -> AMBIGUOUS, leave it alone
        # Tracks whether WE cleared a zombie for this member in this run; gates
        # the `-o nouuid` fallback below.
        cleared_zombie=0

        if is_mounted "$mp"; then
          # Capture the probe status explicitly: `rc=$?` after a bare `if`
          # would read 0, because an `if` whose branch is not taken exits zero.
          rc=0
          timeout ${toString readTimeout} ls -A "$mp" > /dev/null 2>&1 || rc=$?
          if [ "$rc" -eq 0 ]; then
            rm -f "$fails"
            record_health "$pool" "$d" 1
            continue
          fi
          if [ "$rc" -eq 124 ]; then
            # Could be a drive spinning up under load rather than a dead one.
            # Tearing down a merely-slow member would cause the very outage
            # this module exists to prevent, so back off and retry next run.
            note_failure "$pool" "$d" "$fails" \
              "mounted but did not answer a readdir within ${toString readTimeout}s — ambiguous (slow vs dead), NOT unmounting"
            continue
          fi
          echo "$pool/$d: ZOMBIE MOUNT — $mp is mounted but unreadable (rc=$rc); the filesystem was shut down under a live mount. Clearing with 'umount -l' so it can be remounted."
          if ! umount -l "$mp"; then
            note_failure "$pool" "$d" "$fails" \
              "zombie mount detected but 'umount -l' failed — cannot recover automatically"
            continue
          fi
          cleared_zombie=1
          echo "$pool/$d: zombie mount cleared — proceeding to remount"
        fi

        echo "$pool/$d: $mp is NOT mounted — evaluating auto-remount"

        # Flap cap: keep only successful-remount timestamps from the last 24 h.
        recent=0
        if [ -f "$log" ]; then
          tmp=$(mktemp)
          while read -r ts; do
            [ -n "$ts" ] || continue
            if [ $(( now - ts )) -lt "$window" ]; then
              echo "$ts" >> "$tmp"
              recent=$(( recent + 1 ))
            fi
          done < "$log"
          mv "$tmp" "$log"
        fi

        if [ "$recent" -ge ${toString maxPerDay} ]; then
          note_failure "$pool" "$d" "$fails" \
            "flap cap reached ($recent auto-remounts in 24 h) — refusing to remount again; this drive is failing"
          continue
        fi

        # Warn (journal only) if the bare mountpoint accumulated files during
        # the outage — mergerfs may have written onto the root filesystem;
        # those get shadowed by the mount and should be cleaned up manually.
        if [ -n "$(ls -A "$mp" 2>/dev/null)" ]; then
          echo "$pool/$d: WARNING — $mp is non-empty while unmounted; stray files may have landed on the ROOT fs and will be shadowed by the remount"
        fi

        echo "$pool/$d: attempting 'systemctl start $unit'"
        nouuid_used=0
        if ! timeout ${toString mountTimeout} systemctl start "$unit"; then
          # A pinned superblock arises in TWO ways, not one:
          #   (a) a zombie we just cleared with `umount -l`  (cleared_zombie=1)
          #   (b) a clean drop + re-enumeration, where the kernel kept the old
          #       superblock anyway — no zombie ever existed
          # Case (b) is detected from the kernel's own refusal.
          # try_nouuid_mount still refuses if the UUID is on more than one
          # device.
          src_now=$(findmnt -sn -o SOURCE "$mp" 2>/dev/null || true)
          if { [ "$cleared_zombie" -eq 1 ] \
               || { [ -n "$src_now" ] && kernel_said_duplicate_uuid "$src_now"; }; } \
             && try_nouuid_mount "$pool" "$d" "$mp"; then
            nouuid_used=1
          else
            # Deliberately does NOT claim the device is unreadable: that
            # wording once sent a reader chasing a healthy drive when the real
            # blocker was a mount the kernel had refused.
            note_failure "$pool" "$d" "$fails" \
              "'systemctl start $unit' failed — the device may be absent, or the kernel refused the mount; check 'journalctl -k' for its reason before assuming bad hardware"
            continue
          fi
        fi

        if ! is_mounted "$mp"; then
          note_failure "$pool" "$d" "$fails" \
            "start returned 0 but $mp is still not a mountpoint"
          continue
        fi

        # Write-test + restore the sentinel any backup preflights check.
        if ! touch "$mp/.pool-member" 2>/dev/null; then
          systemctl stop "$unit" || true
          note_failure "$pool" "$d" "$fails" \
            "remounted but NOT writable — unmounted again and backing off"
          continue
        fi

        rm -f "$fails"
        record_health "$pool" "$d" 1
        echo "$now" >> "$log"
        count=$(( recent + 1 ))
        # A `-o nouuid` recovery is NOT equivalent to a normal one: it is a
        # manual mount outside the unit, and the pinned corpse survives until
        # reboot, so any further drop of this member will fail the plain fstab
        # mount the same way. Say so rather than reporting a clean heal.
        nouuid_note=""
        if [ "$nouuid_used" -eq 1 ]; then
          nouuid_note=" NOTE: recovered with '-o nouuid' because the previous superblock was still pinned — this is a manual mount and the stale superblock persists until a REBOOT, so another drop before then will not self-heal."
          echo "$pool/$d: recovered via '-o nouuid' — a reboot is needed to clear the stale superblock"
        fi

        echo "$pool/$d: auto-remounted OK (occurrence $count of ${toString maxPerDay} in 24 h)"
        notify "Pool drive auto-remounted" \
          "$pool/$d ($mp) dropped off the bus and was automatically remounted — occurrence $count of ${toString maxPerDay} allowed in 24 h. $poolMount is whole again.$nouuid_note" \
          low "floppy_disk,white_check_mark" || true
      done
    '';
  };
in
{
  imports = [ ../options.nix ];

  config = lib.mkIf (entries != [ ]) {
    environment.systemPackages = [ pool-autoremount ];

    systemd.services.pool-autoremount = {
      description = "Auto-remount storage pool members that dropped off the bus";
      after = [ "local-fs.target" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${pool-autoremount}/bin/pool-autoremount";
      };
    };

    systemd.timers.pool-autoremount = {
      description = "Periodic storage pool auto-remount check";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "3min";
        OnUnitActiveSec = "2min";
        AccuracySec = "30s";
      };
    };

    systemd.tmpfiles.rules = [
      "d /var/lib/pool-autoremount 0755 root root - -"
    ];
  };
}
