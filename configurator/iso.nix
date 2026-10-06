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
{ nixpkgs, homelab-configure, system, rev ? "" }:

let
  iso = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      "${nixpkgs}/nixos/modules/installer/cd-dvd/installation-cd-minimal.nix"
      ({ pkgs, lib, ... }: {
        isoImage.isoName = lib.mkForce "homelab-installer-${system}.iso";
        isoImage.volumeID = lib.mkForce "HOMELAB";
        environment.systemPackages = [ homelab-configure pkgs.git pkgs.curl pkgs.jq pkgs.qrencode ];
        # The browser installer: the same wizard, served to any computer on the
        # network so a long token can be pasted instead of typed.
        networking.firewall.allowedTCPPorts = [ 8099 ];
        networking.hostName = lib.mkForce "homelab-installer";
        services.avahi = { enable = true; publish.enable = true; publish.addresses = true; nssmdns4 = true; };
        nix.settings.experimental-features = [ "nix-command" "flakes" ];
        # The newest configurator comes from the project's binary cache, so a
        # stick burned months ago still runs today's installer: `nix run` of
        # the library's configurator, substituted (never compiled) from the
        # cache on Spaces, signed with the key below (--max-jobs 0: download or
        # nothing, never a compile on a live USB). No network, or anything else
        # wrong: the copy baked into the ISO runs instead.
        nix.settings.substituters = [ "https://cache.nixos.org" "https://homelab-installer.nyc3.digitaloceanspaces.com/cache" ];
        nix.settings.trusted-public-keys = [
          "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
          "homelab-installer-1:vA5hnA0yEKfAfOOxleHWVvsGBJnZAKsW+EeumRPvMes="
        ];
        # The installer starts by itself on the first console after the
        # auto-login (once per login; Ctrl-Q leaves it and the shell is there).
        programs.bash.loginShellInit = ''
          if [ "$(tty 2>/dev/null)" = /dev/tty1 ] && [ -z "$HOMELAB_TUI_STARTED" ]; then
            export HOMELAB_TUI_STARTED=1
            # The network: DHCP is still negotiating when this shell starts.
            # Wait up to 10 s quietly, then say what is missing and keep
            # looking every 3 s (a cable plugged in now is picked up).
            has_addr() { ip -4 route get 1.1.1.1 >/dev/null 2>&1; }
            n=0
            until has_addr || [ $n -ge 10 ]; do sleep 1; n=$((n+1)); done
            if ! has_addr; then
              echo "No network address yet. Plug in a network cable (wired is automatic; Wi-Fi: wpa_cli)."
              echo "Waiting for one... (Ctrl-C to go on without, with the installer from this stick)"
              until has_addr; do sleep 3; done
            fi
            echo "Network: $(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
            # Only fetch when the library has actually moved: this stick was
            # built from ${if rev == "" then "an untracked tree" else rev}, and a `nix run` costs a
            # minute of evaluation even when nothing changed.
            #
            # Ask the binary CACHE, not the git mirror. The mirror has a commit
            # seconds after a merge; the closure lands here minutes later, and
            # `--max-jobs 0` cannot build what it cannot fetch. The marker is
            # written only once the closure is up.
            baked=${if rev == "" then "" else rev}
            latest=$(curl -fsS --max-time 10 https://homelab-installer.nyc3.digitaloceanspaces.com/cache/latest-installer.json 2>/dev/null | jq -r .rev 2>/dev/null || true)
            if [ -z "$latest" ] || [ "$latest" = "null" ]; then
              echo "Could not check for a newer installer; starting the one on this stick."
              homelab-configure tui
            elif [ -n "$baked" ] && [ "$latest" = "$baked" ]; then
              echo "This stick already has the current installer."
              homelab-configure tui
            else
              echo "A newer installer is available; fetching it (about a minute)... Ctrl-C skips it."
              if ! nix run --refresh --no-write-lock-file --max-jobs 0 'github:ww4/homelab-modules?dir=configurator' -- tui; then
                echo "Could not fetch it; starting the installer on this stick."
                homelab-configure tui
              fi
            fi
          fi
        '';
        services.getty.helpLine = lib.mkForce ''

          Homelab installer. Wired network is automatic (`wpa_cli` for Wi-Fi).
          The installer opens by itself on this console; `homelab-configure tui` opens it again.
          Everything it writes lives in RAM until the install copies it to the new system.
        '';
        # Kernel messages scribble over the wizard on tty1 (a block-layer line
        # appeared across the Finished screen in the rehearsal): errors only.
        boot.consoleLogLevel = 3;
        # Keep the ISO small-ish: no docs, no manual.
        documentation.enable = false;
        documentation.nixos.enable = false;
      })
    ];
  };
in
iso.config.system.build.isoImage
