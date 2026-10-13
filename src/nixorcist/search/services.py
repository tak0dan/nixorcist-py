"""Link packages to the NixOS options that make them work.

Some software is not just a package: Steam wants ``programs.steam.enable``,
virt-manager is useless without ``virtualisation.libvirtd.enable``, Flatpak
needs ``services.flatpak.enable``.  This module finds those options for a
package using the release's own option metadata, so the answer matches the
release (``services.xserver.displayManager.sddm`` became
``services.displayManager.sddm`` in 24.11, for example).

Signals, strongest first:

1. ``<base>.package`` defaults to this package  ->  ``<base>.enable``
   (and enabling it already installs the package).
2. ``{programs,services,virtualisation,hardware,security}.<name>.enable`` where
   ``<name>`` matches the package name.
3. A small curated table for well-known companions that metadata cannot
   express (virt-manager -> libvirtd).  Entries are only offered when the
   option exists on this release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .core import Candidate, norm
from .nixpkgs import PackageIndex

_PREFIXES = ("programs", "services", "virtualisation", "hardware", "security")

# package attr -> companion options that metadata alone cannot reveal
COMPANIONS: dict[str, list[tuple[str, str]]] = {
    "virt-manager": [
        ("programs.virt-manager.enable", "virt-manager's NixOS module"),
        ("virtualisation.libvirtd.enable", "virt-manager needs the libvirtd daemon"),
    ],
    "gnome-boxes": [("virtualisation.libvirtd.enable", "Boxes needs the libvirtd daemon")],
    "docker": [("virtualisation.docker.enable", "runs the docker daemon")],
    "docker-compose": [("virtualisation.docker.enable", "needs the docker daemon")],
    "podman": [("virtualisation.podman.enable", "rootless containers and the podman socket")],
    "virtualbox": [("virtualisation.virtualbox.host.enable", "kernel modules and the vboxusers group")],
    "flatpak": [("services.flatpak.enable", "system Flatpak support and portals")],
    "sddm": [
        ("services.displayManager.sddm.enable", "sddm is a display manager service"),
        ("services.xserver.displayManager.sddm.enable", "sddm (releases before 24.11)"),
    ],
    "kdePackages.sddm": [("services.displayManager.sddm.enable", "sddm is a display manager service")],
    "gdm": [("services.displayManager.gdm.enable", "gdm is a display manager service"),
            ("services.xserver.displayManager.gdm.enable", "gdm (releases before 25.05)")],
    "tailscale": [("services.tailscale.enable", "runs tailscaled")],
    "openssh": [("services.openssh.enable", "runs sshd")],
    "syncthing": [("services.syncthing.enable", "runs syncthing as a service")],
    "wireshark": [("programs.wireshark.enable", "capture permissions via the wireshark group")],
    "steam": [("programs.steam.enable", "32-bit libraries, udev rules and firewall options")],
    "gamemode": [("programs.gamemode.enable", "the gamemoded daemon")],
    "hyprland": [("programs.hyprland.enable", "session file, portals and polkit")],
    "sway": [("programs.sway.enable", "session file, polkit and security wrappers")],
}


@dataclass
class ServiceLink:
    option: str
    reason: str
    installs_package: bool = False
    verified: bool = True  # option exists on this release
    signal: str = ""

    def __hash__(self) -> int:
        return hash(self.option)


@dataclass
class ServiceReport:
    package: str
    links: list[ServiceLink] = field(default_factory=list)

    @property
    def recommended(self) -> list[ServiceLink]:
        return [l for l in self.links if l.verified]


_PKG_DEFAULT = re.compile(r"^\s*(?:pkgs\.)?([A-Za-z_][\w.'-]*)\s*$")


def services_for(index: PackageIndex, package: Candidate | str) -> ServiceReport:
    attr = package if isinstance(package, str) else package.name
    cand = index.get(attr) if isinstance(package, str) else package
    names = {norm(attr.rsplit(".", 1)[-1])}
    if cand is not None and cand.pname:
        names.add(norm(cand.pname))
    have_options = index.option_count > 0
    report = ServiceReport(attr)
    seen: dict[str, ServiceLink] = {}

    def add(link: ServiceLink) -> None:
        if link.option in seen:
            prev = seen[link.option]
            prev.installs_package = prev.installs_package or link.installs_package
            return
        seen[link.option] = link
        report.links.append(link)

    if have_options:
        # 1. <base>.package defaults to this package
        for name, _type, _desc, default in index.options("%.package"):
            m = _PKG_DEFAULT.match(default or "")
            if not m or m.group(1) != attr:
                continue
            base = name[: -len(".package")]
            if index.options(base + ".enable"):
                add(ServiceLink(base + ".enable", f"{name} defaults to pkgs.{attr}",
                                installs_package=True, signal="package-option"))
        # 2. option named after the package
        for name, type_, _desc, _default in index.options("%.enable"):
            parts = name.split(".")
            if len(parts) != 3 or parts[0] not in _PREFIXES or type_ not in ("boolean", "bool", ""):
                continue
            if norm(parts[1]) in names:
                add(ServiceLink(name, f"NixOS option named after {attr}", signal="name"))

    # 3. curated companions
    for option, why in COMPANIONS.get(attr, []):
        exists = bool(index.options(option)) if have_options else False
        if have_options and not exists:
            continue  # not on this release
        add(ServiceLink(option, why, verified=exists, signal="curated"))
    return report
