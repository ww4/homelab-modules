# Glances — htop-style system monitor with a REST/web API, at
# glances.<domain>. A dashboard container can poll it for CPU / RAM / disk /
# network / temps live charts.
#
# https://github.com/nicolargo/glances
{ config, lib, pkgs, ... }:

{
  imports = [ ../options.nix ];

  services.glances = {
    enable = true;
    port = 61208;
    openFirewall = false;             # opened scoped below
    # --webserver is mandatory: the unit's default ExecStart launches the
    # curses TUI which immediately exits without a terminal. With this flag,
    # the same process serves the HTML UI and the REST API at /api/4/.
    extraArgs = [
      "--webserver"
      "--disable-plugin" "raid"        # raid plugin needs mdadm devices
    ];
  };

  # Tailnet clients may hit the port directly; the bridge rule is what lets a
  # dashboard container poll through the docker bridge gateway.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 61208 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 61208 -j nixos-fw-accept
  '';

  # nginx vhost for the familiar https URL with a real cert. Glances itself
  # has no auth — the network source-gate (and optionally the SSO
  # forward-auth list) is the perimeter.
  services.nginx.virtualHosts."glances.${config.homelab.domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:61208";
      recommendedProxySettings = true;
    };
  };
}
