# The machine `nix flake check` builds: every module in the library turned on,
# with values a real installation would have.
#
# ⚠️ WHY VALUES AND NOT DEFAULTS. Several modules interpolate an option into a
# shell program, and `writeShellApplication` runs shellcheck at build time. A
# script can therefore be correct with the default and broken the moment the
# option is set: `[ -z "${cfg.repoUrl}" ]` is a fine test when the URL is
# empty and a shellcheck error (SC2157, an always-false test) when it is not.
# That exact bug shipped, and `nix flake check` passed the whole time, because
# nothing in it set a URL. A check that only exercises defaults checks the one
# configuration nobody runs.
#
# The values here are deliberately plausible rather than minimal: a domain, a
# pool with branches, a repository URL, credential paths. None of the files
# need to exist; nothing in this evaluation reads them.
{ lib, ... }:
{
  # A machine shaped enough to evaluate. Nothing here is ever booted.
  # The library's own `boot` module picks the bootloader, so this fixture
  # must not pick a second one.
  fileSystems."/" = { device = "/dev/disk/by-label/nixos"; fsType = "ext4"; };
  fileSystems."/boot" = { device = "/dev/disk/by-label/ESP"; fsType = "vfat"; };
  system.stateVersion = "26.05";
  users.users.operator = { isNormalUser = true; group = "users"; };

  homelab = {
    domain = "example.com";
    adminUser = "operator";

    acme = { email = "admin@example.com"; credentialsFile = "/run/secrets/acme"; };
    backup = {
      passwordFile = "/run/secrets/restic";
      local = { enable = true; repository = "/srv/restic"; requiresMountsFor = [ "/srv" ]; };
      remote = { enable = true; repository = "s3:s3.example.com/bucket"; environmentFile = "/run/secrets/b2"; };
      sftpPush.enable = true;
    };

    # snapraid derives its data disks from the pool's members, so the pool
    # has to be described the way a machine with parity describes it.
    pools.media = {
      mountpoint = "/mnt/media";
      branches = "/mnt/d*";
      memberDir = "/mnt";
      members = [ "d1" "d2" ];
    };
    snapraid = {
      enable = true;
      pool = "media";
      parityFiles = [ "/mnt/parity/snapraid.parity" ];
    };

    # The one that shipped broken: a URL that is actually set.
    deployDriftWatch = { enable = true; repoUrl = "https://git.example.com/me/flakes.git"; };
    mirrorDriftWatch.pairs = [ { name = "media"; source = "/mnt/media"; mirror = "/mnt/mirror"; } ];

    arrStack = { vpnProvider = "mullvad"; vpnEnvFile = "/run/secrets/vpn"; };
    arrMissingSweep.apiEnvFile = "/run/secrets/arr-api";
    aurral.envFile = "/run/secrets/aurral";
    decluttarr.envFile = "/run/secrets/decluttarr";
    unpackerr.envFile = "/run/secrets/unpackerr";
    recyclarr.configFile = "/run/secrets/recyclarr.yml";
    vaultwarden.envFile = "/run/secrets/vaultwarden";
    nextcloud.adminPasswordFile = "/run/secrets/nextcloud";
    paperless.adminPasswordFile = "/run/secrets/paperless";
    meshagent.mshFile = "/run/secrets/agent.msh";
    hermes.environmentFile = "/run/secrets/hermes";
    monitoring.enable = true;
  };
}
