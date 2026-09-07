{
  description = ''
    homelab-modules — an option-driven NixOS module library for a self-hosted
    homelab. Implementations live here; personal values (domain, addresses,
    secrets) live in the consumer's own flake. See README.md.
  '';

  outputs = { self }: {
    nixosModules = {
      # The homelab.* option set — the interface between this library and a
      # consumer's values. Modules that need it import it themselves (the
      # module system dedupes by path), so consumers rarely import it directly.
      options = ./modules/options.nix;

      # Base system.
      system = ./modules/base/system.nix;
      boot = ./modules/base/boot.nix;

      # Services & tooling.
      remote-desktop = ./modules/services/remote-desktop.nix;
      meshagent = ./modules/services/meshagent;
      decluttarr = ./modules/services/decluttarr.nix;
      nginx-log-paths-check = ./modules/services/nginx-log-paths-check.nix;
      smart-dump = ./modules/services/smart-dump.nix;
      qbit-vpn-watchdog = ./modules/services/qbit-vpn-watchdog.nix;
      arr-missing-sweep = ./modules/services/arr-missing-sweep.nix;
      disk-io-watch = ./modules/services/disk-io-watch.nix;
    };
  };
}
