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
{ config, lib, ... }:

let
  cfg = config.homelab.nginxAccess;
in
{
  # The library's vhosts are no use without the server: a consumer flake has
  # no reason to know it must enable nginx itself (the reference box did it
  # in private config; the 2026-10-02 rehearsal install had no nginx at all).
  services.nginx.enable = lib.mkDefault true;
  services.nginx.recommendedProxySettings = lib.mkDefault true;
  services.nginx.recommendedTlsSettings = lib.mkDefault true;

  # ⚠️ WHO THIS LETS IN, AND WHY DOCKER IS ON THE LIST.
  #
  # A third-party review called out the container bridge range: a compromised
  # container can reach every vhost as a trusted source. That is true, and it
  # is a deliberate choice, so it is spelled out here rather than left in a
  # comment nobody reads.
  #
  # Two things make it less alarming than it sounds, and one makes it worse.
  #
  # It does not bypass application authentication. This list is a network
  # source filter. A vhost behind Authelia still challenges a container
  # exactly as it challenges a laptop, so what a container gains is what any
  # device already on the LAN has.
  #
  # It cannot be fixed by narrowing the addresses, because docker's bridges
  # live INSIDE RFC1918: 172.16.0.0/12 is both "the container bridges" and a
  # perfectly ordinary home LAN range. Separating the two means filtering by
  # interface, which nginx's access module cannot do. The real fix is a
  # separate internal listener per service, which is a larger change than
  # this module.
  #
  # What makes it worse: emptying `containerBridges` is not free. Anything
  # that reaches a vhost from a container stops working, and this library
  # ships at least one such thing — uptime-kuma checking the vhosts it is
  # told to watch. Server-side dashboard widgets are the other case.
  #
  # So the list is an option now, with the previous behaviour as its default.
  # An operator who runs no container that needs a vhost can set
  # `homelab.nginxAccess.containerBridges = [ ]` and lose nothing.
  services.nginx.commonHttpConfig =
    let
      render = lib.concatMapStrings (c: "    allow ${c};\n");
    in ''
      # Allowed sources. Everything else, notably any public address and any
      # WAN, is denied.
      ${render cfg.allowedSources}${render cfg.containerBridges}
      deny all;
    '';
}
