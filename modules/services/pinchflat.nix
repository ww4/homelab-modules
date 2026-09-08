# PinchFlat — YouTube archiver, at pinchflat.<domain>.
{ config, lib, pkgs, ... }:

{
  imports = [ ../options.nix ];

  options.homelab.pinchflat.mediaDir = lib.mkOption {
    type = lib.types.str;
    example = "/mnt/media/pinchflat";
    description = "Where PinchFlat stores downloaded media.";
  };

  config = {
    services.pinchflat = {
      enable = true;
      selfhosted = true;
      mediaDir = config.homelab.pinchflat.mediaDir;
    };

    # Not great, but needed (per maintainer): run as a fixed system user
    # rather than a DynamicUser.
    users.users.pinchflat = {
      isSystemUser = true;
      group = "pinchflat";
    };
    systemd.services.pinchflat.serviceConfig.User = "pinchflat";
    systemd.services.pinchflat.serviceConfig.DynamicUser = lib.mkForce false;

    # A vhost like every other service — never a raw ip:port link that
    # bypasses the nginx/TLS posture. PinchFlat binds 0.0.0.0 but no firewall
    # rule opens 8945 on any interface; nginx proxies over loopback and the
    # port stays closed.
    services.nginx.virtualHosts."pinchflat.${config.homelab.domain}" =
      import ../lib/proxy-vhost.nix { port = 8945; };
  };
}
