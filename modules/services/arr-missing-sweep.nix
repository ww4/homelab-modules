# arr-missing-sweep — weekly "search for what's still missing" across Sonarr/Radarr.
#
# WHY THIS EXISTS: Sonarr has NO recurring missing-episode search. Its RSS Sync
# (15 min) only sees releases *newly posted* to indexer feeds, so any back-catalog
# request whose one-shot search-on-add didn't fire sits monitored and untouched
# forever — with usable releases sitting on the indexers the whole time. Radarr
# has the same gap.
#
# POLITENESS: weekly, not daily; a large RandomizedDelaySec; and it only
# searches what is genuinely missing.
#
# ⚠️ THE SKIP RULE IS THE IMPORTANT PART. A blanket "search all missing" is
# actively harmful here: TVDB numbers some shows by segment rather than by
# broadcast episode (The Bullwinkle Show = 792 "missing" episodes that no release
# can ever satisfy, because the packs contain 13 files per season). Searching
# those hammers every indexer, every week, forever, for nothing. Any series with
# more than maxMissingPerSeries outstanding is treated as a metadata mismatch,
# skipped, and reported so a human can look — rather than silently retried.
#
# CONSUMER MUST DECLARE a sops secret whose file exports SONARR_API_KEY and
# RADARR_API_KEY (it is sourced by the sweep script), point
# homelab.arrMissingSweep.apiEnvFile at it, and set
# homelab.arrMissingSweep.user to the user that owns it.
{ config, lib, pkgs, ... }:

let
  notify = import ../lib/notify.nix { inherit pkgs; url = config.homelab.ntfy.url; };
  maxMissingPerSeries = 200;

  sweep = pkgs.writeShellApplication {
    name = "arr-missing-sweep";
    runtimeInputs = [ pkgs.curl pkgs.jq notify ];
    excludeShellChecks = [ "SC1091" ];
    text = ''
      set -euo pipefail
      . ${config.homelab.arrMissingSweep.apiEnvFile}
      S=http://127.0.0.1:8989/api/v3
      R=http://127.0.0.1:7878/api/v3
      MAX=${toString maxMissingPerSeries}
      searched=0; skipped=""

      miss=$(curl -sS -m 120 -H "X-Api-Key: $SONARR_API_KEY" \
        "$S/wanted/missing?pageSize=5000&monitored=true&includeSeries=true")

      # series title -> outstanding count, biggest first
      while IFS=$'\t' read -r n title; do
        [ -z "$title" ] && continue
        id=$(curl -sS -m 60 -H "X-Api-Key: $SONARR_API_KEY" "$S/series" \
             | jq -r --arg t "$title" '.[]|select(.title==$t)|.id')
        [ -z "$id" ] && continue
        if [ "$n" -gt "$MAX" ]; then
          skipped="''${skipped}\n  $title ($n missing — likely TVDB numbering mismatch)"
          continue
        fi
        curl -sS -m 60 -X POST "$S/command" -H "X-Api-Key: $SONARR_API_KEY" \
          -H 'Content-Type: application/json' \
          -d "{\"name\":\"MissingEpisodeSearch\",\"seriesId\":$id}" >/dev/null
        searched=$((searched + 1))
        sleep 20   # stagger: never burst every indexer at once
      done < <(echo "$miss" | jq -r '[.records[]?|.series.title]|group_by(.)
                 |map({t:.[0],n:length})|sort_by(-.n)[]|"\(.n)\t\(.t)"')

      movies=$(curl -sS -m 60 -H "X-Api-Key: $RADARR_API_KEY" "$R/movie" \
               | jq '[.[]|select(.monitored and (.hasFile|not))]|length')
      if [ "$movies" -gt 0 ]; then
        curl -sS -m 60 -X POST "$R/command" -H "X-Api-Key: $RADARR_API_KEY" \
          -H 'Content-Type: application/json' -d '{"name":"MissingMoviesSearch"}' >/dev/null
      fi

      msg="Searched $searched series + $movies missing movie(s)."
      if [ -n "$skipped" ]; then
        msg="$msg"$'\n'"Skipped (needs a human):"$(printf '%b' "$skipped")
      fi
      echo "$msg"
      notify "Weekly *arr missing sweep" "$msg" default "mag,tv" || true
    '';
  };
in
{
  imports = [ ../options.nix ];

  environment.systemPackages = [ sweep ];

  systemd.services.arr-missing-sweep = {
    description = "Weekly search for still-missing episodes/movies";
    serviceConfig = {
      Type = "oneshot";
      # Runs as the user that owns the *arr API-key secret.
      User = config.homelab.arrMissingSweep.user;
      ExecStart = "${sweep}/bin/arr-missing-sweep";
    };
  };

  systemd.timers.arr-missing-sweep = {
    description = "Weekly *arr missing sweep";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Sunday 09:00, late morning so it never overlaps the usual weekly
      # backup windows. Wide jitter so indexers never see a clockwork hit.
      OnCalendar = "Sun *-*-* 09:00:00";
      RandomizedDelaySec = "45m";
      Persistent = true;
    };
  };
}
