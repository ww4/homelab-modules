# The catalog — a machine-readable index of every module this library exports.
#
# Plain data, no nixpkgs needed: `nix eval --json .#catalog`. flake.nix checks
# it against nixosModules at eval time, so a module cannot be added without an
# entry here (and vice versa). Tooling — an installer, an agent composing a
# consumer flake — reads this instead of parsing module headers.
#
# Per entry:
#   description  one line, what the module is for
#   memory       rough steady-state resident memory in MiB at household load,
#                the number the configurator adds up against the box's RAM
#                (bursts — a transcode, an ML job, a snapraid sync — are not in it;
#                 0 = no long-running process of its own)
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
    memory = 0;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── base ───────────────────────────────────────────────────────────────────
  system = {
    description = "Locale, Nix settings, nixpkgs config for an always-on server.";
    memory = 0;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  boot = {
    description = "Bootloader and power behaviour for an always-on server.";
    memory = 0;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── perimeter & SSO ────────────────────────────────────────────────────────
  nginx-access = {
    description = "nginx source-access gate: allow/deny inherited by every vhost from one place.";
    memory = 64;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  acme = {
    description = "Let's Encrypt via DNS-01, the TLS default for every vhost.";
    memory = 0;
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
    memory = 160;
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
    memory = 96;
    enable = "import";
    options = [ "homelab.pools" ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  snapraid = {
    description = "SnapRAID parity for a MergerFS pool's member disks: nightly sync, weekly partial scrub; any one member recoverable per parity disk.";
    memory = 64;
    enable = "homelab.snapraid.enable";
    options = [ "homelab.snapraid" "homelab.pools" ];
    requires = [ "mergerfs-pools" ];
    vhosts = [ ];
    secrets = [ ];
  };
  pool-autoremount = {
    description = "Self-healing remount for pool members that drop off the bus; detects zombie mounts with real I/O.";
    memory = 8;
    enable = "import";
    options = [ "homelab.pools" "homelab.ntfy.url" ];
    requires = [ "mergerfs-pools" ];
    vhosts = [ ];
    secrets = [ ];
  };
  smart-dump = {
    description = "Dump the full SMART table for every drive to world-readable files.";
    memory = 8;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  drive-temps = {
    description = "Drive temperature + SMART-health exporter for spinning disks.";
    memory = 8;
    enable = "import";
    options = [ "homelab.driveTemps" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };
  disk-io-watch = {
    description = "Count kernel I/O errors and USB resets per device; alert on a device that starts failing.";
    memory = 8;
    enable = "import";
    options = [ "homelab.ntfy.url" "homelab.quietHours" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── monitoring & alerting ──────────────────────────────────────────────────
  monitoring = {
    description = "Prometheus + Grafana + Alertmanager with alerting provisioned declaratively.";
    memory = 640;
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
    memory = 64;
    enable = "import";
    options = [ "homelab.domain" "homelab.ntfy.topic" ];
    requires = [ "monitoring" "ntfy" ];
    vhosts = [ ];
    secrets = [ ];
  };
  ntfy = {
    description = "Self-hosted ntfy: write-only anonymous access, self-provisioning subscriber.";
    memory = 32;
    enable = "import";
    options = [ "homelab.domain" "homelab.adminUser" "homelab.ntfy" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "ntfy" ];
    secrets = [ ];
  };
  deploy-drift-watch = {
    description = "Alert when the forge has commits the box never deployed.";
    memory = 8;
    enable = "homelab.deployDriftWatch.enable";
    options = [ "homelab.deployDriftWatch" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };
  mirror-drift-watch = {
    description = "Alert when a git mirror stops tracking its source.";
    memory = 8;
    enable = "import";
    options = [ "homelab.mirrorDriftWatch" ];
    requires = [ "monitoring" ];
    vhosts = [ ];
    secrets = [ ];
  };
  nginx-log-paths-check = {
    description = "Build-time guard: nginx may only be told to write logs where it can write.";
    memory = 8;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── services ───────────────────────────────────────────────────────────────
  nextcloud = {
    description = "Nextcloud with Postgres + Redis, curated apps, optional OIDC SSO.";
    memory = 768;
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
    memory = 320;
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
    memory = 96;
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
    memory = 1024;
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
    memory = 1536;
    enable = "import";
    options = [ "homelab.domain" "homelab.immich" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "photos" ];
    secrets = [ ];
  };
  jellyfin = {
    description = "Jellyfin media server.";
    memory = 512;
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "jellyfin" ];
    secrets = [ ];
  };
  audiobookshelf = {
    description = "Audiobookshelf audiobook / podcast server.";
    memory = 192;
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "abs" ];
    secrets = [ ];
  };
  tandoor = {
    description = "Tandoor Recipes.";
    memory = 384;
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "recipes" ];
    secrets = [ ];
  };
  silverbullet = {
    description = "SilverBullet markdown notes/tasks, optionally a two-writer space.";
    memory = 128;
    enable = "import";
    options = [ "homelab.domain" "homelab.silverbullet" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "notes" ];
    secrets = [ ];
  };
  uptime-kuma = {
    description = "Uptime Kuma status wall-board.";
    memory = 192;
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "uptime" ];
    secrets = [ ];
  };
  glances = {
    description = "Glances system monitor with a REST/web API.";
    memory = 96;
    enable = "import";
    options = [ "homelab.domain" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "glances" ];
    secrets = [ ];
  };
  metube = {
    description = "MeTube web GUI for yt-dlp one-off downloads.";
    memory = 192;
    enable = "import";
    options = [ "homelab.domain" "homelab.metube" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "metube" ];
    secrets = [ ];
  };
  pinchflat = {
    description = "PinchFlat YouTube archiver.";
    memory = 320;
    enable = "import";
    options = [ "homelab.domain" "homelab.pinchflat" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "pinchflat" ];
    secrets = [ ];
  };
  remote-desktop = {
    description = "xrdp + XFCE remote desktop, Tailscale-only.";
    memory = 256;
    enable = "import";
    options = [ ];
    requires = [ ];
    vhosts = [ ];
    secrets = [ ];
  };
  meshagent = {
    description = "MeshCentral MeshAgent so a MeshCentral server can manage this host.";
    memory = 48;
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
    memory = 1280;
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" ];
    requires = [ "acme" "nginx-access" ];
    vhosts = [ "prowlarr" "sonarr" "radarr" "requests" "qbittorrent" ];
    secrets = [
      (envRoot "homelab.arrStack.vpnEnvFile"
        [ "WIREGUARD_PRIVATE_KEY" "WIREGUARD_PRESHARED_KEY" "WIREGUARD_ADDRESSES" "SERVER_COUNTRIES" "FIREWALL_VPN_INPUT_PORTS (optional)" ]
        "supply")
      # Minted by tooling and seeded into each app before first start (lib/arr-api-seed.nix);
      # every consumer module reads the same file.
      (envRoot "homelab.arrStack.apiKeyEnvFile" [ "SONARR_API_KEY" "RADARR_API_KEY" "PROWLARR_API_KEY" "LIDARR_API_KEY" ] "generate")
    ];
  };
  recyclarr = {
    description = "Sync TRaSH-Guides quality profiles into Sonarr & Radarr daily (bring your own profile YAML).";
    memory = 16;
    enable = "import";
    options = [ "homelab.recyclarr" "homelab.arrStack" ];
    requires = [ "arr" ];
    vhosts = [ ];
    # secrets.yml is rendered from homelab.arrStack.apiKeyEnvFile when that is set
    # (the configurator always sets it); by hand otherwise — see the module header.
    secrets = [ ];
  };
  unpackerr = {
    description = "Extract RAR'd releases in place so the *arrs can import them; seeds untouched.";
    memory = 48;
    enable = "import";
    options = [ "homelab.arrStack" "homelab.unpackerr" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [
      # Same values as homelab.arrStack.apiKeyEnvFile, in unpackerr's spelling.
      (envRoot "homelab.unpackerr.envFile" [ "UN_SONARR_0_API_KEY" "UN_RADARR_0_API_KEY" ] "generate")
    ];
  };
  decluttarr = {
    description = "Reap stalled/failed downloads from Sonarr/Radarr and re-search.";
    memory = 64;
    enable = "import";
    options = [ "homelab.decluttarr" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [
      (envRoot "homelab.decluttarr.envFile" [ "SONARR_API_KEY" "RADARR_API_KEY" ] "generate")
    ];
  };
  lidarr = {
    description = "Lidarr music manager on the shared /data tree.";
    memory = 320;
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" ];
    requires = [ "arr" ];
    vhosts = [ "lidarr" ];
    secrets = [ ];
  };
  lazylibrarian = {
    description = "LazyLibrarian ebook/audiobook automation on the shared /data tree.";
    memory = 192;
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" ];
    requires = [ "arr" ];
    vhosts = [ "lazylibrarian" ];
    secrets = [ ];
  };
  aurral = {
    description = "Aurral music discovery/request UI in front of Lidarr.";
    memory = 96;
    enable = "import";
    options = [ "homelab.domain" "homelab.arrStack" "homelab.aurral" ];
    requires = [ "lidarr" ];
    vhosts = [ "music" ];
    secrets = [
      (envRoot "homelab.aurral.envFile" [ "LIDARR_API_KEY" ] "generate")
    ];
  };
  arr-missing-sweep = {
    description = "Weekly search for what is still missing in Sonarr/Radarr, with a metadata-mismatch skip rule.";
    memory = 8;
    enable = "import";
    options = [ "homelab.arrMissingSweep" "homelab.ntfy.url" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [
      (file "homelab.arrMissingSweep.apiEnvFile" [ "SONARR_API_KEY" "RADARR_API_KEY" ] "<homelab.arrMissingSweep.user>" "generate")
    ];
  };
  qbit-vpn-watchdog = {
    description = "Self-heal the gluetun-IP-change qBittorrent wedge.";
    memory = 8;
    enable = "import";
    options = [ "homelab.ntfy.url" ];
    requires = [ "arr" ];
    vhosts = [ ];
    secrets = [ ];
  };

  # ── backup ─────────────────────────────────────────────────────────────────
  backup = {
    description = "restic snapshots of the irreplaceable small state: a local repo on the pool plus an optional offsite one, same paths and retention; optional SFTP push target for a second machine.";
    memory = 64;
    enable = "import";
    options = [ "homelab.backup" "homelab.adminUser" ];
    requires = [ ];
    vhosts = [ ];
    secrets = [
      (envRoot "homelab.backup.passwordFile" [ "<restic repository passphrase, one line>" ] "generate")
      # Only read when homelab.backup.remote.enable is on (the option is nullable;
      # the configurator skips a nullable secret under a disabled feature group).
      (envRoot "homelab.backup.remote.environmentFile" [ "<backend credentials as restic env vars, e.g. B2_ACCOUNT_ID + B2_ACCOUNT_KEY>" ] "supply")
    ];
  };
}
