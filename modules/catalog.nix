# The catalog — a machine-readable index of every module this library exports.
#
# Plain data, no nixpkgs needed: `nix eval --json .#catalog`. flake.nix checks
# it against nixosModules at eval time, so a module cannot be added without an
# entry here (and vice versa). Tooling — an installer, an agent composing a
# consumer flake — reads this instead of parsing module headers.
#
# Per entry:
#   description  one line, what the module is for
#   enable       "import" (importing it enables it) or the homelab.* enable
#                option that gates it
#   options      the homelab.* option paths the module reads (prefixes; the
#                option definitions with types/descriptions live in options.nix
#                and are the authority)
#   requires     other modules in this catalog that must be imported too
#   vhosts       subdomains it claims under homelab.domain
#   secrets      what the consumer must provide, one entry per file:
#                  option  the homelab.*File option to point at the file
#                  keys    variable names / contents the file must carry
#                  owner   which user reads it ("root" unless the module says)
#                  source  "generate" — tooling can mint the value;
#                          "supply"   — only the consumer can provide it;
#                          "first-boot" — the value only exists after a service
#                                         has run once (an API key it minted)
let
  file = option: keys: owner: source: { inherit option keys owner source; };
  envRoot = option: keys: source: file option keys "root" source;
in
{
  options = {
    description = "The homelab.* option set — the interface between the library and a consumer's values.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── base ───────────────────────────────────────────────────────────────────
  system = {
    description = "Locale, Nix settings, nixpkgs config for an always-on server.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  boot = {
    description = "Bootloader and power behaviour for an always-on server.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── perimeter & SSO ────────────────────────────────────────────────────────
  nginx-access = {
    description = "nginx source-access gate: allow/deny inherited by every vhost from one place.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  acme = {
    description = "Let's Encrypt via DNS-01, the TLS default for every vhost.";
    enable = "import";
    options = [ "homelab.acme" ];
    requires = [ ];
    vhosts = [ ];
    secrets = [
      (envRoot "homelab.acme.credentialsFile" [ "<DNS provider API credential, lego variable name>" ] "supply")
    ];
  };
  authelia = {
    description = "Authelia SSO: forward-auth gateway + OIDC provider.";
    enable = "homelab.authelia.enable";
    options = [ "homelab.domain" "homelab.adminUser" "homelab.adminDisplayName" "homelab.authelia" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "auth" ];
    # Machine secrets (jwt/session/storage keys) are generated on first start;
    # per-app OIDC client secrets are HASHES in homelab.authelia.oidcClients
    # with the plaintext in each app's own secret.
    secrets = [ ];
  };

  # ── storage ────────────────────────────────────────────────────────────────
  mergerfs-pools = {
    description = "Assemble homelab.pools into mounted MergerFS pools.";
    enable = "import";
    options = [ "homelab.pools" ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  pool-autoremount = {
    description = "Self-healing remount for pool members that drop off the bus; detects zombie mounts with real I/O.";
    enable = "import";
    options = [ "homelab.pools" "homelab.ntfy.url" ];
    requires = [ "mergerfs-pools" ];
    vhosts = [ ];
    secrets = [ ];
  };
  smart-dump = {
    description = "Dump the full SMART table for every drive to world-readable files.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  drive-temps = {
    description = "Drive temperature + SMART-health exporter for spinning disks.";
    enable = "import";
    options = [ "homelab.driveTemps" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };
  disk-io-watch = {
    description = "Count kernel I/O errors and USB resets per device; alert on a device that starts failing.";
    enable = "import";
    options = [ "homelab.ntfy.url" "homelab.quietHours" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── monitoring & alerting ──────────────────────────────────────────────────
  monitoring = {
    description = "Prometheus + Grafana + Alertmanager with alerting provisioned declaratively.";
    enable = "homelab.monitoring.enable";
    options = [ "homelab.domain" "homelab.monitoring" "homelab.quietHours" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "grafana" "prometheus" ];
    secrets = [
      (file "homelab.monitoring.grafanaOidcSecretFile" [ "<OIDC client secret, plaintext>" ] "grafana" "generate")
    ];
  };
  alertmanager-ntfy = {
    description = "Alertmanager webhook → ntfy phone notifications.";
    enable = "import";
    options = [ "homelab.domain" "homelab.ntfy.topic" ];
    requires = [ "monitoring" "ntfy" ];
    vhosts = [ ];
    secrets = [ ];
  };
  ntfy = {
    description = "Self-hosted ntfy: write-only anonymous access, self-provisioning subscriber.";
    enable = "import";
    options = [ "homelab.domain" "homelab.adminUser" "homelab.ntfy" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "ntfy" ];
    secrets = [ ];
  };
  deploy-drift-watch = {
    description = "Alert when the forge has commits the box never deployed.";
    enable = "homelab.deployDriftWatch.enable";
    options = [ "homelab.deployDriftWatch" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };
  mirror-drift-watch = {
    description = "Alert when a git mirror stops tracking its source.";
    enable = "import";
    options = [ "homelab.mirrorDriftWatch" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };
  nginx-log-paths-check = {
    description = "Build-time guard: nginx may only be told to write logs where it can write.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── services ───────────────────────────────────────────────────────────────
  nextcloud = {
    description = "Nextcloud with Postgres + Redis, curated apps, optional OIDC SSO.";
    enable = "import";
    options = [ "homelab.domain" "homelab.nextcloud" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "cloud" ];
    secrets = [
      (file "homelab.nextcloud.adminPasswordFile" [ "<initial admin password>" ] "nextcloud" "generate")
      (file "homelab.nextcloud.oidcSecretFile" [ "<OIDC client secret, plaintext>" ] "nextcloud" "generate")
    ];
  };
  forgejo = {
    description = "Forgejo git forge.";
    enable = "import";
    options = [ "homelab.domain" "homelab.forgejo" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "git" ];
    secrets = [
      (file "homelab.forgejo.oidcSecretFile" [ "<OIDC client secret, plaintext>" ] "forgejo" "generate")
    ];
  };
  vaultwarden = {
    description = "Vaultwarden (Bitwarden-compatible) password server.";
    enable = "import";
    options = [ "homelab.domain" "homelab.vaultwarden" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "<homelab.vaultwarden.subdomain>" ];
    secrets = [
      (envRoot "homelab.vaultwarden.envFile" [ "ADMIN_TOKEN (argon2 hash)" "SMTP_* (optional)" ] "generate")
    ];
  };
  paperless = {
    description = "Paperless-ngx OCR-indexed document archive.";
    enable = "import";
    options = [ "homelab.domain" "homelab.paperless" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "paperless" ];
    secrets = [
      (file "homelab.paperless.adminPasswordFile" [ "<initial admin password>" ] "paperless" "generate")
    ];
  };
  immich = {
    description = "Immich photo & video management.";
    enable = "import";
    options = [ "homelab.domain" "homelab.immich" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "photos" ];
    secrets = [ ];
  };
  jellyfin = {
    description = "Jellyfin media server.";
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "jellyfin" ];
    secrets = [ ];
  };
  audiobookshelf = {
    description = "Audiobookshelf audiobook / podcast server.";
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "abs" ];
    secrets = [ ];
  };
  tandoor = {
    description = "Tandoor Recipes.";
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "recipes" ];
    secrets = [ ];
  };
  silverbullet = {
    description = "SilverBullet markdown notes/tasks, optionally a two-writer space.";
    enable = "import";
    options = [ "homelab.domain" "homelab.silverbullet" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "notes" ];
    secrets = [ ];
  };
  uptime-kuma = {
    description = "Uptime Kuma status wall-board.";
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "uptime" ];
    secrets = [ ];
  };
  glances = {
    description = "Glances system monitor with a REST/web API.";
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "glances" ];
    secrets = [ ];
  };
  metube = {
    description = "MeTube web GUI for yt-dlp one-off downloads.";
    enable = "import";
    options = [ "homelab.domain" "homelab.metube" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "metube" ];
    secrets = [ ];
  };
  pinchflat = {
    description = "PinchFlat YouTube archiver.";
    enable = "import";
    options = [ "homelab.domain" "homelab.pinchflat" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "pinchflat" ];
    secrets = [ ];
  };
  remote-desktop = {
    description = "xrdp + XFCE remote desktop, Tailscale-only.";
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  meshagent = {
    description = "MeshCentral MeshAgent so a MeshCentral server can manage this host.";
    enable = "import";
    options = [ "homelab.meshagent" ];
    requires = [ ];
    vhosts = [ ];
    secrets = [
      (envRoot "homelab.meshagent.mshFile" [ "<server-generated .msh identity file>" ] "supply")
    ];
  };

  # ── download stack ─────────────────────────────────────────────────────────
  arr = {
    description = "Prowlarr + Sonarr + Radarr + Jellyseerr + qBittorrent inside a Gluetun VPN namespace.";
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "prowlarr" "sonarr" "radarr" "requests" "qbittorrent" ];
    secrets = [
      (envRoot "homelab.arrStack.vpnEnvFile"
        [ "WIREGUARD_PRIVATE_KEY" "WIREGUARD_PRESHARED_KEY" "WIREGUARD_ADDRESSES" "SERVER_COUNTRIES" "FIREWALL_VPN_INPUT_PORTS (optional)" ]
        "supply")
    ];
  };
  recyclarr = {
    description = "Sync TRaSH-Guides quality profiles into Sonarr & Radarr daily (bring your own profile YAML).";
    enable = "import";
    options = [ "homelab.recyclarr" ];
    requires = [ "arr" ];
    vhosts = [ ];
    # /var/lib/recyclarr/secrets.yml is written by hand after the *arrs mint
    # their keys; not a nix-managed file today.
    secrets = [
      (file "<manual: /var/lib/recyclarr/secrets.yml>" [ "sonarr_api_key" "radarr_api_key" ] "recyclarr" "first-boot")
    ];
  };
  unpackerr = {
    description = "Extract RAR'd releases in place so the *arrs can import them; seeds untouched.";
    enable = "import";
    options = [ "homelab.arrStack" "homelab.unpackerr" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [
      (envRoot "homelab.unpackerr.envFile" [ "UN_SONARR_0_API_KEY" "UN_RADARR_0_API_KEY" ] "first-boot")
    ];
  };
  decluttarr = {
    description = "Reap stalled/failed downloads from Sonarr/Radarr and re-search.";
    enable = "import";
    options = [ "homelab.decluttarr" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [
      (envRoot "homelab.decluttarr.envFile" [ "SONARR_API_KEY" "RADARR_API_KEY" ] "first-boot")
    ];
  };
  lidarr = {
    description = "Lidarr music manager on the shared /data tree.";
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" ];
    requires = [ "arr" ];
    vhosts = [ "lidarr" ];
    secrets = [ ];
  };
  lazylibrarian = {
    description = "LazyLibrarian ebook/audiobook automation on the shared /data tree.";
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" ];
    requires = [ "arr" ];
    vhosts = [ "lazylibrarian" ];
    secrets = [ ];
  };
  aurral = {
    description = "Aurral music discovery/request UI in front of Lidarr.";
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" "homelab.aurral" ];
    requires = [ "lidarr" ];
    vhosts = [ "music" ];
    secrets = [
      (envRoot "homelab.aurral.envFile" [ "LIDARR_API_KEY" ] "first-boot")
    ];
  };
  arr-missing-sweep = {
    description = "Weekly search for what is still missing in Sonarr/Radarr, with a metadata-mismatch skip rule.";
    enable = "import";
    options = [ "homelab.arrMissingSweep" "homelab.ntfy.url" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [
      (file "homelab.arrMissingSweep.apiEnvFile" [ "SONARR_API_KEY" "RADARR_API_KEY" ] "<homelab.arrMissingSweep.user>" "first-boot")
    ];
  };
  qbit-vpn-watchdog = {
    description = "Self-heal the gluetun-IP-change qBittorrent wedge.";
    enable = "import";
    options = [ "homelab.ntfy.url" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [ ];
  };
}
