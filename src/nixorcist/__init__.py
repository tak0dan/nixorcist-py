"""Nixorcist - a higher-level package and configuration management layer on top of Nix.

Nixorcist organizes intent.  Nix performs the actual package management.
"""

__version__ = "0.1.0"

from .cli.diagnostics import NixorcistError, ErrorCode

__all__ = ["__version__", "NixorcistError", "ErrorCode"]