# Audiobookshelf audiobook / podcast server — fronted by nginx at
# abs.<domain> (network-gated; TLS via the flake's ACME DNS-01 defaults).
{ config, lib, pkgs, ... }:

{
  imports = [ ../options.nix ];

  services.audiobookshelf = {
    enable = true;
    group = "media";
    host = "127.0.0.1";        # nginx fronts; no direct external access
  };

  services.nginx.virtualHosts."abs.${config.homelab.domain}" =
    import ../lib/proxy-vhost.nix {
      port = 8000;
      extraConfig = ''
        client_max_body_size 0;
        proxy_buffering off;
      '';
    };
}
