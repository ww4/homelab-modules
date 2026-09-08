# Vaultwarden — Rust re-implementation of the Bitwarden server, at
# <subdomain>.<domain>.
#
# Tiny (~10 MB resident), single SQLite DB at /var/lib/bitwarden_rs.
#
# CONSUMER MUST DECLARE a sops secret and set homelab.vaultwarden.envFile:
# it carries ADMIN_TOKEN (a one-way `vaultwarden hash` argon2 value — /admin
# requires it) and any SMTP credentials. systemd reads the environmentFile as
# root before dropping privileges → root:0400 is fine.
#
# SMTP (invites, new-device alerts, email 2FA) is site-specific: put the
# non-secret parts in homelab.vaultwarden.extraConfig and the credentials in
# the env file. Upstream rule: once SMTP_USERNAME is set, SMTP_PASSWORD is
# mandatory — add both to the secret BEFORE enabling, or vaultwarden errors
# on the SMTP config. Outbound only, so it doesn't change the network
# posture.
{ config, lib, pkgs, ... }:

let cfg = config.homelab.vaultwarden; in
{
  imports = [ ../options.nix ];

  options.homelab.vaultwarden = {
    subdomain = lib.mkOption {
      type = lib.types.str;
      default = "vault";
      description = ''
        Vhost subdomain. (Tip from the field: Chrome Safe Browsing has been
        known to flag "vault.*" names — pick something else if that bites.)
      '';
    };
    envFile = lib.mkOption {
      type = lib.types.str;
      description = "environmentFile with ADMIN_TOKEN (+ SMTP creds if used).";
    };
    extraConfig = lib.mkOption {
      type = lib.types.attrs;
      default = { };
      description = "Extra non-secret vaultwarden config (e.g. the SMTP block).";
    };
  };

  config = {
    services.vaultwarden = {
      enable = true;
      dbBackend = "sqlite";
      environmentFile = cfg.envFile;
      config = {
        DOMAIN = "https://${cfg.subdomain}.${config.homelab.domain}";
        ROCKET_ADDRESS = "127.0.0.1";
        ROCKET_PORT = 8222;
        # WebSockets for live-sync between clients (deprecated in upstream;
        # default-disabled but harmless to leave configured).
        WEBSOCKET_ENABLED = false;
        # Block public signups — invite-only via admin panel.
        SIGNUPS_ALLOWED = false;
        INVITATIONS_ALLOWED = true;
        SHOW_PASSWORD_HINT = false;
      } // cfg.extraConfig;
    };

    services.nginx.virtualHosts."${cfg.subdomain}.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      locations."/" = {
        proxyPass = "http://127.0.0.1:8222";
        recommendedProxySettings = true;
        extraConfig = ''
          client_max_body_size 525M;       # for attachments
        '';
      };
    };
  };
}
