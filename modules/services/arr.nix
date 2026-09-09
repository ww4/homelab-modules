# *arr stack — Prowlarr + Sonarr + Radarr + Jellyseerr + qBittorrent (via a
# Gluetun VPN tunnel).
#
# All containers via virtualisation.oci-containers. Each web UI binds to
# 127.0.0.1 and is fronted by nginx behind the network source-gate.
#
# Network topology — qBittorrent shares Gluetun's network namespace so ALL
# its traffic exits through the VPN's WireGuard tunnel; there is no
# non-tunnel path for it to leak onto. Gluetun is the only thing that
# publishes qBittorrent's web-UI port.
#
# Storage layout (under homelab.arrStack.root):
#   root/
#   ├── media/{tv,movies}/                        # Sonarr/Radarr libraries
#   └── downloads/
#       ├── incomplete/  →  arrStack.scratchDir   # optional separate FS
#       └── complete/                             # hardlink target for *arr import
#
# Hardlinks work because complete/ and media/ are inside ONE filesystem.
# incomplete/ can live on a scratch disk; the client copies once at
# completion.
#
# CONSUMER MUST DECLARE a sops secret and point homelab.arrStack.vpnEnvFile
# at it: an environmentFile with the WireGuard
# credentials (WIREGUARD_PRIVATE_KEY / _PRESHARED_KEY / _ADDRESSES,
# SERVER_COUNTRIES, and — if your VPN offers port forwarding for inbound
# peers — FIREWALL_VPN_INPUT_PORTS; set qBittorrent's listen port to the
# SAME number in WebUI → Connection).
#
# Each *arr generates its own API key on first run; wire them up in the UIs
# (Prowlarr → Settings → Apps adds Sonarr/Radarr; Jellyseerr → Settings →
# Services adds Sonarr/Radarr; download-client wiring → qBittorrent).
{ config, lib, pkgs, ... }:

let
  s = config.homelab.arrStack;
  TZ = config.time.timeZone;

  # The unified /data tree gives Sonarr/Radarr/qBittorrent matching paths
  # for hardlinks.
  dataVolume = "${s.root}:/data:rw";

  # Subdomain → backend port
  ports = {
    prowlarr     = 9696;
    sonarr       = 8989;
    radarr       = 7878;
    jellyseerr   = 5055;
    qbittorrent  = 8085;  # qBit's default 8080 is a popular port; stay clear
    flaresolverr = 8191;  # headless browser proxy for Cloudflare-protected indexers
  };

  vhost = port: import ../lib/proxy-vhost.nix { inherit port; };

  # User-defined Docker network gives the *arr containers DNS-based service
  # discovery (Prowlarr can reach `flaresolverr:8191`, Sonarr can reach
  # `prowlarr:9696`, etc.). The default Docker bridge doesn't do DNS between
  # containers, only by IP — and IPs can shuffle on restart.
  arrNet = "arr-net";

  # Force IPv4-only inside the *arr containers.
  #
  # `arr-net` is a plain Docker bridge: IPv4 subnet, no IPv6 subnet, no
  # NAT66. So a container has no route to a v6 address. Many indexers (and
  # TMDB) are Cloudflare-fronted and DUAL-STACK, and glibc's getaddrinfo
  # prefers the AAAA answer — so the container picks an address it cannot
  # reach and the connection dies as "Resource temporarily unavailable" /
  # ERR_NAME_NOT_RESOLVED, while A-only hosts work fine. Diagnosed here
  # after 5 of 9 indexers auto-disabled and Sonarr logged "No available
  # indexers" 95× in a day; the dual-stack/A-only correlation was exact, and
  # Prowlarr's own error message says "ensure IPv6 is working or disabled".
  #
  # The alternative — giving arr-net a real IPv6 subnet + NAT66 — is more
  # moving parts for no benefit: nothing here needs v6 reachability.
  ipv4Only = "--sysctl=net.ipv6.conf.all.disable_ipv6=1";

