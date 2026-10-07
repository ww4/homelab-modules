{
  description = ''
    homelab-configure — turn a set of answers into a private consumer flake
    for the homelab-modules library. A sub-flake so the library itself keeps
    zero inputs: `nix run 'git+<library-url>?dir=configurator' -- --help`.
  '';

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: lib.genAttrs systems (system: f system nixpkgs.legacyPackages.${system});

      # The library this configurator ships with — the parent directory, at the
      # same revision. Its outputs need no inputs, so a plain import works.
      library = let o = (import ../flake.nix).outputs { self = o; }; in o;

      # Every homelab.* option with its type/description/default, from a full
      # module-system evaluation of ALL library modules (some declare their
      # options locally rather than in options.nix). Only `options` is forced;
      # no config is evaluated.
      optionsJson = system:
        let
          eval = lib.nixosSystem {
            inherit system;
            modules = builtins.attrValues library.nixosModules
              ++ [ { nixpkgs.hostPlatform = system; } ];
          };
          docs = lib.optionAttrSetToDocList eval.options.homelab;
          text = v: if builtins.isAttrs v && v ? text then v.text else toString v;
        in builtins.toJSON (map (o: {
          inherit (o) name type;
          description = o.description or null;
          hasDefault = o ? default;
          default = if o ? default then text o.default else null;
          example = if o ? example then text o.example else null;
        }) (builtins.filter (o: (o.visible or true) && !(o.internal or false)) docs));

      package = system: pkgs: pkgs.rustPlatform.buildRustPackage {
        pname = "homelab-configure";
        version = "0.1.0";
        # Only what the binary is actually built from: a README or a tools/
        # change must not give the installer a new store path, or every stick
        # downloads a "new" installer that is the same program.
        src = lib.fileset.toSource {
          root = ./.;
          fileset = lib.fileset.unions [ ./src ./build.rs ./Cargo.toml ./Cargo.lock ./profiles ];
        };
        cargoLock.lockFile = ./Cargo.lock;

        # Baked into the binary by build.rs — the schema is the checkout's.
        # The commit this binary was built from: shown in the installer's
        # footer and compared with the mirror when it checks for an update.
        HOMELAB_REV = self.rev or "dirty";
        HOMELAB_CATALOG_JSON = pkgs.writeText "catalog.json" (builtins.toJSON library.catalog);
        HOMELAB_OPTIONS_JSON = pkgs.writeText "options.json" (optionsJson system);

        nativeBuildInputs = [ pkgs.makeWrapper ];
        # Key generation, encryption and hashing are delegated to the tools that
        # own those formats rather than re-implemented.
        postInstall = ''
          wrapProgram $out/bin/homelab-configure --prefix PATH : ${lib.makeBinPath [
            pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssh pkgs.authelia pkgs.mkpasswd pkgs.git pkgs.nix
            pkgs.disko pkgs.nixos-install-tools   # `install`: disko + nixos-install + nixos-generate-config
            pkgs.curl                             # `tui`: GitHub key import
            pkgs.util-linux                       # `install`: blkid + swapon after disko
          ]}
        '';

        meta.mainProgram = "homelab-configure";
      };
    in {
      packages = forAll (system: pkgs: rec {
        homelab-configure = package system pkgs;
        default = homelab-configure;
        # The live USB with the configurator on it: `nix build .#iso`.
        iso = import ./iso.nix { inherit nixpkgs system; inherit homelab-configure; rev = self.rev or ""; };
      });

      apps = forAll (system: pkgs: rec {
        configure = {
          type = "app";
          program = lib.getExe self.packages.${system}.homelab-configure;
        };
        default = configure;
      });

      # `nix eval --raw .#optionsJson.x86_64-linux` — the option docs on their
      # own (development builds outside nix read this via HOMELAB_OPTIONS_JSON).
      optionsJson = lib.genAttrs systems optionsJson;

      # ── what `nix flake check` actually enforces ──────────────────────
      #
      # The repository used to claim that `nix flake check` enforced catalog
      # completeness. It did not: the integrity test lives in the library's
      # `catalog` output, and `flake check` printed "unknown flake output
      # 'catalog'" and passed. A promise nothing executes is not a promise.
      checks = forAll (system: pkgs: {
        # Forces the catalog's integrity throw: a module without an entry, an
        # entry without a module, or an entry without an integer `memory`.
        catalog = pkgs.writeText "catalog.json" (builtins.toJSON library.catalog);

        # The configurator builds and its unit tests run (buildRustPackage
        # runs `cargo test` in its check phase).
        configurator = self.packages.${system}.homelab-configure;

        # The option documentation the configurator is built from evaluates.
        options = pkgs.writeText "options.json" (optionsJson system);

        # Every module, enabled, with values a real machine would have. This
        # is the check that catches a module which is only broken once an
        # option is SET: shellcheck runs when writeShellApplication builds.
        modules = (lib.nixosSystem {
          inherit system;
          modules = builtins.attrValues library.nixosModules
            ++ [ ./checks/every-module.nix { nixpkgs.hostPlatform = system; } ];
        }).config.system.build.toplevel;

        # ⚠️ The page's cryptography must be the published library and nothing
        # else. The file carries a provenance header saying which release it
        # came from and that release's hash; this is what makes that claim
        # mechanical rather than a comment nobody checks. Strip the header,
        # which is ours, and hash what is left, which is not.
        tweetnacl = pkgs.runCommand "tweetnacl-is-upstream" { nativeBuildInputs = [ pkgs.coreutils ]; } ''
          want=2555523ab79e980c7aec94aaf6c80e3c120fba04e9c4a95ab9faa7878602380e
          # Only the LEADING header is ours: the library has its own comments
          # further down and they are part of what was reviewed.
          got=$(${pkgs.gawk}/bin/awk 'started || ($0 !~ /^(\/\/.*)?$/) { started=1; print }' ${./src/tweetnacl.js} | sha256sum | cut -d' ' -f1)
          if [ "$got" != "$want" ]; then
            echo "configurator/src/tweetnacl.js is not the reviewed TweetNaCl 1.0.3." >&2
            echo "  expected $want" >&2
            echo "  got      $got" >&2
            echo "Replace the whole file from a published tarball, or update the hash in its header and here." >&2
            exit 1
          fi
          touch $out
        '';

        # ⚠️ Three things about the Hermes container are the image's and not
        # ours, and all three were wrong on the first pass: where its state
        # has to be mounted, what has to be run to keep it alive, and when it
        # may be pointed at local models. Each is a silent failure — a lost
        # state directory, a restart loop, a provider key sent to a port with
        # nothing behind it — so each is asserted here.
        hermes-container =
          let
            hermes = { ollama, key }:
              (lib.nixosSystem {
                inherit system;
                modules = [ library.nixosModules.hermes-agent ]
                  ++ lib.optional ollama library.nixosModules.ollama
                  ++ [
                    { nixpkgs.hostPlatform = system; }
                    { homelab.hermes.environmentFile = if key then "/run/secrets/hermes" else null; }
                  ];
              }).config.virtualisation.oci-containers.containers.hermes-agent;
            plain = hermes { ollama = false; key = false; };
            pointedLocal = args: (hermes args).environment ? OPENAI_BASE_URL;
            cases = [
              { case = { ollama = true; key = false; }; want = true; why = "models on this machine and no credentials: point at them"; }
              { case = { ollama = true; key = true; }; want = false; why = "credentials supplied: a -e here would silently beat the file"; }
              { case = { ollama = false; key = false; }; want = false; why = "no models on this machine: that port has nothing behind it"; }
              { case = { ollama = false; key = true; }; want = false; why = "credentials and no local models: the provider's own address"; }
            ];
            wrong = builtins.filter (t: pointedLocal t.case != t.want) cases;
            # The image's HERMES_HOME. Mounted anywhere else, the state
            # directory is an empty decoration and the real state is thrown
            # away on the next recreate.
            stateMounted = builtins.any (v: lib.hasSuffix ":/opt/data" v) plain.volumes;
            # With no command the image runs the interactive CLI, which has
            # no terminal under systemd and exits; the unit then restarts it
            # forever.
            staysUp = plain.cmd != [ ];
          in
          if wrong != [ ]
          then throw "hermes OPENAI_BASE_URL is wrong where: ${lib.concatMapStringsSep "; " (t: t.why) wrong}"
          else if !stateMounted
          then throw "hermes state is not mounted at /opt/data, so nothing it writes survives a recreate"
          else if !staysUp
          then throw "hermes has no cmd, so the container runs the interactive CLI and restart-loops"
          else pkgs.writeText "hermes-container" "ok\n";

        # No personal names, hosts, domains or addresses in the public tree.
        leak-scan = pkgs.runCommand "leak-scan" { nativeBuildInputs = [ pkgs.bash pkgs.ugrep pkgs.gnugrep pkgs.coreutils pkgs.findutils ]; } ''
          cp -r ${../.} src && chmod -R u+w src && cd src
          bash tools/leak-scan.sh
          touch $out
        '';
      });

      devShells = forAll (system: pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.cargo pkgs.rustc pkgs.rustfmt pkgs.clippy pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssh pkgs.authelia pkgs.mkpasswd ];
        };
      });
    };
}
