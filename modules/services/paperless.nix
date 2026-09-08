# Paperless-ngx — OCR-indexed document archive, at paperless.<domain>.
#
# Consume folder: /var/lib/paperless/consume — drop a PDF/jpg there and
# paperless OCRs, dates, tags, and files it.
# DB: SQLite by default; switch to postgres if you ever cross ~50k docs.
#
# CONSUMER MUST DECLARE a sops secret for the initial admin password and set
# homelab.paperless.adminPasswordFile to its path (owner = paperless — the
# module reads passwordFile to set the Django superuser password).
{ config, lib, pkgs, ... }:

let cfg = config.homelab.paperless; in
{
  imports = [ ../options.nix ];

  options.homelab.paperless = {
    adminPasswordFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to the initial admin password (a sops secret owned by paperless).";
    };
    # OIDC SSO. The secret-bearing PAPERLESS_SOCIALACCOUNT_PROVIDERS JSON (it
    # embeds the client secret) goes in an environmentFile, NOT in `settings`
    # — settings render into the world-readable systemd unit. The file is
    # read by systemd (root) AND sourced by the paperless-manage wrapper as
    # the paperless user → owner=paperless. Matching client hash belongs in
    # homelab.authelia.oidcClients. null = no SSO.
    oidcEnvFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "environmentFile carrying the OIDC provider JSON; null disables SSO.";
    };
  };

  config = {
    services.paperless = {
      enable = true;
      address = "127.0.0.1";
      port = 28981;
      passwordFile = cfg.adminPasswordFile;
      consumptionDir = "/var/lib/paperless/consume";
      consumptionDirIsPublic = false;
      mediaDir = "/var/lib/paperless/media";       # holds originals + archives
      dataDir = "/var/lib/paperless/data";
      settings = {
        # Kept explicitly under /var/lib/paperless so a first-tier backup
        # catches everything with one path.
        PAPERLESS_URL = "https://paperless.${config.homelab.domain}";
        PAPERLESS_OCR_LANGUAGE = "eng";
        PAPERLESS_OCR_MODE = "skip";               # only OCR files that need it
        PAPERLESS_TIME_ZONE = config.time.timeZone;
        PAPERLESS_CONSUMER_POLLING = 60;           # seconds; inotify is unreliable on NFS/mergerfs
        PAPERLESS_FILENAME_FORMAT = "{created_year}/{correspondent}/{title}";
      } // lib.optionalAttrs (cfg.oidcEnvFile != null) {
        # OIDC non-secret settings; the provider JSON lives in the env file.
        # Regular admin login stays enabled as a fallback.
        PAPERLESS_APPS = "allauth.socialaccount.providers.openid_connect";
        PAPERLESS_SOCIAL_AUTO_SIGNUP = true;             # skip the intermediate signup form
        PAPERLESS_SOCIALACCOUNT_ALLOW_SIGNUPS = true;    # allow first-time SSO users
      };
    } // lib.optionalAttrs (cfg.oidcEnvFile != null) {
      environmentFile = cfg.oidcEnvFile;
    };

    services.nginx.virtualHosts."paperless.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      locations."/" = {
        proxyPass = "http://127.0.0.1:28981";
        recommendedProxySettings = true;
        extraConfig = ''
          client_max_body_size 200M;     # for big scans
        '';
      };
    };
  };
}
