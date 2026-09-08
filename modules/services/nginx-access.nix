# nginx source-access gate — the real perimeter.
#
# Every vhost is meant to be reachable only over Tailscale or the trusted
# local network — never any WAN source, including a public IPv6 GUA the box
# may hold. Without this, nothing enforces that posture: nginx listens on
# 0.0.0.0/[::]:443 with no allow/deny, so "Tailscale-only" is DNS-illusory (a
# forged Host: header from the LAN or public IPv6 reaches the backend, with
# only per-app login as the gate).
#
# allow/deny here lives in the http{} block and is inherited by every server{}
# block (ngx_http_access_module), so this one place gates all current and
# future vhosts. Assumes ACME via DNS-01, so there is no inbound HTTP-01
# challenge to carve out; non-proxied listeners (e.g. a bitcoind P2P port)
# are unaffected.
#
# To reach a service from a new network, add its source range below and
# rebuild — a reviewable, version-controlled edit rather than a console tweak.
{ ... }:

{
  services.nginx.commonHttpConfig = ''
    # --- Allowed sources: loopback, RFC1918/LAN, docker, Tailscale ---
    allow 127.0.0.0/8;          # loopback (host-local service fetches)
    allow 10.0.0.0/8;           # RFC1918
    allow 172.16.0.0/12;        # RFC1918 (docker bridges; server-side widget fetches)
    allow 192.168.0.0/16;       # RFC1918 (the trusted LAN)
    allow 100.64.0.0/10;        # Tailscale CGNAT (IPv4)
    allow ::1/128;              # loopback (IPv6)
    allow fd7a:115c:a1e0::/48;  # Tailscale (IPv6)
    allow fc00::/7;             # unique-local (IPv6)
    allow fe80::/10;            # link-local (IPv6)
    # Everything else -- notably any public IPv6 GUA and any WAN -- is denied.
    deny all;
  '';
}
