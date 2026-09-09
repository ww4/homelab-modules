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
        src = ./.;
        cargoLock.lockFile = ./Cargo.lock;

        # Baked into the binary by build.rs — the schema is the checkout's.
        HOMELAB_CATALOG_JSON = pkgs.writeText "catalog.json" (builtins.toJSON library.catalog);
        HOMELAB_OPTIONS_JSON = pkgs.writeText "options.json" (optionsJson system);

        nativeBuildInputs = [ pkgs.makeWrapper ];
        # Key generation, encryption and hashing are delegated to the tools that
        # own those formats rather than re-implemented.
        postInstall = ''
          wrapProgram $out/bin/homelab-configure --prefix PATH : ${lib.makeBinPath [
            pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssh pkgs.authelia pkgs.mkpasswd pkgs.git pkgs.nix
          ]}
        '';

        meta.mainProgram = "homelab-configure";
      };
    in {
      packages = forAll (system: pkgs: rec {
        homelab-configure = package system pkgs;
        default = homelab-configure;
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

      devShells = forAll (system: pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.cargo pkgs.rustc pkgs.rustfmt pkgs.clippy pkgs.sops pkgs.age pkgs.ssh-to-age pkgs.openssh pkgs.authelia pkgs.mkpasswd ];
        };
      });
    };
}