in
{
  imports = [ ../options.nix ];

  # Create the arr-net Docker network before any *arr container starts.
  systemd.services.docker-network-arr = {
    description = "Create the arr-net Docker bridge network";
    wantedBy = [ "multi-user.target" ];
    after = [ "docker.service" ];
    before = map (n: "docker-${n}.service") [
      "prowlarr" "sonarr" "radarr" "jellyseerr" "gluetun" "flaresolverr"
      "homepage"   # a dashboard container may join arr-net for widget hostnames
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.docker}/bin/docker network inspect ${arrNet} >/dev/null 2>&1 || \
        ${pkgs.docker}/bin/docker network create --driver bridge ${arrNet}
    '';
  };

  # State + media + scratch dirs must exist before containers start.
  systemd.tmpfiles.rules = [
    "d ${s.root}                         0775 ${s.owner} ${s.group} - -"
    "d ${s.root}/media                   0775 ${s.owner} ${s.group} - -"
    "d ${s.root}/media/tv                0775 ${s.owner} ${s.group} - -"
    "d ${s.root}/media/movies            0775 ${s.owner} ${s.group} - -"
    "d ${s.root}/downloads               0775 ${s.owner} ${s.group} - -"
    "d ${s.root}/downloads/complete      0775 ${s.owner} ${s.group} - -"
    "d /var/lib/prowlarr                  0750 ${s.owner} ${s.group} - -"
    "d /var/lib/sonarr                    0750 ${s.owner} ${s.group} - -"
    "d /var/lib/radarr                    0750 ${s.owner} ${s.group} - -"
    "d /var/lib/jellyseerr                0750 ${s.owner} ${s.group} - -"
    "d /var/lib/qbittorrent               0750 ${s.owner} ${s.group} - -"
    "d /var/lib/gluetun                   0700 root  root  - -"
  ] ++ lib.optional (s.scratchDir != null)
    "d ${s.scratchDir}                     0775 ${s.owner} ${s.group} - -";

  # Images are pinned tag@digest: a bare :latest re-pulls on every GitOps
  # redeploy, which makes every container a standing supply-chain surface —
  # and containers sit INSIDE the nginx source-gate (172.16.0.0/12 is
  # allowed), so a compromised upstream image is a LAN-equivalent attacker.
  # The tag stays for readability; the digest is what deploys. To bump one
  # deliberately:
  #   nix run nixpkgs#skopeo -- inspect --format '{{.Digest}}' docker://<image>:<tag>
  # then update the digest in a PR.
  virtualisation.oci-containers.containers = {
    #--- Prowlarr (indexer hub) ---
    prowlarr = {
      image = "ghcr.io/linuxserver/prowlarr:latest@sha256:1295cff29d10b486c0d8324d1559a552140a5932bf8b3d87e398654414f63f92";
      ports = [ "127.0.0.1:${toString ports.prowlarr}:9696" ];
      environment = { PUID = s.puid; PGID = s.pgid; inherit TZ; };
      volumes = [
        "/var/lib/prowlarr:/config:rw"
      ];
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- FlareSolverr (Cloudflare challenge solver for protected indexers) ---
    # Runs a headless Chromium; Prowlarr POSTs requests here when an indexer
    # is gated by Cloudflare's JS challenge. FlareSolverr solves the
    # challenge and returns the cookie+HTML to Prowlarr. Configure in
    # Prowlarr: Settings → Indexers → FlareSolverr → http://flaresolverr:8191/v1
    # Stays OUTSIDE Gluetun's netns — only does HTTP challenge solving, not
    # torrent traffic, so it doesn't need VPN routing.
    flaresolverr = {
      image = "ghcr.io/flaresolverr/flaresolverr:latest@sha256:139dfee1c6f89249c8d665d1333a42e8ec74ec0a86bc6bb1c8461e10d3a66a47";
      ports = [ "127.0.0.1:${toString ports.flaresolverr}:8191" ];
      environment = {
        inherit TZ;
        LOG_LEVEL = "info";
      };
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- Sonarr (TV) ---
    sonarr = {
      image = "ghcr.io/linuxserver/sonarr:latest@sha256:373159ba768e23a3a1c497d9f2b936addf8fd5b1fdce7dd6a14080ac928bfda0";
      ports = [ "127.0.0.1:${toString ports.sonarr}:8989" ];
      environment = { PUID = s.puid; PGID = s.pgid; inherit TZ; };
      volumes = [
        "/var/lib/sonarr:/config:rw"
        dataVolume
      ] ++ lib.optional (s.keepersTv != null) "${s.keepersTv}:/keepers/tv:rw";
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- Radarr (movies) ---
    radarr = {
      image = "ghcr.io/linuxserver/radarr:latest@sha256:a45b5ab0f850f39edb4cc9c95bbd967b52ddc3d4574a4dfb45561177db6c88f4";
      ports = [ "127.0.0.1:${toString ports.radarr}:7878" ];
      environment = { PUID = s.puid; PGID = s.pgid; inherit TZ; };
      volumes = [
        "/var/lib/radarr:/config:rw"
        dataVolume
      ] ++ lib.optional (s.keepersMovies != null) "${s.keepersMovies}:/keepers/movies:rw";
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- Jellyseerr (request UI) ---
    jellyseerr = {
      image = "fallenbagel/jellyseerr:latest@sha256:4538137bc5af902dece165f2bf73776d9cf4eafb6dd714670724af8f3eb77764";
      ports = [ "127.0.0.1:${toString ports.jellyseerr}:5055" ];
      environment = { inherit TZ; };
      volumes = [
        "/var/lib/jellyseerr:/app/config:rw"
      ];
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- Gluetun (the VPN tunnel) ---
    # Owns the network namespace that qBittorrent shares. Publishes
    # qBittorrent's web-UI port here because qBittorrent itself has no
    # ports field (its netns is borrowed).
    gluetun = {
      image = "qmcgaw/gluetun:latest@sha256:e3272b29a4bc177b389fbdcb54cf9716ccbfc30f04d8b7a35b0a5be9cdb58461";
      ports = [
        # qBittorrent's web UI. Both sides the same number (matches
        # WEBUI_PORT below).
        "127.0.0.1:${toString ports.qbittorrent}:${toString ports.qbittorrent}"
        # qBittorrent's torrent listen port is the VPN's FORWARDED port
        # (FIREWALL_VPN_INPUT_PORTS in the sops env), bound on the
        # VPN-tunnel side, not the host. No host publishing needed.
      ];
      environment = {
        VPN_SERVICE_PROVIDER = s.vpnProvider;
        VPN_TYPE             = "wireguard";
        # Everything account-specific lives in vpnEnvFile (see the header).
      };
      environmentFiles = [ s.vpnEnvFile ];
      extraOptions = [
        "--cap-add=NET_ADMIN"
        "--device=/dev/net/tun"
        "--sysctl=net.ipv4.conf.all.rp_filter=2"
        "--network=${arrNet}"
        # Stable hostname for the shared netns. qBittorrent inherits it
        # (--network=container:gluetun forbids setting its own), and Qt's
        # QLockFile refuses to clear a stale profile lock written under a
        # DIFFERENT hostname — with the default hostname (= container ID,
        # new on every recreation) any hard-kill leaves an unclearable lock
        # and qbittorrent-nox crash-loops silently on every start. Learned
        # here the hard way: a VPN-provider die-off hard-killed the stack
        # and left thousands of rotated crash logs behind an "Up" container.
        "--hostname=gluetun"
      ];
    };

    #--- qBittorrent (downloads via Gluetun's netns) ---
    qbittorrent = {
      image = "ghcr.io/linuxserver/qbittorrent:latest@sha256:212b86dff59e3962b4082b5ef20a577e76c8f8527d2ab505cfa887b4bcecb0b0";
      dependsOn = [ "gluetun" ];
      environment = {
        PUID = s.puid; PGID = s.pgid; inherit TZ;
        WEBUI_PORT = toString ports.qbittorrent;
        # VueTorrent replaces qBit's default WebUI with the nicer Vue.js
        # alternative. The mod downloads VueTorrent at container start and
        # sets WebUI\AlternativeUIEnabled + WebUI\RootFolder in qBit's
        # config automatically. Backend API unchanged, so *arr
        # download-client wiring and dashboard widgets keep working.
        DOCKER_MODS = "ghcr.io/gabe565/linuxserver-mod-vuetorrent:latest@sha256:543f484b84489b651ccfed1ac8af62255652c00418143726c1a7d2331035abad";
      };
      volumes = [
        "/var/lib/qbittorrent:/config:rw"
        dataVolume
      ] ++ lib.optional (s.scratchDir != null) "${s.scratchDir}:/scratch/incomplete:rw";
      # Share Gluetun's network namespace — all traffic exits via the VPN.
      # NOTE: no `ports` field here; the web UI is published by gluetun.
      extraOptions = [
        "--network=container:gluetun"
      ];
    };
  };

  # The gabe565 vuetorrent mod drops files at /vuetorrent, but its s6-init
  # step that flips qBit's WebUI\AlternativeUIEnabled fails on the
  # linuxserver:latest base (s6 v3 vs v2 layout mismatch). In practice qBit
  # serves VueTorrent fine just from WebUI\RootFolder being set — it
  # validates the path on startup and serves the alt UI from memory. This
  # post-start poke is belt-and-suspenders: API-set both keys after qBit is
  # up. Never fails the unit (|| true) so a stuck startup window doesn't
  # cause a restart loop.
  systemd.services.docker-qbittorrent.serviceConfig.ExecStartPost = [
    "+${pkgs.writeShellScript "qbit-enable-vuetorrent" ''
      for i in $(seq 1 60); do
        ${pkgs.curl}/bin/curl -fsS --max-time 2 \
          -o /dev/null http://127.0.0.1:8085/api/v2/app/version && break
        sleep 1
      done
      # Subnet whitelist (set elsewhere) lets us call without auth from host.
      # JSON body must be url-encoded under the json= param per qBit API docs.
      ${pkgs.curl}/bin/curl -fsS -X POST \
        --data-urlencode 'json={"alternative_webui_enabled":true,"alternative_webui_path":"/vuetorrent"}' \
        http://127.0.0.1:8085/api/v2/app/setPreferences || true
    ''}"
  ];

  #--- nginx vhosts (behind the source gate) ---
  services.nginx.virtualHosts = {
    "prowlarr.${config.homelab.domain}"    = vhost ports.prowlarr;
    "sonarr.${config.homelab.domain}"      = vhost ports.sonarr;
    "radarr.${config.homelab.domain}"      = vhost ports.radarr;
    "requests.${config.homelab.domain}"    = vhost ports.jellyseerr;
    "qbittorrent.${config.homelab.domain}" = vhost ports.qbittorrent;
  };
}
