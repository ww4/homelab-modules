# Uptime Kuma — visual status dashboard at uptime.<domain>.
#
# Complements the Prometheus alert pipeline by giving a "is everything up
# RIGHT NOW" wall-board. First user signup via the UI becomes admin — do that
# once and never again. After admin exists, signup auto-closes (the NixOS
# module sets DISABLE_SIGNUP via the wizard flow).
{ config, lib, pkgs, ... }:

{
  imports = [ ../options.nix ];

  services.uptime-kuma = {
    enable = true;
    settings = {
      PORT = "3010";
      HOST = "127.0.0.1";
      UPTIME_KUMA_DISABLE_FRAME_SAMEORIGIN = "1";   # let nginx own framing policy
    };
  };

  services.nginx.virtualHosts."uptime.${config.homelab.domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:3010";
      proxyWebsockets = true;            # Kuma uses socket.io for live updates
      recommendedProxySettings = true;
    };
  };
}
