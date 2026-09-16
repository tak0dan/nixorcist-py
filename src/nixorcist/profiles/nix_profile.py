"""Re-export of the ``nix profile`` backend at the spec's
``nixorcist.profiles`` location (spec §6)."""

from ..backends.profile import NixProfile

__all__ = ["NixProfile"]