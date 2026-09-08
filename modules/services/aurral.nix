# Aurral — "Jellyseerr for music": a MusicBrainz-backed discovery/request UI
# that hands one-click artist/album requests to Lidarr. The audio counterpart
# to Jellyseerr (which only does movies/TV).
#
# Single container; serves UI + API on container port 3001, mapped to host
# 3007 (3001 is Grafana here). On arr-net so it reaches Lidarr at
# lidarr:8686. Reachable at https://music.<domain>.
#
# CONSUMER MUST DECLARE a sops secret "aurral-env" carrying
# LIDARR_API_KEY=<Lidarr → Settings → General → API Key> (read by root via
# docker --env-file → root:0400 is fine).
{ config, lib, pkgs, ... }:
let
  arrNet = "arr-net";
  hostPort = 3007;   # container listens on 3001; 3001 is taken by Grafana
  s = config.homelab.arrStack;
in
{
  imports = [ ../options.nix ];

  systemd.tmpfiles.rules = [
    "d /var/lib/aurral             0750 ${s.owner} ${s.group} - -"
  ];

  virtualisation.oci-containers.containers.aurral = {
    image = "ghcr.io/lklynet/aurral:latest@sha256:df151d4ea7d84629005484b7329516f1bd4d579c6dd38dc43bb327347aea6ac3";
    ports = [ "127.0.0.1:${toString hostPort}:3001" ];
    environment = {
      LIDARR_URL = "http://lidarr:8686";
      CONTACT_EMAIL = "admin@${config.homelab.domain}";  # MusicBrainz API User-Agent contact
    };
    environmentFiles = [ config.sops.secrets."aurral-env".path ];  # LIDARR_API_KEY
    volumes = [ "/var/lib/aurral:/app/data:rw" ];
    dependsOn = [ "lidarr" ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  systemd.services.docker-aurral = {
    after = [ "docker-network-arr.service" "docker-lidarr.service" ];
    requires = [ "docker-network-arr.service" ];
  };

  services.nginx.virtualHosts."music.${config.homelab.domain}" =
    import ../lib/proxy-vhost.nix { port = hostPort; };
}
