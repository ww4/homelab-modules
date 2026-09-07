# System basics: locale, Nix settings, nixpkgs config.
# (Time zone is a personal value — set time.timeZone in your own flake.)
{ config, lib, pkgs, ... }:

{
  # Internationalisation.
  i18n.defaultLocale = "en_US.UTF-8";
  i18n.extraLocaleSettings = {
    LC_ADDRESS = "en_US.UTF-8";
    LC_IDENTIFICATION = "en_US.UTF-8";
    LC_MEASUREMENT = "en_US.UTF-8";
    LC_MONETARY = "en_US.UTF-8";
    LC_NAME = "en_US.UTF-8";
    LC_NUMERIC = "en_US.UTF-8";
    LC_PAPER = "en_US.UTF-8";
    LC_TELEPHONE = "en_US.UTF-8";
    LC_TIME = "en_US.UTF-8";
  };

  # Allow unfree packages.
  nixpkgs.config.allowUnfree = true;

  # nix-ld: provide a real dynamic loader at /lib64/ld-linux-x86-64.so.2 so
  # generic (non-Nix) dynamically-linked binaries can run. NixOS otherwise
  # ships a stub loader that refuses them with a "cannot run dynamically linked
  # executable" error. Needed for binaries bundled inside VS Code extensions
  # (the auto-fix-vscode-server patcher only fixes VS Code's own server, not
  # extension payloads).
  programs.nix-ld = {
    enable = true;
    libraries = with pkgs; [
      stdenv.cc.cc.lib   # libstdc++ / libgcc_s
      zlib
    ];
  };

  nix = {
    package = pkgs.nixVersions.stable;
    extraOptions = "experimental-features = nix-command flakes";
    optimise = {
      automatic = true;
      dates = [ "03:45" ];
    };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 30d";
    };
  };
}
