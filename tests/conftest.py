"""Shared fixtures for the Nixorcist test-suite.

Everything threads through the corrected *orthogonal* state model
(``active × backend``): promotion/demotion change ``backend`` only;
activation/deactivation change ``active`` only; package membership and
profile installation are fully independent (spec §66 corrected model).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import nixorcist  # noqa: E402  (must be importable after src is on sys.path)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


@pytest.fixture
def logger():
    from nixorcist.logging import Logger

    return Logger(debug=False, dry_run=False, stream=sys.stderr)


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path):
    from nixorcist.storage.paths import Paths

    return Paths(Path(tmp_path)).ensure()


def _package(requested):
    from nixorcist.core.models import ResolvedPackage

    return ResolvedPackage(requested=requested, attribute=requested)


@pytest.fixture
def package():
    return _package


# ---------------------------------------------------------------------------
# Resolver (identity mode so tests never invoke Nix)
# ---------------------------------------------------------------------------


@pytest.fixture
def resolver():
    from nixorcist.core.resolver import PackageResolver, ResolutionMode

    return PackageResolver(mode=ResolutionMode.IDENTITY)


# ---------------------------------------------------------------------------
# Group repository + manager
# ---------------------------------------------------------------------------


@pytest.fixture
def repository(paths, logger):
    from nixorcist.groups.repository import GroupRepository

    return GroupRepository(paths=paths, logger=logger)


@pytest.fixture
def manager(repository, resolver, logger):
    from nixorcist.groups.manager import GroupManager

    return GroupManager(repository=repository, resolver=resolver, logger=logger)


@pytest.fixture
def mgr(manager):
    """Alias so tests read naturally (mgr.*)."""
    return manager
