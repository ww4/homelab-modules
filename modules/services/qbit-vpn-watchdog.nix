# qbit-vpn-watchdog — self-heal the gluetun-IP-change qBittorrent wedge.
#
# THE WEDGE: qBittorrent runs in gluetun's network namespace
# (--network=container:gluetun, no interface binding of its own). When
# gluetun's WireGuard tunnel drops and reconnects onto a NEW public IP — e.g.
# a DNS-timeout restart storm — qBit keeps trying to send over the now-dead
# tun0. Every send fails EPERM, so connection_status flips to "firewalled",
# DHT drops to 0, and it silently stops transferring while the container still
# shows "Up" and gluetun still shows healthy. It does NOT self-recover; the
# only fix is restarting qBit (NOT gluetun, which is fine — restarting gluetun
# just re-wedges qBit) so it rebinds to the live tun0. Without this, that
# costs a monitoring page and a manual restart every time.
#
# THE FIX, deliberately narrow to avoid storm-amplification: don't restart qBit
# on every gluetun restart (a storm would restart qBit 15x and risk its own
# stale-QLockFile trouble). Instead poll once a minute and act only when the
# wedge is actually present AND gluetun has SETTLED:
#   - gluetun's current public IP = the newest "[ip getter] Public IP address is
#     X" line in its journald stream; the line's embedded RFC3339 timestamp tells
#     us how long that IP has held (the settle signal — no cross-poll state).
#   - qBit's bound IP + health = /api/v2/transfer/info (localhost auth bypass).
#   - Trigger = connection_status != "connected", once BOTH graces have passed:
#     gluetun's IP has held >= SETTLE seconds AND qBit's container has been up
#     >= QGRACE seconds. gluetun being healthy + settled means a non-connected qBit
#     is the wedge — whether it's bound to a STALE IP (IP-change wedge) or to NO IP
#     at all (never-bound / post-reboot wedge). The two graces keep us out of a
#     gluetun storm and out of qBit's normal warm-up; a cooldown bounds retries.
# Fail-safe: unknowns that mean "can't tell yet" (no gluetun IP line, qBit API down)
# => do nothing. But an empty qBit address is treated as a wedge ONCE past the
# startup grace — NOT skipped forever (that hole once hid a wedge for 2 hours).
# Runs as root so it can restart the unit directly.
{ config, pkgs, ... }:
let
  notify = import ../lib/notify.nix { inherit pkgs; url = config.homelab.ntfy.url; };

  watchdog = pkgs.writeShellApplication {
    name = "qbit-vpn-watchdog";
    runtimeInputs = [ pkgs.curl pkgs.jq pkgs.gnugrep pkgs.gawk pkgs.coreutils pkgs.systemd notify ];
    text = ''
      SETTLE=120     # gluetun's IP must have held this long before we act
      QGRACE=120     # qBit's own startup grace; empty IP / "firewalled" is normal warm-up until now
      COOLDOWN=300   # min seconds between our own restarts (bounds a failed retry)
      QBIT="http://127.0.0.1:8085"
      STATE="''${STATE_DIRECTORY:-/var/lib/qbit-vpn-watchdog}"
      LAST="$STATE/last-restart-epoch"
      now="$(date +%s)"

      # 1) gluetun's current public IP + when it was last (re)established. gluetun
      #    logs to journald (docker journald driver); its own line carries an
      #    RFC3339 timestamp as field 1.
      #
      # ⚠️ gluetun logs that line only when the IP is (re)established. A tunnel
      # that has been STABLE for longer than the lookback emits nothing, so a
      # naive `--since` window + "no line => skip" turns this watchdog OFF
      # exactly when the VPN is healthiest — it logs "skip" every 60 s and
      # never evaluates. That is the same skipped-forever hole the header
      # describes closing for the empty-qBit-IP case.
      #
      # Fix: cache the last IP we ever saw. A missing line then means "stable",
      # not "unknown" — and the cached epoch is old, which correctly reads as
      # settled. Only a genuinely never-seen IP still skips.
      CACHE="$STATE/last-gluetun-ip"
      g_ip=""; g_epoch=0
      line="$(journalctl CONTAINER_NAME=gluetun --since -7d --no-pager 2>/dev/null \
                | grep 'Public IP address is' | tail -1 || true)"
      if [ -n "$line" ]; then
        g_ip="$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' <<<"$line" | head -1)"
        g_ts="$(awk '{for(i=1;i<=NF;i++) if($i ~ /^20[0-9-]+T[0-9:]+Z?$/){print $i; exit}}' <<<"$line")"
        g_epoch="$(date -d "$g_ts" +%s 2>/dev/null || echo 0)"
        if [ -n "$g_ip" ] && [ "$g_epoch" -gt 0 ]; then
          printf '%s %s\n' "$g_ip" "$g_epoch" > "$CACHE"
        fi
      fi
      if { [ -z "$g_ip" ] || [ "$g_epoch" -le 0 ]; } && [ -r "$CACHE" ]; then
        read -r g_ip g_epoch < "$CACHE" || true
        echo "no gluetun public-IP line in journal window (tunnel stable) — using cached $g_ip"
      fi
      { [ -n "$g_ip" ] && [ "$g_epoch" -gt 0 ]; } || { echo "no gluetun IP known yet (no journal line, no cache); skip"; exit 0; }

      age=$(( now - g_epoch ))
      if [ "$age" -lt "$SETTLE" ]; then
        echo "gluetun IP $g_ip only ''${age}s old (<''${SETTLE}s settle) — gluetun may still be flapping; waiting"
        exit 0
      fi

      # 2) qBit's bound external IP + connection health.
      info="$(curl -s --max-time 5 "$QBIT/api/v2/transfer/info" 2>/dev/null || true)"
      [ -n "$info" ] || { echo "qBit API unreachable (likely mid-restart); skip"; exit 0; }
      q_ip="$(jq -r '.last_external_address_v4 // ""' <<<"$info" 2>/dev/null || echo "")"
      q_status="$(jq -r '.connection_status // ""' <<<"$info" 2>/dev/null || echo "")"

      # qBit startup grace. For the first QGRACE seconds after its container
      # (re)start, an empty external address / "firewalled" is normal warm-up — NOT
      # a wedge — so don't act. AFTER that window those same symptoms mean qBit
      # never bound to the tunnel. Skipping on empty IP *unconditionally* hides
      # the post-reboot "never-bound" wedge (qBit comes up before gluetun
      # settles, never takes the forwarded port, and stays firewalled with
      # last_external_address_v4=""). An unknown/empty container timestamp
      # leaves qbit_age huge => treated as past-grace, so we act rather than
      # skip forever.
      qbit_started="$(systemctl show docker-qbittorrent.service -p ActiveEnterTimestamp --value 2>/dev/null || true)"
      qbit_age=999999
      if [ -n "$qbit_started" ]; then
        qs_epoch="$(date -d "$qbit_started" +%s 2>/dev/null || echo 0)"
        [ "$qs_epoch" -gt 0 ] && qbit_age=$(( now - qs_epoch ))
      fi
      if [ "$qbit_age" -lt "$QGRACE" ]; then
        echo "qBit only ''${qbit_age}s into startup (<''${QGRACE}s) — warm-up, not a wedge; waiting"
        exit 0
      fi

      # 3) Past both graces (gluetun settled, qBit done starting). gluetun is
      #    confirmed healthy above, so a non-"connected" status is the wedge —
      #    whether qBit is bound to a STALE IP (IP-change wedge) or to NO IP
      #    (never-bound wedge). Only "connected" is healthy.
      if [ "$q_status" = "connected" ]; then
        echo "healthy: qBit connected (ip=''${q_ip:-none}, gluetun=$g_ip)"
        exit 0
      fi

      last=0; [ -r "$LAST" ] && last="$(cat "$LAST" 2>/dev/null || echo 0)"
      if [ $(( now - last )) -lt "$COOLDOWN" ]; then
        echo "wedge (status=$q_status, qBit ip=''${q_ip:-none} vs gluetun $g_ip) but within ''${COOLDOWN}s cooldown; skip"
        exit 0
      fi

      echo "WEDGE: qBit status=$q_status (ip=''${q_ip:-none}) while gluetun settled on $g_ip ''${age}s ago — restarting qBittorrent"
      echo "$now" > "$LAST"
      systemctl restart docker-qbittorrent.service
      notify "qBittorrent auto-healed (VPN wedge)" \
        "gluetun healthy on $g_ip but qBit was $q_status (bound: ''${q_ip:-none}) and had stopped transferring. Restarted it to rebind — recovered without a page." \
        "low" "vpn,qbit" || true
    '';
  };
in
{
  imports = [ ../options.nix ];

  systemd.services.qbit-vpn-watchdog = {
    description = "Self-heal the gluetun-IP-change qBittorrent netns wedge";
    after = [ "docker-gluetun.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${watchdog}/bin/qbit-vpn-watchdog";
      StateDirectory = "qbit-vpn-watchdog";
    };
  };
  systemd.timers.qbit-vpn-watchdog = {
    description = "Poll every 60s for the gluetun-IP-change qBittorrent wedge";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "60s";
    };
  };
}
