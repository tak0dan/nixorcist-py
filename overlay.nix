# Standalone overlay so nixorcist can be added to configuration.nix as a plain
# package (no flake inputs required):
#
#   nixpkgs.overlays = [ (import /home/tak_1/nixorcist/overlay.nix) ];
#   environment.systemPackages = [ pkgs.nixorcist ];

final: prev: {
  nixorcist = prev.callPackage ./default.nix { };
}