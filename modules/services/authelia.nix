# Authelia — centralized SSO: forward-auth gateway + OIDC provider.
#
# Two modes at once:
#   * nginx forward-auth (auth_request) puts a real login + 2FA in front of
#     services that have NO auth of their own. List them in
#     homelab.authelia.protectedVhosts — ONE list drives both the nginx
#     wiring and the Authelia access-control rule, because keeping those as
#     two separate edits is exactly how a vhost ends up with the auth hook
#     but no rule (a bare 403 instead of a login redirect — nginx's
#     error_page 401 cannot rescue a request Authelia refuses outright).
#   * a full OIDC provider for the apps that speak it — pass client
#     definitions via homelab.authelia.oidcClients (hashes only; plaintext
#     secrets live in the consuming app's sops secret).
#
# Deliberately do NOT forward-auth apps with native clients or webhooks
# (Grafana embeds, ntfy apps, Bitwarden clients, …) — those break behind a
# 302. Give them OIDC instead.
#
# Machine secrets (jwt/session/storage-encryption/OIDC keys) are generated
# random ON FIRST BOOT — no key material ever exists in git. The only human
# step: replace the seeded temp user in /var/lib/authelia-secrets/users.yml
# with your own (authelia crypto hash generate argon2 --password '…'), then
# enrol TOTP/passkey at first login.
{ config, lib, pkgs, ... }:
let
  cfg      = config.homelab.authelia;
  domain   = config.homelab.domain;
  authHost = "auth.${domain}";
  port     = 9091;
  secDir   = "/var/lib/authelia-secrets";

  # Reusable forward-auth wiring merged into each protected vhost. Server-level
  # auth_request (covers every location); the internal subrequest location turns
  # auth_request OFF on itself to avoid a loop.
  protect = {
    extraConfig = ''
      auth_request /internal/authelia/authz;
      auth_request_set $user   $upstream_http_remote_user;
      auth_request_set $groups $upstream_http_remote_groups;
      auth_request_set $name   $upstream_http_remote_name;
      auth_request_set $email  $upstream_http_remote_email;
      proxy_set_header Remote-User   $user;
      proxy_set_header Remote-Groups $groups;
      proxy_set_header Remote-Name   $name;
      proxy_set_header Remote-Email  $email;
      error_page 401 =302 https://${authHost}/?rd=$scheme://$http_host$request_uri;
    '';
    locations."/internal/authelia/authz" = {
      proxyPass = "http://127.0.0.1:${toString port}/api/authz/auth-request";
      extraConfig = ''
        internal;
        auth_request off;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-Method $request_method;
        proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Uri $request_uri;
      '';
    };
  };
