# LazyLibrarian — ebook/audiobook automation; the maintained successor to the
# retired Readarr. Container on arr-net, sharing the homelab.arrStack.root
# /data tree with the download client for hardlink imports; Prowlarr syncs
# Torznab indexers.
#
# NOTE: LazyLibrarian has no clean REST API for full configuration, so the
# download client, providers, and library are set in its web UI.
#
# Library target: /data/media/audiobooks → add this folder to
# Audiobookshelf's libraries so auto-downloaded audiobooks show up there.
# (ebooks → /data/media/books)
#
# Reach at https://lazylibrarian.<domain>.
{ config, lib, pkgs, ... }:
let
  s = config.homelab.arrStack;
  arrNet = "arr-net";
  port = 5299;
in
{
  imports = [ ../options.nix ];

  systemd.tmpfiles.rules = [
    "d /var/lib/lazylibrarian          0750 ${s.owner} ${s.group} - -"
    "d ${s.root}/media/audiobooks 0775 ${s.owner} ${s.group} - -"
    "d ${s.root}/media/books      0775 ${s.owner} ${s.group} - -"
  ];

  virtualisation.oci-containers.containers.lazylibrarian = {
    image = "ghcr.io/linuxserver/lazylibrarian:latest@sha256:f5a59edddd2bc35130586a5eeaab83bf409cc0be43fda48c2bf01eb9a1b46b3f";
    ports = [ "127.0.0.1:${toString port}:5299" ];
    environment = {
      PUID = s.puid; PGID = s.pgid; TZ = config.time.timeZone;
      # Calibre + ebook conversion tooling docker mod (handy for ebooks; harmless for audiobooks)
      DOCKER_MODS = "linuxserver/mods:universal-calibre@sha256:f34dea158ed513e3f0d973e7fc40f1a1e156ed0ba35abbf19e8d85c3c0974441";
    };
    volumes = [
      "/var/lib/lazylibrarian:/config:rw"
      "${s.root}:/data:rw"
    ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  systemd.services.docker-lazylibrarian = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };

  services.nginx.virtualHosts."lazylibrarian.${config.homelab.domain}" =
    import ../lib/proxy-vhost.nix { port = port; };
}
