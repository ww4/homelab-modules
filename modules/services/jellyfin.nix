# Jellyfin media server, at jellyfin.<domain>.
#
# Jellyfin keeps listening on 0.0.0.0:8096 (default) so existing direct
# ip:8096 access for Roku/TV clients still works — the firewall decides who
# reaches it, not the bind address. nginx adds the friendly TLS URL.
{ config, lib, pkgs, ... }:

let
  jellyfinHost = "jellyfin.${config.homelab.domain}";
  jellyfinPort = 8096;
in
{
  imports = [ ../options.nix ];

  # The shared `media` group: every media-serving module declares it (the
  # module system merges the identical declarations) so a consumer need not
  # know which module happens to be first.
  users.groups.media = { };

  services.jellyfin = {
    enable = true;
    group = "media";
  };

  # Containers that authenticate against Jellyfin (e.g. Jellyseerr) reach it
  # via the host IP. User-defined Docker networks get auto-named br-<id>
  # bridges, which interface-scoped firewall rules silently cut off — every
  # container→Jellyfin SYN drops (connect ETIMEDOUT), breaking
  # Jellyfin-mediated logins and syncs. This opens 8096 to local Docker
  # bridges only, not LAN/world (the classic docker-bridge hole).
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 8096 -j nixos-fw-accept
  '';

  services.nginx.virtualHosts."${jellyfinHost}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;

    extraConfig = ''
      # Allow big uploads (image sync, large transcoded segment buffers).
      client_max_body_size 20M;
      add_header X-Content-Type-Options "nosniff" always;
    '';

    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString jellyfinPort}";
      proxyWebsockets = true;  # required for Jellyfin's now-playing + websocket transport
      extraConfig = ''
        # Streaming: disable response buffering so playback starts immediately
        # and big segment ranges don't hog nginx memory.
        proxy_buffering off;
        proxy_request_buffering off;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Protocol $scheme;
        proxy_set_header X-Forwarded-Host $http_host;
      '';
    };

    # Dedicated websocket path Jellyfin uses for control + push events.
    locations."/socket" = {
      proxyPass = "http://127.0.0.1:${toString jellyfinPort}";
      proxyWebsockets = true;
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
      '';
    };
  };
}
