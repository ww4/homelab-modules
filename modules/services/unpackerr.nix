# unpackerr — extract RAR'd Scene releases so Sonarr/Radarr can import them.
#
# WHY: Scene release rules still require video be split into RAR volumes with
# SFV checksums — a BBS/FTP-era artifact preserved by rule inertia long after
# it stopped making sense. Neither Sonarr nor Radarr can extract an archive;
# they see `movie.r00`, `movie.r01`, … , fail the import, and a queue reaper
# then removes the download. unpackerr watches the *arr queues, extracts
# completed archive sets in place, lets the import proceed, then removes only
# what it extracted.
#
# SEEDS ARE NOT AFFECTED. unpackerr extracts *alongside* the archive set and
# never touches the .rNN files, so the client keeps seeding the original
# payload throughout (the same hardlink-never-move rule as the rest of a
# well-behaved media stack).
#
# CONSUMER MUST DECLARE a sops secret "unpackerr-env" with
# UN_SONARR_0_API_KEY / UN_RADARR_0_API_KEY. Only the keys are secret; URLs
# and paths stay readable in the nix environment below.
{ config, lib, pkgs, ... }:

let
  arrNet  = "arr-net";
  s = config.homelab.arrStack;

  # MUST match the main arr module exactly. unpackerr reads each queue item's
  # path as the *arr reports it (`/data/downloads/...`) and then opens that
  # path itself, so it only works if unpackerr sees the identical tree under
  # the identical mount point. Running it on the host instead — where the
  # same tree lives under homelab.arrStack.root — would silently match
  # nothing.
  dataVolume = "${s.root}:/data:rw";
in
{
  imports = [ ../options.nix ];

  virtualisation.oci-containers.containers.unpackerr = {
    # `:latest` matches the convention of the rest of the stack — GitOps
    # redeploys pull the current image.
    image = "golift/unpackerr:latest@sha256:4ec141eeb0cb2f971d7c92f21cc40b0d2d50d7920eb7a0557443cca52270c0b0";

    environment = {
      TZ = config.time.timeZone;

      UN_SONARR_0_URL       = "http://sonarr:8989";
      UN_SONARR_0_PATHS_0   = "/data/downloads";
      UN_SONARR_0_PROTOCOLS = "torrent";

      UN_RADARR_0_URL       = "http://radarr:7878";
      UN_RADARR_0_PATHS_0   = "/data/downloads";
      UN_RADARR_0_PROTOCOLS = "torrent";

      # How long after a successful import before the EXTRACTED files are
      # removed. The archives themselves are never touched. Default is 5m;
      # 10m buys margin for a slow pooled-storage import without leaving the
      # duplicate around long enough to matter for disk.
      UN_DELETE_DELAY = "10m";

      UN_INTERVAL    = "2m";
      UN_START_DELAY = "1m";
      UN_RETRY_DELAY = "5m";
      UN_MAX_RETRIES = "3";

      # One extraction at a time. On USB-attached pooled drives, parallel
      # extraction thrashes the pool for no wall-clock gain.
      UN_PARALLEL = "1";

      # Group-writable to match the rest of the media tree.
      UN_FILE_MODE = "0664";
      UN_DIR_MODE  = "0775";

      # Log to stdout -> journald, so failures surface like any other unit
      # rather than in a file nobody reads.
      UN_LOG_FILE = "";
      UN_DEBUG    = "false";
    };

    environmentFiles = [ config.sops.secrets."unpackerr-env".path ];
    volumes = [ dataVolume ];
    dependsOn = [ "sonarr" "radarr" ];
    extraOptions = [
      "--network=${arrNet}"
      "--user=${s.puid}:${s.pgid}"
    ];
  };

  # Same ordering guard the other *arr containers use: the user-defined
  # bridge must exist before this starts, otherwise the container fails to
  # attach.
  systemd.services.docker-unpackerr = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };
}
