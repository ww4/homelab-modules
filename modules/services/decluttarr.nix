# Decluttarr — auto-reaps stalled/failed downloads from Sonarr/Radarr and
# triggers a re-search, so dead torrents don't clog the queue.
#
# Conservative config: 3 strikes before removing a stalled torrent (~45 min at
# the 15-min timer), and only the safe jobs are enabled — failed downloads,
# failed imports (matched to known-unrecoverable messages), missing metadata,
# and stalled. NOT remove_slow / remove_orphans / remove_unmonitored /
# remove_done_seeding (those risk killing slow-but-alive grabs, manual
# downloads, or seeding torrents). private_tracker_handling stays `skip`.
#
# Runs as a container on arr-net so it resolves sonarr/radarr by name and
# reaches qBittorrent via gluetun:8085 (qBit shares gluetun's netns; the
# arr-net subnet is in qBit's WebUI auth whitelist, so no qBit password).
#
# CONSUMER MUST DECLARE the secret (root:0400; read by docker --env-file
# before the container starts):
#
#   sops.secrets."decluttarr-env" = {
#     sopsFile = <your secrets>/decluttarr-env.yaml;   # keys:
#     key = "decluttarr-env";                          #   SONARR_API_KEY=...
#   };                                                 #   RADARR_API_KEY=...
{ config, lib, pkgs, ... }:

let
  arrNet = "arr-net";

  # Config lives at /app/config/config.yaml inside the image (WorkDir /app).
  # No secrets here — api_key uses the !ENV tag (yaml_env_tag: bare var name),
  # resolved from the environmentFile at container start.
  configYaml = pkgs.writeText "decluttarr-config.yaml" ''
    general:
      log_level: INFO
      test_run: false
      timer: 15
      detect_deletions: false
      private_tracker_handling: skip
      public_tracker_handling: remove
    job_defaults:
      max_strikes: 3
    jobs:
      remove_failed_downloads:
      remove_failed_imports:
        message_patterns:
          - "Not a Custom Format upgrade for existing*"
          - "Not an upgrade for existing*"
          - "*Found potentially dangerous file with extension*"
          - "Invalid video file*"
          - "No files found are eligible for import*"
          - "One or more episodes expected in this release were not imported or missing from the release"
      remove_metadata_missing:
      remove_stalled:
    instances:
      sonarr:
        - base_url: "http://sonarr:8989"
          api_key: !ENV SONARR_API_KEY
      radarr:
        - base_url: "http://radarr:7878"
          api_key: !ENV RADARR_API_KEY
    download_clients:
      qbittorrent:
        - base_url: "http://gluetun:8085"
          name: "qBittorrent"
  '';
in
{
  systemd.tmpfiles.rules = [
    "d /var/lib/decluttarr            0700 root root - -"
  ];

  virtualisation.oci-containers.containers.decluttarr = {
    image = "ghcr.io/manimatter/decluttarr:latest@sha256:c06d48426b612b845f2406c2d045f266468a6390faf87aea796d497a2935ec95";
    environment = {
      TZ = config.time.timeZone;
      IN_DOCKER = "true";
    };
    environmentFiles = [ config.sops.secrets."decluttarr-env".path ];
    volumes = [ "${configYaml}:/app/config/config.yaml:ro" ];
    dependsOn = [ "sonarr" "radarr" "gluetun" ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  # Ensure the arr-net bridge exists before this container starts (the
  # network is created by docker-network-arr in the arr module).
  systemd.services.docker-decluttarr = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];

    # Bound the retries so a genuinely broken config (bad API key, missing
    # secret) fails loudly as a failed unit instead of hot-looping forever.
    # 5 tries in 10 min comfortably outlasts a gluetun restart while still
    # converging on a real fault. StartLimit* live in [Unit], not [Service].
    unitConfig = {
      StartLimitIntervalSec = 600;
      StartLimitBurst = 5;
    };

    serviceConfig = {
      # ⚠️ decluttarr EXITS 0 when it cannot reach qBittorrent. With the default
      # Restart=on-failure that clean exit is read as "finished successfully", so
      # systemd never restarts it — and there is no timer to pick it back up. A
      # gluetun/qBit blip once stopped it silently for ~41 h: the unit sat
      # `inactive (dead)` with Result=success, indistinguishable from a healthy
      # oneshot at a glance, and only came back because an unrelated deploy
      # happened to restart the unit.
      #
      # This is a long-running daemon that should never exit for any reason, so
      # the exit CODE must not gate the restart.
      Restart = lib.mkForce "always";
      RestartSec = 30;
    };
  };
}
