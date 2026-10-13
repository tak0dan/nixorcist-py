"""Search engine: a distribution-agnostic core (:mod:`.core`, :mod:`.picker`)
plus the version-aware nixpkgs backend (:mod:`.nixpkgs`, :mod:`.services`,
:mod:`.resolve`).  ``uni`` reuses the core with imperative backends."""

from .core import Candidate, rank

__all__ = ["Candidate", "rank"]
