# Bootloader and power behaviour for an always-on server.
{ config, lib, pkgs, ... }:

{
  # Bootloader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot/efi";

  # Disable the boot-menu kernel-cmdline editor: with console access it
  # otherwise lets anyone append e.g. `init=/bin/sh` for an unauthenticated
  # root shell. On a box with no disk encryption, console access is already
  # powerful — but this closes the trivial, no-tooling path. Takes effect on
  # the next `switch` (bootloader install).
  boot.loader.systemd-boot.editor = false;

  # This box is a server — never let it sleep. Disable the sleep/suspend
  # systemd targets and block suspend/hibernate at the polkit level so
  # nothing can trigger it.
  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;

  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.login1.suspend" ||
            action.id == "org.freedesktop.login1.suspend-multiple-sessions" ||
            action.id == "org.freedesktop.login1.hibernate" ||
            action.id == "org.freedesktop.login1.hibernate-multiple-sessions")
        {
            return polkit.Result.NO;
        }
    });
  '';
}