in
{
  imports = [ ../options.nix ];

  config = lib.mkIf cfg.enable {
    # --- random machine secrets + a seeded temp user (so the service can start) ---
    systemd.services.authelia-secrets = {
      description = "Generate Authelia machine secrets + seed user db";
      wantedBy = [ "multi-user.target" ];
      before = [ "authelia-main.service" ];
      after = [ "systemd-sysusers.service" ];
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
      path = [ pkgs.openssl pkgs.coreutils pkgs.authelia ];
      script = ''
        set -eu
        install -d -m 700 ${secDir}
        for s in jwt session storage; do
          [ -s ${secDir}/$s ] || openssl rand -hex 48 > ${secDir}/$s
        done
        # OIDC machine secrets — generated on-box like the others, so no key
        # material lives in git. hmac signs OIDC tokens; the RSA key is the
        # issuer JWKS private key (the module templates it into the oidc config).
        [ -s ${secDir}/oidc-hmac ]       || openssl rand -hex 32 > ${secDir}/oidc-hmac
        [ -s ${secDir}/oidc-issuer.pem ] || openssl genrsa -out ${secDir}/oidc-issuer.pem 4096
        if [ ! -s ${secDir}/users.yml ]; then
          # Seed the admin user with a RANDOM password (nobody can log in until
          # the human replaces this file with a real hash). Lets Authelia start
          # + lets the forward-auth redirect be verified end-to-end.
          tmp=$(openssl rand -hex 16)
          hash=$(authelia crypto hash generate argon2 --password "$tmp" 2>/dev/null \
                   | sed -n 's/^Digest: //p')
          cat > ${secDir}/users.yml <<EOF
        users:
          ${config.homelab.adminUser}:
            disabled: false
            displayname: "${config.homelab.adminDisplayName}"
            password: "$hash"
            email: ${config.homelab.adminUser}@${domain}
            groups: [admins]
        EOF
        fi
        chmod 600 ${secDir}/* || true
        chown -R authelia-main:authelia-main ${secDir} || true
      '';
    };

    services.authelia.instances.main = {
      enable = true;
      secrets = {
        jwtSecretFile            = "${secDir}/jwt";
        sessionSecretFile        = "${secDir}/session";
        storageEncryptionKeyFile = "${secDir}/storage";
        # OIDC: the module env-injects the hmac and templates the issuer key
        # into identity_providers.oidc.jwks for us.
        oidcHmacSecretFile        = "${secDir}/oidc-hmac";
        oidcIssuerPrivateKeyFile  = "${secDir}/oidc-issuer.pem";
      };
      settings = {
        theme = "dark";
        server.address = "tcp://127.0.0.1:${toString port}/";
        log.level = "info";

        authentication_backend.file.path = "${secDir}/users.yml";
        # single-user file backend: no self-service password reset (needs SMTP)
        authentication_backend.password_reset.disable = true;

        totp.issuer = domain;
        webauthn.display_name = cfg.displayName;

        # OIDC provider. hmac_secret + jwks come from the secret files above
        # (module-wired). Client definitions come from the consumer — each
        # client_secret there is a pbkdf2 HASH (safe in the store — one-way
        # hash of a 256-bit random secret); the matching plaintext lives in
        # the consuming app's sops secret.
        identity_providers.oidc = lib.mkIf (cfg.oidcClients != [ ]) {
          clients = cfg.oidcClients;
        };

        session.cookies = [{
          inherit domain;
          authelia_url = "https://${authHost}";
          default_redirection_url = "https://${domain}";
          name = "authelia_session";
          # Relaxed for convenience — the real perimeter is the network
          # source-gate in front of every vhost, so long sessions are fine. A
          # positive remember_me also enables the portal's "Remember me" box.
          expiration = "1M";      # hard session cap
          inactivity = "1w";      # idle timeout
          remember_me = "3M";     # "Remember me" extended lifetime
        }];

        # Runtime-writable files go in the service's StateDirectory (/var/lib/
        # authelia-main); ProtectSystem=strict makes everything else (incl. the
        # read-only secrets dir) non-writable, so the DB + notifier must live here.
        storage.local.path = "/var/lib/authelia-main/db.sqlite3";

        # filesystem notifier: the 2FA-registration link is written to a file
        # the admin reads once (avoids an SMTP dependency for a single user).
        notifier.filesystem.filename = "/var/lib/authelia-main/notification.txt";

        access_control = {
          default_policy = "deny";
          rules = [{
            domain = map (v: "${v}.${domain}") cfg.protectedVhosts;
            policy = "two_factor";
          }];
        };
      };
    };

    # --- the portal vhost + forward-auth merged into every protected vhost ---
    # (one attrset: the protect entries come from the SAME list as the rule)
    services.nginx.virtualHosts = {
      "${authHost}" = {
        forceSSL = true;
        enableACME = true;
        acmeRoot = null;
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString port}";
          extraConfig = ''
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $http_host;
            proxy_set_header X-Forwarded-Uri $request_uri;
            proxy_set_header X-Forwarded-For $remote_addr;
          '';
        };
      };
    } // lib.genAttrs
      (map (v: "${v}.${domain}") cfg.protectedVhosts)
      (_: protect);
  };
}
