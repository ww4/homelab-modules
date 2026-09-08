# Immich — self-hosted photo & video management, at photos.<domain>.
# Immich itself binds 127.0.0.1 only; nginx fronts it.
#
# OIDC note: deliberately NOT wired into Immich declaratively —
# services.immich.settings would write a FULL config.json and reset every
# other system setting tuned in the admin UI. Enable OAuth in the admin UI
# instead (Administration → Settings → OAuth), issuer
# https://auth.<domain>/.well-known/openid-configuration, and keep the
# client secret in a sops secret you `sudo cat` for the one-time UI step.
{ config, lib, pkgs, ... }:

let cfg = config.homelab.immich; in
{
  imports = [ ../options.nix ];

  options.homelab.immich = {
    mediaLocation = lib.mkOption {
      type = lib.types.str;
      example = "/mnt/media/immich";
      description = "Where Immich stores photo/video data.";
    };
    # Offload ML inference to another box (e.g. a beefier CPU/GPU host over
    # the tailnet). If that host is down, ML jobs queue/fail but photo
    # serving, upload, and browsing are unaffected. null = run ML locally.
    mlUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://ml-host:3003";
      description = "Remote immich machine-learning endpoint; null = local ML.";
    };
  };

  config = {
    services.immich = {
      enable = true;
      host = "127.0.0.1";                # nginx fronts; no direct external access
      port = 2283;
      mediaLocation = cfg.mediaLocation;
      machine-learning.enable = lib.mkIf (cfg.mlUrl != null) false;
      environment = lib.mkIf (cfg.mlUrl != null) {
        IMMICH_MACHINE_LEARNING_URL = lib.mkForce cfg.mlUrl;
      };
    };

    # Trust the local nginx so Immich honors X-Forwarded-Proto and generates
    # https:// URLs in the UI / share links.
    systemd.services.immich-server.environment.IMMICH_TRUSTED_PROXIES = "127.0.0.1";

    services.nginx.virtualHosts."photos.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      locations."/" = {
        proxyPass = "http://127.0.0.1:2283";
        proxyWebsockets = true;
        extraConfig = ''
          client_max_body_size 50000M;
          proxy_buffering off;
          proxy_read_timeout 600s;
          proxy_send_timeout 600s;
        '';
      };
    };
  };
}
