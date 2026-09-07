# disk-io-watch — count kernel I/O errors and USB resets per device, publish
# them as Prometheus counters, and alert on a device that starts failing.
#
# WHY THIS EXISTS. A USB-attached pool member here once logged 1,762
# `DID_ERROR` reads in ~10 h — a real, sustained link fault — and NOTHING on
# the box noticed. There were zero USB bus resets, because the block layer
# retried and mostly won, so nothing escalated to the events the mount
# watchdog and the health checks watch for. The pool-health gauge read 1 (it
# comes from a readdir, and readdirs succeeded), `df` was happy, and there
# were no failed units. The only thing that could find it was a hand-run
# `journalctl -k -b | grep -c DID_ERROR`.
#
# That is the quiet shape of the fault that later becomes a hard drop or a
# zombie mount. No check anywhere counted kernel I/O errors per device, so this
# module does exactly that and nothing else.
#
# METRICS (node_exporter textfile collector):
#   disk_io_errors_total{serial}      block-layer "I/O error, dev sdX, sector N"
#   disk_scsi_errors_total{serial}    SCSI "hostbyte=DID_ERROR" / "Result: ..."
#   usb_device_resets_total{port}     "usb 6-2: reset SuperSpeed USB device"
#   disk_device_info{device,serial}   current letter<->serial map (informational)
#   disk_io_watch_last_run_seconds    liveness — a stalled watcher is not silence
#
# The counters are true counters, reset to 0 on reboot, which is what Prometheus
# expects. They are accumulated from the journal with a CURSOR, not by
# re-scanning `-b` every run: a full-boot scan costs more every hour the box
# stays up, and this runs every 5 minutes.
#
# ⚠️ COUNTERS ARE KEYED BY SERIAL, NOT BY KERNEL DEVICE NAME — and that is not a
# nicety. Kernel names are not stable: a USB drive that drops and re-enumerates
# comes back as a different `sdX`, and it can inherit a letter another drive just
# vacated. That has happened here — one drive went through three letters in an
# evening and picked up a letter a neighbour had released; device-keyed counters
# then credited one drive's errors to another, and that wrong number was read
# back and acted on. A metric that silently changes which physical object it
# describes is worse than no metric. The kernel only ever says "sdm", so the
# mapping happens at parse time and `disk_device_info` publishes the current
# correspondence for humans correlating against journal lines.
#
# ⚠️ A counter that only ever goes up is not the same as a working watcher. If
# the journal read fails, the run leaves the previous totals in place and does
# NOT write zeros — a blank series would read as "no errors" when it means "no
# data". disk_io_watch_last_run_seconds is what distinguishes the two.
{ config, lib, pkgs, ... }:

