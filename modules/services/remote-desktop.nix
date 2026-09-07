# Remote desktop via xrdp + XFCE — Tailscale-only.
#
# GNOME 50 dropped its X11 session target and gnome-remote-desktop's --system
# mode is broken on NixOS (the Enable dbus method calls `pkexec systemctl
# enable` against read-only /etc/systemd/system). XFCE under xrdp is the
# reliable, battle-tested combo for "remote Linux GUI" so that's what we use.
# The local desktop session on the console is untouched.
{ config, lib, pkgs, ... }:
let
  # Wrap xfce4-session so it gets a private dbus session bus and a sane env
  # when launched by xrdp (no inherited login session).
  xrdpStartXfce = pkgs.writeShellScript "xrdp-startxfce" ''
    . /etc/profile
    export XDG_SESSION_TYPE=x11
    export XDG_CURRENT_DESKTOP=XFCE
    export DESKTOP_SESSION=xfce
    exec ${pkgs.dbus}/bin/dbus-run-session -- ${pkgs.xfce.xfce4-session}/bin/xfce4-session
  '';
in {
  # Install XFCE alongside the existing desktop (login chooser will offer both,
  # but local users keep their session; xrdp sessions get XFCE).
  services.xserver.desktopManager.xfce.enable = true;

  services.xrdp = {
    enable = true;
    defaultWindowManager = "${xrdpStartXfce}";
    openFirewall = false;
  };

  # RDP only over Tailscale; never over WAN or LAN.
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 3389 ];
}
