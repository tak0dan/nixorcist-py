# Nixorcist package expression — callPackage-compatible, no flake required.
#
# Use from configuration.nix as an overlay package (NOT a flake input):
#
#   nixpkgs.overlays = [ (final: prev: {
#     nixorcist = prev.callPackage /home/tak_1/nixorcist { };
#   }) ];
#   environment.systemPackages = [ pkgs.nixorcist ];
#
# or directly:  nix profile install "path:/home/tak_1/nixorcist#default"

{ lib
, python3
, fetchFromGitHub
}:

python3.pkgs.buildPythonApplication rec {
  pname = "nixorcist";
  version = "0.1.0";
  format = "pyproject";

  src = fetchFromGitHub {
    owner = "tak0dan";
    repo = "nixorcist-py";
    rev = "8f63973b7cef3631f7d14f755603c34e783d7ce9";
    sha256 = "Q0tidGA+XAeLOX6eE2NZX8JiDFfwZrvh9LR0FZdwZwQ=";
  };

  nativeBuildInputs = [ python3.pkgs.setuptools ];

  postInstall = ''
    mkdir -p $out/share/zsh/site-functions
    cp ${src}/src/nixorcist/cli/_nist $out/share/zsh/site-functions/_nist
  '';

  pythonImportsCheck = [ "nixorcist" ];

  meta = with lib; {
    description = "Higher-level Nix package/profile management and configuration promotion tool";
    homepage = "https://github.com/tak0dan/nixorcist-py";
    license = licenses.mit;
    maintainers = [ ];
    platforms = platforms.linux;
    mainProgram = "nixorcist";
  };
}