let
  notify = import ../lib/notify.nix { inherit pkgs; url = config.homelab.ntfy.url; };

  # Shared with the pool watchdog / temperature exporter, if you run them.
  textfileDir = "/var/lib/node-exporter-textfile";
  stateDir = "/var/lib/disk-io-watch";

  # Alerting is RELATIVE: compare against the state at the LAST notification,
  # never against a fixed band. A device that is already known-bad must not
  # re-page every interval.
  #
  #   - first alert:  a device crosses `newFaultErrors` having been clean
  #   - re-alert:     only when its total has at least DOUBLED since the last
  #                   notification AND grown by at least `newFaultErrors`
  #
  # Doubling makes re-alerts logarithmic: a fault going 50 -> 50,000 produces
  # ~10 notifications, not 1,000. A drive throwing ~300 resets/hr would
  # otherwise page hourly forever.
  newFaultErrors = 50;

  # Quiet hours are absolute for this class. A failing disk is not fire, flood,
  # or electrical, so it waits for morning. Nothing here may pierce them.
  notifyAfterHour = config.homelab.quietHours.end;
  notifyBeforeHour = config.homelab.quietHours.start;

  disk-io-watch = pkgs.writeShellApplication {
    name = "disk-io-watch";
    runtimeInputs = [ pkgs.systemd pkgs.coreutils pkgs.gnugrep pkgs.gawk pkgs.util-linux notify ];
    text = ''
      mkdir -p "${stateDir}"
      CURSOR="${stateDir}/cursor"
      BOOTID="${stateDir}/bootid"
      TOTALS="${stateDir}/totals"     # "<metric>|<label> <count>" per line
      NOTIFIED="${stateDir}/notified" # totals as of the last notification

      now_boot=$(cat /proc/sys/kernel/random/boot_id)
      prev_boot=$(cat "$BOOTID" 2>/dev/null || echo "")

      # A reboot resets the kernel log AND the real-world counters, so the
      # cursor and the accumulated totals are both meaningless across one.
      # Start clean rather than carrying a phantom baseline forward.
      if [ "$now_boot" != "$prev_boot" ]; then
        echo "boot changed ($prev_boot -> $now_boot) — resetting counters"
        rm -f "$CURSOR" "$TOTALS" "$NOTIFIED"
        printf '%s' "$now_boot" > "$BOOTID"
      fi

      # Read only what is new. Without a cursor (first run this boot) read the
      # whole boot once, which is the only full scan we ever pay for.
      jargs=( -k -o short-unix --no-pager --show-cursor )
      if [ -s "$CURSOR" ]; then
        jargs+=( --after-cursor "$(cat "$CURSOR")" )
      else
        jargs+=( -b )
      fi

      chunk=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$chunk'" EXIT
      if ! journalctl "''${jargs[@]}" > "$chunk" 2>/dev/null; then
        # ⚠️ Do NOT publish on a failed read. Writing zeros here would turn
        # "the lookup failed" into "there are no errors" — the classic
        # empty-result ambiguity.
        echo "journalctl read failed — leaving previous totals in place"
        exit 0
      fi

      # `--show-cursor` appends "-- cursor: s=..." as the LAST line. Save it for
      # the next run, then drop it so it can't be parsed as a log line.
      newcur=$(grep -oP '^-- cursor: \K.*' "$chunk" || true)
      grep -v '^-- cursor: ' "$chunk" > "$chunk.body" || true
      mv -f "$chunk.body" "$chunk"

      # --- count this chunk ------------------------------------------------
      # Block layer: "blk_update_request: I/O error, dev sdm, sector 12345"
      # SCSI:        "sd 6:0:0:0: [sdm] tag#0 ... hostbyte=DID_ERROR"
      # USB:         "usb 6-2: reset SuperSpeed USB device number 5 using xhci_hcd"
      delta=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$chunk' '$delta'" EXIT
      # ⚠️ KEY BY SERIAL, NOT BY KERNEL DEVICE NAME (see header). The kernel
      # only ever says "sdm", so map sdX -> serial at parse time and accumulate
      # under the serial. Letters may churn between runs; the serial does not.
      # (Residual edge case: a letter change WITHIN one 5-minute chunk is still
      # mis-mapped. Rare, and vastly better than the status quo.)
      declare -A SERIAL_OF=()
      while read -r kname serial; do
        [ -n "''${serial:-}" ] && SERIAL_OF["$kname"]="$serial"
      done < <(lsblk -dno KNAME,SERIAL 2>/dev/null || true)

      # ⚠️ The live map is only valid for RECENT lines. On the first run of a boot
      # there is no cursor, so we backfill the entire boot — and letters may have
      # changed many times inside that window. Mapping historical lines through
      # today's table reproduces the very misattribution this change exists to
      # stop, one level up: a backfill once credited one drive's errors to
      # another purely because the second drive later inherited the first's
      # letter.
      #
      # So: attribute only when the mapping can be trusted. On a backfill run,
      # every letter is recorded as "unresolved-<dev>" — honest, visibly
      # unattributed, and impossible to confuse with a real drive. Incremental
      # runs cover at most 5 minutes and DO use the live map.
      BACKFILL=0
      [ -s "$CURSOR" ] || BACKFILL=1

      # Unknown letters fall back rather than being dropped — losing an error
      # because a disk vanished before we could name it would be the same class
      # of silent-undercount this module exists to prevent.
      serial_of() {
        if [ "$BACKFILL" = "1" ]; then printf 'unresolved-%s' "$1"; return; fi
        printf '%s' "''${SERIAL_OF[$1]:-unknown-$1}"
      }

      {
        while read -r d; do
          [ -n "$d" ] && printf 'disk_io_errors_total|serial|%s\n' "$(serial_of "$d")"
        done < <(grep -oP 'I/O error, dev \K[a-z0-9]+' "$chunk" || true)
        while read -r d; do
          [ -n "$d" ] && printf 'disk_scsi_errors_total|serial|%s\n' "$(serial_of "$d")"
        done < <(grep -oP '\[\K[a-z0-9]+(?=\].*DID_ERROR)' "$chunk" || true)
        # USB ports are physical sockets and DO stay put, so they keep their own
        # stable identifier and need no translation.
        grep -oP 'usb \K[0-9]+-[0-9.]+(?=: reset )' "$chunk" \
          | awk '{print "usb_device_resets_total|port|" $1}' || true
      } | sort | uniq -c | awk '{print $2, $1}' > "$delta"

      # --- accumulate into persisted totals ---------------------------------
      # ⚠️ ONE-TIME MIGRATION: this module used to key counters by kernel device
      # name. Leaving those rows beside the new serial-keyed ones would keep
      # publishing a frozen series under a label that now describes a DIFFERENT
      # disk — precisely the confusion this change exists to end. Drop them; the
      # counters are per-boot anyway, so nothing durable is lost.
      if [ -s "$TOTALS" ] && grep -q '|device|' "$TOTALS"; then
        echo "migrating totals off device-name keys onto serials"
        grep -v '|device|' "$TOTALS" > "$TOTALS.mig" || true
        mv -f "$TOTALS.mig" "$TOTALS"
      fi

      touch "$TOTALS"
      merged=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$chunk' '$delta' '$merged'" EXIT
      awk '
        NR==FNR { t[$1] += $2; next }
                { t[$1] += $2 }
        END     { for (k in t) print k, t[k] }
      ' "$TOTALS" "$delta" | sort > "$merged"
      mv -f "$merged" "$TOTALS"

      # NOT `[ -n "$newcur" ] && ...` — as a trailing statement under `set -e`
      # a false test would exit the script before it ever publishes.
      if [ -n "$newcur" ]; then
        printf '%s' "$newcur" > "$CURSOR"
      fi

      # --- publish ----------------------------------------------------------
      # Atomic same-directory rename; node_exporter never sees a partial file.
      # Exactly one sample per name+label pair — a duplicate fails the WHOLE
      # textfile collector, not just this file (learned the hard way).
      if [ -d "${textfileDir}" ] && mtmp=$(mktemp "${textfileDir}/.disk-io-watch.prom.XXXXXX"); then
        {
          echo "# HELP disk_io_errors_total Block-layer I/O errors seen in the kernel log this boot."
          echo "# TYPE disk_io_errors_total counter"
          echo "# HELP disk_scsi_errors_total SCSI DID_ERROR results seen in the kernel log this boot."
          echo "# TYPE disk_scsi_errors_total counter"
          echo "# HELP usb_device_resets_total USB device resets seen in the kernel log this boot."
          echo "# TYPE usb_device_resets_total counter"
          # TOTALS lines are "<metric>|<label>|<value> <count>".
          awk '{ split($1, p, "|"); printf "%s{%s=\"%s\"} %s\n", p[1], p[2], p[3], $2 }' "$TOTALS"
          # The CURRENT letter<->serial mapping, so a human reading a journal line
        # that says "sdm" can find which counter it belongs to. Informational and
        # allowed to churn — the counters above deliberately do not.
        echo "# HELP disk_device_info Current kernel-name to serial mapping. The NAME churns; the serial does not."
        echo "# TYPE disk_device_info gauge"
        lsblk -dno KNAME,SERIAL 2>/dev/null \
          | awk 'NF==2 {printf "disk_device_info{device=\"%s\",serial=\"%s\"} 1\n",$1,$2}' || true
        echo "# HELP disk_io_watch_last_run_seconds Unix time of the last successful run. Staleness here means the watcher stopped, not that errors stopped."
          echo "# TYPE disk_io_watch_last_run_seconds gauge"
          echo "disk_io_watch_last_run_seconds $(date +%s)"
        } > "$mtmp"
        chmod 0644 "$mtmp"
        mv -f "$mtmp" "${textfileDir}/disk-io-watch.prom" || rm -f "$mtmp"
      fi

      # --- alert ------------------------------------------------------------
      hour=$(date +%-H)
      if [ "$hour" -lt ${toString notifyAfterHour} ] || [ "$hour" -ge ${toString notifyBeforeHour} ]; then
        echo "quiet hours (hour=$hour) — metrics published, notification suppressed"
        exit 0
      fi

      touch "$NOTIFIED"
      msg=$(awk -v thresh=${toString newFaultErrors} '
        NR==FNR { seen[$1] = $2; next }
        {
          key = $1; cur = $2; last = (key in seen ? seen[key] : 0)
          if (cur < thresh) next
          # First alert for a previously-clean device, or a doubling since the
          # last one. Both require thresh new events, so noise cannot creep up.
          if (last == 0 || (cur >= last * 2 && cur - last >= thresh)) {
            split(key, p, "|")
            printf "%s %s: %d (was %d)\n", p[3], p[1], cur, last
          }
        }
      ' "$NOTIFIED" "$TOTALS")

      if [ -n "$msg" ]; then
        notify "Disk I/O errors climbing" \
          "Kernel is logging I/O errors. This is the quiet fault shape that precedes a drop or zombie mount.

$msg

Check: journalctl -k -b | grep -E 'DID_ERROR|I/O error'" \
          default "warning,floppy_disk" || true
        # Only rebaseline the devices we actually mentioned, so a quiet device
        # that crosses the threshold later still gets its own first alert.
        cp -f "$TOTALS" "$NOTIFIED"
      fi
    '';
  };
in
{
  imports = [ ../options.nix ];

  systemd.tmpfiles.rules = [
    "d ${stateDir} 0750 root root - -"
  ];

  systemd.services.disk-io-watch = {
    description = "Count kernel disk I/O errors and USB resets per device";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${disk-io-watch}/bin/disk-io-watch";
    };
  };

  systemd.timers.disk-io-watch = {
    description = "Periodic disk I/O error census";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "5min";
      # Not Persistent: the counters are per-boot by construction, so there is
      # nothing to catch up on after downtime.
      AccuracySec = "30s";
    };
  };
}
