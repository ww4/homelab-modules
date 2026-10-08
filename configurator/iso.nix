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
        # qrencode is NOT here any more: it is on the binary's own PATH, where
        # the rest of the tools it shells out to live. Leaving it to the ISO
        # meant the dependency held only by accident, and when it was missing
        # the Welcome screen drew a hole where the square belongs and said
        # nothing about it.
        environment.systemPackages = [ homelab-configure pkgs.git pkgs.curl pkgs.jq ];
        # The browser installer: the same wizard, served to any computer on the
        # network so a long token can be pasted instead of typed.
        # The browser installer answers computers on this network and nobody
        # else. The server refuses a foreign peer itself, before it reads a
        # byte; this is the same rule one layer down, so a household router
        # forwarding a port cannot put the form on the internet even for the
        # moment it takes the server to hang up. The ranges are the private
        # ones, link-local, and the tailnet, which this project treats as a
        # way in from elsewhere.
        networking.firewall.extraCommands = ''
          for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
            iptables -I nixos-fw -p tcp --dport 8099 -s "$net" -j nixos-fw-accept
          done
          for net in ::1/128 fc00::/7 fe80::/10; do
            ip6tables -I nixos-fw -p tcp --dport 8099 -s "$net" -j nixos-fw-accept
          done
        '';
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
            marker=$(curl -fsS --max-time 10 https://homelab-installer.nyc3.digitaloceanspaces.com/cache/latest-installer.json 2>/dev/null || true)
            latest=$(echo "$marker" | jq -r .rev 2>/dev/null || true)
            if [ -z "$latest" ] || [ "$latest" = "null" ]; then
              echo "Could not check for a newer installer; starting the one on this stick."
              homelab-configure tui
            elif [ -n "$baked" ] && [ "$latest" = "$baked" ]; then
              echo "This stick already has the current installer."
              homelab-configure tui
            else
              # Say what is about to replace this program before replacing it.
              echo "A newer installer is available."
              echo "  commit:    $latest"
              echo "  built as:  $(echo "$marker" | jq -r '.store_path // "unknown"')"
              echo "  published: $(echo "$marker" | jq -r '.published // "unknown"')"
              echo "  accepted only if signed by: homelab-installer-1"
              echo "Fetching it (about a minute)... Ctrl-C skips it and uses the installer on this stick."
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
