# Nextcloud, at cloud.<domain>. Postgres + Redis, curated app set, optional
# OIDC SSO (Authelia-style provider at auth.<domain>).
#
# CONSUMER MUST DECLARE the admin-password sops secret (owner = nextcloud)
# and set homelab.nextcloud.adminPasswordFile. Nightly DB backups are the
# consumer's concern (services.postgresqlBackup — dump named databases
# individually, never pg_dumpall: it aborts entirely if any one database
# fails, which can silently break ALL backups for months).
{ config, lib, pkgs, ... }:

let cfg = config.homelab.nextcloud; in
{
  imports = [ ../options.nix ];

  # Configure the OIDC provider in user_oidc, idempotently, after setup.
  # Non-fatal (always exits 0) so it never blocks Nextcloud. `user_oidc:provider`
  # creates-or-updates by identifier, so re-running is safe. unique-uid=0 +
  # uid=preferred_username keeps usernames stable/matchable.
  systemd.services.nextcloud-oidc-setup = lib.mkIf (cfg.oidcSecretFile != null) {
    description = "Configure Nextcloud user_oidc OIDC provider";
    wantedBy = [ "multi-user.target" ];
    after = [ "nextcloud-setup.service" "phpfpm-nextcloud.service" ];
    requires = [ "nextcloud-setup.service" ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = ''
      set -u
      secret=$(cat ${cfg.oidcSecretFile} 2>/dev/null) || exit 0
      [ -n "$secret" ] || exit 0
      ${lib.getExe config.services.nextcloud.occ} app:enable user_oidc || true
      ${lib.getExe config.services.nextcloud.occ} user_oidc:provider authelia \
        --clientid="nextcloud" \
        --clientsecret="$secret" \
        --discoveryuri="https://auth.${config.homelab.domain}/.well-known/openid-configuration" \
        --scope="openid email profile" \
        --unique-uid=0 \
        --mapping-uid=preferred_username \
        --mapping-email=email \
        --mapping-display-name=name || true
      exit 0
    '';
  };

  services = {
    nginx = {
      enable = true;
      virtualHosts."cloud.${config.homelab.domain}" = {
        forceSSL = true;
        enableACME = true;
        acmeRoot = null;   # DNS-01 via the acme module's defaults
      };
    };
    nextcloud = {
      enable = true;
      hostName = "cloud.${config.homelab.domain}";
      package = pkgs.nextcloud32;
      database.createLocally = true;
      configureRedis = true;
      maxUploadSize = "16G";
      https = true;
      autoUpdateApps.enable = true;
      extraAppsEnable = true;
      extraApps = with config.services.nextcloud.package.packages.apps; {
        # Apps already packaged in nixpkgs' nextcloud-apps.json.
        inherit calendar contacts notes onlyoffice tasks cookbook qownnotesapi;
        inherit user_oidc;   # OIDC SSO app (inert unless a provider is configured)
      };
      settings = {
        overwriteProtocol = "https";
        default_phone_region = "US";
        maintenance_window_start = 2; # start at 2AM
      };
      config = {
        # Postgres, recommended over SQLite.
        dbtype = "pgsql";
        adminpassFile = cfg.adminPasswordFile;
        adminuser = "admin";
      };
      # Suggested by Nextcloud's health check.
      phpOptions."opcache.interned_strings_buffer" = "16";
    };
  };

  systemd.services.nextcloud-setup.serviceConfig = {
    RequiresMountsFor = [ "/var/lib/nextcloud" ];
  };
}
