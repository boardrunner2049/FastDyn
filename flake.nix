{
  description = "FastDyn development environment and documentation";

  inputs = {
    rumoca.url = "github:cognipilot/rumoca/21843c115cd4b4c7fa02011d18a6c1e501ebb387";
    nixpkgs.follows = "rumoca/nixpkgs";
  };

  outputs = { self, nixpkgs, rumoca, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = import ./nix/python.nix { inherit pkgs; };
      qemu = import ./nix/qemu.nix { inherit pkgs; };
      compiler = rumoca.packages.${system}.rumoca;
      docsAssets = import ./nix/docs-assets.nix { inherit pkgs; };
      mavlink = pkgs.fetchFromGitHub {
        owner = "mavlink";
        repo = "c_library_v2";
        rev = "d1beb068135e069d3fe28a53fc171700a632e06a";
        hash = "sha256-4bh8qlGHaDkJCjrRjxdf7MaWCzWlPgq4H1QD9OF/UQk=";
      };
      plugin = pkgs.stdenv.mkDerivation {
        pname = "fastdyn-plugin";
        version = "0.1.0";
        # Documentation and Python changes must not rebuild the native plugin.
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./meson.build ./meson_options.txt ./config.h.in
            (pkgs.lib.fileset.intersection
              (pkgs.lib.fileset.fileFilter
                (file: file.hasExt "c" || file.hasExt "h" || file.name == "meson.build") ./.)
              (pkgs.lib.fileset.unions [ ./core ./include ./libs ./utils ./virtuals ./device_models ]))
          ];
        };
        nativeBuildInputs = with pkgs; [ meson ninja pkg-config ];
        buildInputs = with pkgs; [ glib cjson ];
        postPatch = ''
          mkdir -p third_party/courbet_deps/mavlink_headers
          cp -r ${mavlink}/. third_party/courbet_deps/mavlink_headers/
        '';
        mesonFlags = [
          "-Dqemu_path=${qemu}" "-Denable_phy=true" "-Denable_fmu=true"
          "-Denable_flight_controllers=true"
        ];
        installPhase = ''
          mkdir -p "$out/lib"
          cp libfastdyn.so "$out/lib/"
        '';
      };
      fastdyn = pkgs.writeShellScriptBin "fastdyn" ''
        if [ -d "$PWD/src/fastdyn" ]; then
          export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
        else
          export PYTHONPATH="${./src}''${PYTHONPATH:+:$PYTHONPATH}"
        fi
        exec ${python}/bin/python -c 'from fastdyn.main import cli; cli()' "$@"
      '';
      configure = pkgs.writeShellScriptBin "fastdyn-config" ''
        export PYTHONPATH="$PWD/src:${./src}''${PYTHONPATH:+:$PYTHONPATH}"
        exec ${python}/bin/python -c 'from fastdyn.configure import main; main()' \
          --qemu ${qemu}/bin/qemu-system-arm \
          --plugin ${plugin}/lib/libfastdyn.so \
          --compiler ${compiler}/bin/rumoca "$@"
      '';
      developmentShell = pkgs.mkShell {
        packages = with pkgs; [
          bashInteractive coreutils git util-linux universal-ctags lsof
          cmake ninja meson pkg-config stdenv.cc glib cjson
          python compiler qemu fastdyn configure mdbook docsAssets
        ];
        shellHook = ''
          export PYTHONPATH="$PWD/src:${./src}''${PYTHONPATH:+:$PYTHONPATH}"
        '';
      };
      devContainer = import ./nix/dev-container.nix {
        inherit pkgs;
        devShell = developmentShell;
      };
    in {
      packages.${system} = { inherit python qemu plugin devContainer; rumoca = compiler; };
      devShells.${system} = {
        default = developmentShell;
        docs = pkgs.mkShell {
          packages = [ pkgs.mdbook docsAssets (pkgs.python312.withPackages (ps: [ ps.tomli-w ])) ];
        };
      };
    };
}
