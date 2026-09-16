{
  description = "Higher-level Nix package/profile management and configuration promotion tool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in {
      packages = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          default = pkgs.callPackage ./default.nix { };
        }
      );

      devShells = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          default = pkgs.mkShell {
            inputsFrom = [ self.packages.${system}.default ];
            packages = [
              pkgs.python313
              pkgs.python313.pkgs.pytest
              pkgs.nix
            ];
          };
        }
      );

      # Modern overlay attribute name.
      overlays.default = import ./overlay.nix;

      # Backwards-compatible singular name.
      overlay = import ./overlay.nix;
    };
}