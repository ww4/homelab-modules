# Forgejo — self-hosted Git forge at git.<domain>.
#
# Friendly fork of Gitea, governed by Codeberg e.V. Lightweight Go single
# binary; perfect for personal scale.
#
# First-user bootstrap: registration is disabled below, so create the admin
# via CLI (`forgejo admin user create`) or temporarily flip
# DISABLE_REGISTRATION — the first web-UI account is auto-promoted to admin.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.forgejo;
  domain = config.homelab.domain;
  # Idempotently register the OIDC source. Runs as an ExecStartPost in the
  # forgejo unit so it inherits the service's env + credentials (SECRET_KEY
  # etc., needed to encrypt the stored client secret). Non-fatal (the unit
  # prefixes it with '-' and the script always exits 0) so a hiccup never
  # takes Forgejo down. Reads the client secret from its file at runtime.
  oauthSetup = pkgs.writeShellScript "forgejo-oauth-oidc" ''
    set -u
    fj=${config.services.forgejo.package}/bin/forgejo
    secret=$(cat ${toString cfg.oidcSecretFile} 2>/dev/null) || exit 0
    [ -n "$secret" ] || exit 0
    # already present? (match the source name in `admin auth list`)
    if "$fj" admin auth list 2>/dev/null | grep -qiw 'authelia'; then exit 0; fi
    "$fj" admin auth add-oauth \
      --name authelia \
      --provider openidConnect \
      --key forgejo \
      --secret "$secret" \
      --auto-discover-url https://auth.${domain}/.well-known/openid-configuration \
      --scopes "openid email profile groups" || true
    exit 0
  '';
in
{
  imports = [ ../options.nix ];

  systemd.services.forgejo.serviceConfig.ExecStartPost =
    lib.mkIf (cfg.oidcSecretFile != null) [ "-${oauthSetup}" ];

  services.forgejo = {
    enable = true;
    # SQLite is fine for personal scale (single user, dozens of repos).
    # Postgres becomes worthwhile only above ~50 active users or heavy CI.
    database.type = "sqlite3";
    # Repos + LFS objects live under /var/lib/forgejo by default.
    lfs.enable = true;
    settings = {
      server = {
        DOMAIN = "git.${domain}";
        ROOT_URL = "https://git.${domain}/";
        HTTP_ADDR = "127.0.0.1";
        HTTP_PORT = 3002;
        # SSH is exposed via the host's sshd so we don't collide with port
        # 22. Forgejo embeds the right git URL for clones.
        SSH_DOMAIN = "git.${domain}";
        START_SSH_SERVER = false;       # use host sshd, not built-in
        SSH_PORT = 22;                  # what Forgejo advertises in clone URLs
      };
      service = {
        # Open registration off — invite/admin-only.
        DISABLE_REGISTRATION = true;
        REQUIRE_SIGNIN_VIEW = false;    # public repos remain anonymous-cloneable
      };
      session.COOKIE_SECURE = true;
      log.LEVEL = "Info";
      # Mailer left unset — no SMTP wired. Password resets need the CLI
      # ('forgejo admin user change-password') until you add one.
      "ui.meta" = {
        AUTHOR = "Forgejo";
        DESCRIPTION = "Self-hosted Git";
      };
    };
  };

  # nginx reverse proxy.
  # NB: recommendedProxySettings is set at the location level only, NOT
  # globally — setting it at the services.nginx level pushes proxy_*
  # directives into the http {} block, which expands every vhost's
  # proxy_headers_hash beyond nginx's default bucket size and causes
  # those vhosts to return 400 on every request.
  services.nginx = {
    enable = true;
    virtualHosts."git.${domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;                  # DNS-01 (inherited defaults)
      locations."/" = {
        proxyPass = "http://127.0.0.1:3002";
        recommendedProxySettings = true;
        # Forgejo's git smart-HTTP can push large objects; lift the cap.
        extraConfig = ''
          client_max_body_size 4G;
          proxy_request_buffering off;
        '';
      };
    };
  };
}
