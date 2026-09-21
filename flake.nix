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
              pkgs.zsh
            ];
            shellHook = ''
              # Make ``python -m nixorcist`` and direct imports work from the
              # source checkout, matching pytest's configured ``src`` path.
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"

              # Zsh brace expansion silently breaks the DSL collections.
              # Add to ~/.zshrc:  unsetopt brace_expand
              true
            '';
          };
        }
      );

      # Modern overlay attribute name.
      overlays.default = import ./overlay.nix;

      # Backwards-compatible singular name.
      overlay = import ./overlay.nix;
    };
}
