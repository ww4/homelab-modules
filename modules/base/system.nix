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
      # DAILY, not weekly. On the reference box the root filesystem reached 96%
      # with 21 GB free, and a GC run freed 23.3 GiB in one pass — all of it
      # accumulated since the previous weekly run the day before. Agent build
      # activity (`nixos-rebuild build` while validating a PR) churns the store
      # at roughly that rate, so a weekly collector is a week behind a daily
      # problem. Daily costs a few minutes of IO at 03:15 and keeps the trend
      # flat instead of sawtoothing into the disk-space alert.
      dates = "daily";
      randomizedDelaySec = "45min";
      # 14d rather than 30d: nothing here has ever needed a month-old
      # generation to roll back to, and each retained generation holds the
      # deltas of a full system closure.
      options = "--delete-older-than 14d";
    };
  };

  # Cap the journal. There was no explicit limit, so systemd's default applies:
  # 10% of the filesystem, which on a 450 GB root is ~44 GB it is
  # entitled to grow into. It was sitting at 3.9 GB. 1 GB is still weeks of
  # history on these hosts and bounds a store that nothing else bounds.
  #
  # ⚠️ Chosen as a DECLARATIVE cap rather than a one-off `journalctl
  # --vacuum-size`, because the agent cannot run that: it is in the
  # systemd-journal group, so it can READ the journal but not delete archived
  # files (Permission denied, verified 2026-10-06). A fix only root can apply
  # by hand is a fix that stops being applied.
  services.journald.extraConfig = ''
    SystemMaxUse=1G
    SystemKeepFree=10G
  '';
}
