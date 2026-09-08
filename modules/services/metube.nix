# MeTube — web GUI for yt-dlp, for one-off video downloads at
# metube.<domain> (network-gated; consider adding it to the SSO list).
#
# Downloads land in homelab.metube.downloadDir, written group `media` and
# world-readable (UMASK 022) so a media server running as group media can
# read them.
{ config, lib, pkgs, ... }:

let cfg = config.homelab.metube; in
{
  imports = [ ../options.nix ];

  options.homelab.metube = {
    downloadDir = lib.mkOption {
      type = lib.types.str;
      example = "/mnt/media/youtube/metube";
      description = "Host directory downloads land in (bind-mounted into the container).";
    };
    uid = lib.mkOption {
      type = lib.types.int;
      default = 971;
      description = ''
        Fixed uid for the metube user. ⚠️ Must be EXPLICIT: an auto-allocated
        system uid is null at eval time, which makes the container's UID env
        empty and the container fall back to uid 1000. Also note NixOS
        refuses to change an existing user's uid — if the user already
        exists, pin to whatever it was actually allocated.
      '';
    };
    mediaGid = lib.mkOption {
      type = lib.types.int;
      default = 984;
      description = ''
        gid of the `media` group, hardcoded for the same eval-time-null
        reason as uid: config.users.groups.media.gid is null at eval when
        the group was created without an explicit gid.
      '';
    };
  };

  config = {
    # Dedicated user; primary group `media` so output is readable by the
    # media server.
    users.users.metube = {
      isSystemUser = true;
      uid = cfg.uid;
      group = "media";
    };

    virtualisation.oci-containers.containers.metube = {
      image = "ghcr.io/alexta69/metube:latest@sha256:b4400ee6454c0663a93815dbbde59a02b3fdf244d5186e6c1870429034b4ccb3";
      environment = {
        DOWNLOAD_DIR = "/downloads";
        STATE_DIR    = "/downloads/.metube";
        TEMP_DIR     = "/downloads/.tmp";
        UID   = toString cfg.uid;
        GID   = toString cfg.mediaGid;
        UMASK = "022";                       # world-readable files for the media server
        # Flat, media-server-friendly naming; id keeps titles from colliding
        # and lets metadata be re-fetched later by id even for older files.
        OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s";
        # Capture all metadata up front so downloads aren't "raw": write the
        # full .info.json + thumbnail sidecars and embed basic tags into the
        # file. YTDL_OPTIONS is a JSON object merged into yt-dlp's params.
        # (EmbedThumbnail is omitted — unreliable for webm; the sidecar
        # thumbnail is what any promote pipeline should use for the poster.)
        YTDL_OPTIONS = builtins.toJSON {
          writeinfojson = true;
          writethumbnail = true;
          postprocessors = [ { key = "FFmpegMetadata"; } ];
        };
      };
      volumes = [ "${cfg.downloadDir}:/downloads" ];
      ports = [ "127.0.0.1:8092:8081" ];     # nginx fronts this
    };

    systemd.tmpfiles.rules = [
      "d ${builtins.dirOf cfg.downloadDir}  0755 root   root  - -"
      "d ${cfg.downloadDir}                0775 metube media - -"
    ];

    services.nginx.virtualHosts."metube.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      locations."/" = {
        proxyPass = "http://127.0.0.1:8092";
        recommendedProxySettings = true;
        proxyWebsockets = true;                # MeTube uses websockets for progress
        extraConfig = ''
          client_max_body_size 100M;
        '';
      };
    };
  };
}
