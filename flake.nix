{
  description = ''
    homelab-modules — an option-driven NixOS module library for a self-hosted
    homelab. Implementations live here; personal values (domain, addresses,
    secrets) live in the consumer's own flake. See README.md.
  '';

  outputs = { self }:
  let
    nixosModules = self.nixosModules;
    catalog = import ./modules/catalog.nix;
    # The catalog must describe exactly the modules exported below — a module
    # without an entry (or an entry without a module) is an eval error, not a
    # silently incomplete index.
    moduleNames = builtins.attrNames nixosModules;
    catalogNames = builtins.attrNames catalog;
    missing = builtins.filter (n: !(catalog ? ${n})) moduleNames;
    extra = builtins.filter (n: !(nixosModules ? ${n})) catalogNames;
    checkedCatalog =
      if missing != [ ] then throw "catalog.nix lacks entries for: ${toString missing}"
      else if extra != [ ] then throw "catalog.nix names modules that do not exist: ${toString extra}"
      else catalog;
  in {
    # Machine-readable index of the modules: `nix eval --json .#catalog`.
    catalog = checkedCatalog;

    nixosModules = {
      # The homelab.* option set — the interface between this library and a
      # consumer's values. Modules that need it import it themselves (the
      # module system dedupes by path), so consumers rarely import it directly.
      options = ./modules/options.nix;

      # Base system.
      system = ./modules/base/system.nix;
      boot = ./modules/base/boot.nix;

      # Services & tooling.
      mergerfs-pools = ./modules/services/mergerfs-pools.nix;
      authelia = ./modules/services/authelia.nix;
      monitoring = ./modules/services/monitoring;
      pool-autoremount = ./modules/services/pool-autoremount.nix;
      drive-temps = ./modules/services/drive-temps.nix;
      deploy-drift-watch = ./modules/services/deploy-drift-watch;
      mirror-drift-watch = ./modules/services/mirror-drift-watch;
      nginx-access = ./modules/services/nginx-access.nix;
      jellyfin = ./modules/services/jellyfin.nix;
      audiobookshelf = ./modules/services/audiobookshelf.nix;
      tandoor = ./modules/services/tandoor.nix;
      pinchflat = ./modules/services/pinchflat.nix;
      uptime-kuma = ./modules/services/uptime-kuma.nix;
      glances = ./modules/services/glances.nix;
      acme = ./modules/services/acme.nix;
      nextcloud = ./modules/services/nextcloud.nix;
      forgejo = ./modules/services/forgejo.nix;
      ntfy = ./modules/services/ntfy.nix;
      paperless = ./modules/services/paperless.nix;
      vaultwarden = ./modules/services/vaultwarden.nix;
      immich = ./modules/services/immich.nix;
      metube = ./modules/services/metube.nix;
      alertmanager-ntfy = ./modules/services/alertmanager-ntfy.nix;
      lidarr = ./modules/services/lidarr.nix;
      lazylibrarian = ./modules/services/lazylibrarian.nix;
      aurral = ./modules/services/aurral.nix;
      unpackerr = ./modules/services/unpackerr.nix;
      recyclarr = ./modules/services/recyclarr.nix;
      arr = ./modules/services/arr.nix;
      silverbullet = ./modules/services/silverbullet.nix;
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
