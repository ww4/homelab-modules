# mirror-drift-watch — alert when a git mirror stops tracking its source.
#
# THE GAP THIS CLOSES. A push-mirror fails in the worst possible way: the
# source forge keeps accepting pushes, the mirror target keeps serving its
# last-good copy, and the only record of the failure is a `last_error` field
# in the source forge's database that nothing reads. A dead mirror once went
# unnoticed here for days — and because a deploy pipeline happened to be
# reading the mirror, it took deploys down with it. The pipeline has since
# been repointed at the source, but a rotting mirror is still a rotting
# offsite copy, which is the reason the mirror exists.
#
# The check is deliberately CREDENTIAL-FREE: compare the two branch heads
# anonymously with `git ls-remote`. That limits coverage to public repos —
# private mirrors fall back to their sync interval and this module's silence
# about them is documented, not accidental.
#
# Metrics (node_exporter textfile collector), per configured pair:
#   mirror_drift_seconds{repo}    0 when heads match; else age of source head
#   mirror_drift_fetch_ok{repo}   1 if BOTH ends answered this run
#   mirror_drift_last_run_seconds liveness stamp
#
# Failure honesty: an unreachable end publishes fetch_ok=0 for that pair and
# NO drift sample — "the lookup failed" must never read as "no drift".
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.mirrorDriftWatch;
  textfileDir = "/var/lib/node-exporter-textfile";

  pairArgs = map (p: "${p.name}|${p.source}|${p.mirror}|${p.branch}") cfg.pairs;

  watch = pkgs.writeShellApplication {
    name = "mirror-drift-watch";
    runtimeInputs = [ pkgs.git pkgs.gawk pkgs.coreutils ];
    text = ''
      OUT="${textfileDir}/mirror-drift.prom"
      tmp=$(mktemp "${textfileDir}/.mirror-drift.prom.XXXXXX")
      trap 'rm -f "$tmp"' EXIT
      now=$(date +%s)

      {
        echo "# HELP mirror_drift_seconds Seconds the source branch head has existed unmirrored (0 = in sync)."
        echo "# TYPE mirror_drift_seconds gauge"
        echo "# HELP mirror_drift_fetch_ok 1 if both ends answered the drift check this run."
        echo "# TYPE mirror_drift_fetch_ok gauge"
        echo "# HELP mirror_drift_last_run_seconds Unix time of the last drift check."
        echo "# TYPE mirror_drift_last_run_seconds gauge"
        echo "mirror_drift_last_run_seconds $now"
      } > "$tmp"

      for pair in ${lib.escapeShellArgs pairArgs}; do
        IFS='|' read -r name src mirror branch <<< "$pair"

        s=$(timeout 30 git ls-remote "$src" "refs/heads/$branch" 2>/dev/null | awk '{print $1}' || true)
        m=$(timeout 30 git ls-remote "$mirror" "refs/heads/$branch" 2>/dev/null | awk '{print $1}' || true)

        if [ -z "$s" ] || [ -z "$m" ]; then
          echo "mirror_drift_fetch_ok{repo=\"$name\"} 0" >> "$tmp"
          echo "$name: lookup FAILED (source=''${s:-none} mirror=''${m:-none}) — fetch_ok=0, no drift sample"
          continue
        fi
        echo "mirror_drift_fetch_ok{repo=\"$name\"} 1" >> "$tmp"

        drift=0
        if [ "$s" != "$m" ]; then
          # Age of the unmirrored source head, via a shallow fetch into a
          # scratch repo (no history, no working tree).
          scratch="''${STATE_DIRECTORY:-/var/lib/mirror-drift-watch}/$name"
          mkdir -p "$scratch"
          [ -d "$scratch/.git" ] || git -C "$scratch" init -q
          ct=""
          if timeout 60 git -C "$scratch" fetch -q --depth 1 "$src" "$branch" 2>/dev/null; then
            ct=$(git -C "$scratch" log -1 --format=%ct FETCH_HEAD 2>/dev/null || true)
          fi
          if [ -n "$ct" ]; then
            drift=$(( now - ct )); [ "$drift" -lt 0 ] && drift=0
          else
            # Heads known-different but unageable: publish a sentinel hour so
            # the alert can fire rather than hiding real drift as 0.
            drift=3600
          fi
          echo "$name: DRIFT — source=$s mirror=$m (''${drift}s)"
        fi
        echo "mirror_drift_seconds{repo=\"$name\"} $drift" >> "$tmp"
      done

      chmod 0644 "$tmp"
      mv -f "$tmp" "$OUT"
    '';
  };
in
{
  imports = [ ../../options.nix ];

  options.homelab.mirrorDriftWatch = {
    pairs = lib.mkOption {
      default = [ ];
      description = "Source/mirror repo pairs to watch (anonymous read — public repos only).";
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption { type = lib.types.str; description = "Metric label."; };
          source = lib.mkOption { type = lib.types.str; description = "Source-of-truth clone URL."; };
          mirror = lib.mkOption { type = lib.types.str; description = "Mirror clone URL."; };
          branch = lib.mkOption { type = lib.types.str; default = "main"; description = "Branch to compare."; };
        };
      });
    };
  };

  config = lib.mkIf (cfg.pairs != [ ]) {
    homelab.monitoring.extraAlertRuleFiles = [ ./alert-rules.json ];

    systemd.services.mirror-drift-watch = {
      description = "Compare git mirrors against their sources";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${watch}/bin/mirror-drift-watch";
        StateDirectory = "mirror-drift-watch";
      };
    };
    systemd.timers.mirror-drift-watch = {
      description = "Periodic mirror-drift check";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "7min";
        OnUnitActiveSec = "15min";
      };
    };
  };
}
