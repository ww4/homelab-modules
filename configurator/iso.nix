# The installer ISO: the stock NixOS minimal installer with homelab-configure
# preinstalled, so "plug in a live USB" needs no URL, no compile, and no
# network beyond what the install itself fetches. Boot it, log in as root,
# type `homelab-configure tui`.
#
#   nix build .#iso   (from configurator/)  → result/iso/*.iso
#
# Built from the same nixpkgs rev the generated flakes pin, so the live
# system and the installed one share a store where it matters (the install
# copies the closure the live system already evaluated).
{ nixpkgs, homelab-configure, system }:

let
  iso = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      "${nixpkgs}/nixos/modules/installer/cd-dvd/installation-cd-minimal.nix"
      ({ pkgs, lib, ... }: {
        isoImage.isoName = lib.mkForce "homelab-installer-${system}.iso";
        isoImage.volumeID = lib.mkForce "HOMELAB";
        environment.systemPackages = [ homelab-configure pkgs.git pkgs.curl pkgs.jq ];
        nix.settings.experimental-features = [ "nix-command" "flakes" ];
        # The one thing the installer prints: what to type.
        services.getty.helpLine = lib.mkForce ''

          Homelab installer. Get on the network (wired is automatic; `wpa_cli` for Wi-Fi), then:

              homelab-configure tui            # answer the questions, press g
              sudo homelab-configure install ./my-homelab

          Everything it writes lives in RAM until `install` copies it to the new system.
        '';
        # Keep the ISO small-ish: no docs, no manual.
        documentation.enable = false;
        documentation.nixos.enable = false;
      })
    ];
  };
in
iso.config.system.build.isoImage
