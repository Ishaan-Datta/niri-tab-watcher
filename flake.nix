{
  description = "Debounced previous-window restoration for niri";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    let
      overlay = final: _prev: {
        niri-tab-watcher = final.writeShellApplication {
          name = "niri-tab-watcher";
          runtimeInputs = [
            final.niri
            final.python3
          ];
          text = ''
            exec python3 ${./niri_tab_watcher.py} "$@"
          '';
          meta = {
            description = "Restore niri's previous stable window and viewport orientation";
            license = final.lib.licenses.gpl2Only;
            mainProgram = "niri-tab-watcher";
            platforms = final.lib.platforms.linux;
          };
        };
      };
    in
    {
      overlays.default = overlay;
    }
    //
      flake-utils.lib.eachSystem
        [
          "x86_64-linux"
          "aarch64-linux"
        ]
        (
          system:
          let
            pkgs = import nixpkgs {
              inherit system;
              overlays = [ overlay ];
            };
          in
          {
            packages.default = pkgs.niri-tab-watcher;
            packages.niri-tab-watcher = pkgs.niri-tab-watcher;

            apps.default = {
              type = "app";
              program = "${pkgs.niri-tab-watcher}/bin/niri-tab-watcher";
              meta.description = "Restore niri's previous stable window";
            };
            apps.niri-tab-watcher = self.apps.${system}.default;

            checks.unit-tests =
              pkgs.runCommand "niri-tab-watcher-tests"
                {
                  nativeBuildInputs = [ pkgs.python3 ];
                }
                ''
                  export PYTHONDONTWRITEBYTECODE=1
                  PYTHONPATH=${self} python3 -m unittest discover -s ${self}/tests -v
                  touch "$out"
                '';

            devShells.default = pkgs.mkShell {
              packages = [
                pkgs.niri
                pkgs.python3
                pkgs.ruff
              ];
            };

            formatter = pkgs.nixfmt;
          }
        );
}
