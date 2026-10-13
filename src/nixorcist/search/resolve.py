"""Turn what the user typed into attributes that exist on *their* release.

The "new user" path: ``discord`` -> the default candidate, no questions asked,
unless the name does not exist on this release and there is no single obvious
replacement, in which case they must choose.

The "experienced user" path (``pick=True``): always show the ranked
candidates -- default, variants and similarly named packages -- and let them
choose one or several.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .core import Candidate, variants
from .nixpkgs import PackageIndex, SearchError
from .picker import pick as interactive_pick
from .services import ServiceLink, services_for


@dataclass
class Resolution:
    query: str
    chosen: list[Candidate]
    note: str = ""
    services: dict[str, list[ServiceLink]] = field(default_factory=dict)

    @property
    def attrs(self) -> list[str]:
        return [c.name for c in self.chosen]


def resolve_query(
    index: PackageIndex,
    query: str,
    *,
    pick: bool = False,
    declared: Iterable[str] = (),
    limit: int = 40,
) -> Resolution:
    query = query.removeprefix("pkgs.")
    ranked = index.search(query, declared=declared, limit=limit)
    if not ranked:
        raise SearchError(f"no package matches {query!r} on {index.release.channel if index.release else 'this release'}")
    default = next((c for c in ranked if c.is_default), None)
    if pick:
        if default is not None:
            # show the default first, then its own variants, then the rest
            fam = variants(ranked, default)
            rest = [c for c in ranked if c is not default and c not in fam]
            ranked = [default, *fam, *rest]
        header = f"{query}: ★ marks what a plain `{query}` declaration gets on this release"
        chosen = interactive_pick(ranked, prompt=query, header=header)
        if not chosen:
            raise SearchError(f"nothing selected for {query!r}")
        return Resolution(query, chosen)
    if default is None:
        top = ", ".join(c.name for c in ranked[:5])
        raise SearchError(
            f"{query!r} is not a package on this release and has no single replacement",
            suggestion=f"pick one with --pick (closest: {top})",
        )
    note = default.note or ("" if default.name == query else f"'{query}' resolves to '{default.name}' on this release")
    return Resolution(query, [default], note=note)


def attach_services(index: PackageIndex, resolution: Resolution) -> Resolution:
    for c in resolution.chosen:
        links = services_for(index, c).recommended
        if links:
            resolution.services[c.name] = links
    return resolution
