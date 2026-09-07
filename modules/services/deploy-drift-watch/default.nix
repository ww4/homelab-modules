# deploy-drift-watch — alert when the forge has commits the box never deployed.
#
# THE GAP THIS CLOSES. A GitOps applier (comin) polls a git remote and
# rebuilds on new commits. Every failure metric it exports — build failed,
# eval failed, fetch failed — is about work it KNOWS about. If the remote it
# polls goes stale (a dead push-mirror, an expired token upstream, a repoint
# that never took effect), the applier fetches successfully, sees nothing
# new, and reports a perfectly healthy pipeline while merged changes pile up
# undeployed. That exact failure once ran for two days here: merges landed on
# the source-of-truth forge, the mirror the applier polled had silently
# frozen, and every deploy-health gauge stayed green.
#
# The only signal that discriminates is comparing the FORGE's branch head
# against the DEPLOYED commit — two facts no single component owns. This
# timer owns the comparison:
#   1. `git ls-remote` the source-of-truth repo for the branch head.
#   2. Read the deployed commit id from comin's own metrics
#      (comin_deployment_info{commit_id=...}).
#   3. If they differ, publish HOW LONG the undeployed head has existed
#      (age of the newest commit, via a shallow fetch).
#
# Metrics (node_exporter textfile collector):
#   deploy_drift_seconds        0 when deployed == head; else age of the head
#   deploy_drift_fetch_ok       1 if the forge answered this run
#   deploy_drift_last_run_seconds  liveness stamp
#
# Failure honesty (the house rule): a failed forge lookup publishes
# fetch_ok=0 and REMOVES the drift sample rather than writing 0 — "the lookup
# failed" must never read as "no drift". The ./alert-rules.json shipped
# alongside covers both: drift sustained >1h, and the check itself failing.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.deployDriftWatch;
  textfileDir = "/var/lib/node-exporter-textfile";

  watch = pkgs.writeShellApplication {
    name = "deploy-drift-watch";
    runtimeInputs = [ pkgs.git pkgs.curl pkgs.gnugrep pkgs.gawk pkgs.coreutils ];
    text = ''
      OUT="${textfileDir}/deploy-drift.prom"
      tmp=$(mktemp "${textfileDir}/.deploy-drift.prom.XXXXXX")
      trap 'rm -f "$tmp"' EXIT
      now=$(date +%s)

      emit_common() {
        echo "# HELP deploy_drift_fetch_ok 1 if the forge answered the drift check this run."
        echo "# TYPE deploy_drift_fetch_ok gauge"
        echo "# HELP deploy_drift_last_run_seconds Unix time of the last drift check."
        echo "# TYPE deploy_drift_last_run_seconds gauge"
        echo "deploy_drift_last_run_seconds $now"
      }

      # 1) The forge's branch head. Bounded: a hung forge must not wedge the
      # timer.
      head=$(timeout 30 git ls-remote "${cfg.repoUrl}" "refs/heads/${cfg.branch}" 2>/dev/null | awk '{print $1}' || true)
      if [ -z "$head" ]; then
        { emit_common; echo "deploy_drift_fetch_ok 0"; } > "$tmp"
        chmod 0644 "$tmp"; mv -f "$tmp" "$OUT"
        echo "forge lookup FAILED — published fetch_ok=0 and no drift sample"
        exit 0
      fi

      # 2) The deployed commit, from comin's own metrics.
      deployed=$(timeout 10 curl -s "${cfg.cominMetricsUrl}" 2>/dev/null \
                   | grep -oP 'comin_deployment_info\{commit_id="\K[0-9a-f]+' | head -1 || true)

      drift=0
      if [ -n "$deployed" ] && [ "$deployed" != "$head" ]; then
        # 3) Age of the undeployed head. A shallow fetch into a scratch repo
        # gets the commit timestamp without cloning history.
        scratch="''${STATE_DIRECTORY:-/var/lib/deploy-drift-watch}/scratch"
        mkdir -p "$scratch"
        [ -d "$scratch/.git" ] || git -C "$scratch" init -q
        ct=""
        if timeout 60 git -C "$scratch" fetch -q --depth 1 "${cfg.repoUrl}" "${cfg.branch}" 2>/dev/null; then
          ct=$(git -C "$scratch" log -1 --format=%ct FETCH_HEAD 2>/dev/null || true)
        fi
        if [ -n "$ct" ]; then
          drift=$(( now - ct )); [ "$drift" -lt 0 ] && drift=0
        else
          # Head is known-different but unageable: publish a sentinel hour so
          # the alert can still fire, rather than hiding a real drift as 0.
          drift=3600
        fi
        echo "DRIFT: forge ${cfg.branch}=$head, deployed=''${deployed:-unknown} — ''${drift}s"
      fi
      # An empty $deployed (comin down/mid-restart) is NOT drift — comin's own
      # health is alerted separately; claiming drift here would double-page.

      {
        emit_common
        echo "deploy_drift_fetch_ok 1"
        echo "# HELP deploy_drift_seconds Seconds the forge branch head has existed undeployed (0 = in sync)."
        echo "# TYPE deploy_drift_seconds gauge"
        echo "deploy_drift_seconds $drift"
      } > "$tmp"
      chmod 0644 "$tmp"
      mv -f "$tmp" "$OUT"
    '';
  };
in
{
  imports = [ ../../options.nix ];

  config = lib.mkIf cfg.enable {
    # Ship this watcher's alert rules into the monitoring module's merge.
    homelab.monitoring.extraAlertRuleFiles = [ ./alert-rules.json ];

    systemd.services.deploy-drift-watch = {
      description = "Compare the forge branch head against the deployed commit";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${watch}/bin/deploy-drift-watch";
        StateDirectory = "deploy-drift-watch";
      };
    };
    systemd.timers.deploy-drift-watch = {
      description = "Periodic deploy-drift check";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "5min";
        OnUnitActiveSec = "10min";
      };
    };
  };
}
