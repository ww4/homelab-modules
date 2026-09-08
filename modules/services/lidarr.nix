# Lidarr — music manager ("Sonarr/Radarr for music"). Container on arr-net,
# sharing the homelab.arrStack.root /data tree with the download client so
# imports hardlink; Prowlarr syncs indexers.
#
# Wiring after first rebuild (via API or the UI):
#   - Root folder:     /data/media/music
#   - Download client: qBittorrent  (http://gluetun:8085, category "music")
#   - Prowlarr → Settings → Apps → add Lidarr (pushes audio indexers)
#
# Reach at https://lidarr.<domain>.
{ config, lib, pkgs, ... }:
let
  s = config.homelab.arrStack;
  arrNet = "arr-net";
  port = 8686;
in
{
  imports = [ ../options.nix ];

  systemd.tmpfiles.rules = [
    "d /var/lib/lidarr             0750 ${s.owner} ${s.group} - -"
    "d ${s.root}/media/music 0775 ${s.owner} ${s.group} - -"
  ];

  virtualisation.oci-containers.containers.lidarr = {
    image = "ghcr.io/linuxserver/lidarr:latest@sha256:2e4cdc7c8d5fa36915f446a29976ee01d3f9cc9722b8530a09121c76fbabef55";
    ports = [ "127.0.0.1:${toString port}:8686" ];
    environment = { PUID = s.puid; PGID = s.pgid; TZ = config.time.timeZone; };
    volumes = [
      "/var/lib/lidarr:/config:rw"
      "${s.root}:/data:rw"
    ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  systemd.services.docker-lidarr = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };

  services.nginx.virtualHosts."lidarr.${config.homelab.domain}" =
    import ../lib/proxy-vhost.nix { port = port; };
}
