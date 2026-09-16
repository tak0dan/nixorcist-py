"""Group manifest repository tests: TOML round-trip, case-insensitive lookup,
schema-version enforcement, and stale-file cleanup."""

from __future__ import annotations

import pytest

from nixorcist.core.models import Backend, GroupState, ResolvedPackage
from nixorcist.groups.manifest import (
    SCHEMA_VERSION,
    GroupManifest,
    ManifestError,
    parse,
    serialize,
)
from nixorcist.groups.repository import GroupRepository, RepositoryError


def rp(requested, attribute=None):
    return ResolvedPackage(requested=requested, attribute=attribute or requested)


class TestSerializeRoundTrip:
    def test_explicit_roundtrip(self):
        m = GroupManifest(
            name="Programming",
            description="tools",
            packages=[rp("python", "python3"), rp("gcc", "gcc13")],
            state=GroupState(active=True, backend=Backend.DECLARATIVE),
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-02T00:00:00+00:00",
        )
        parsed = parse(serialize(m))
        assert parsed.name == "Programming"
        assert parsed.description == "tools"
        assert parsed.state == GroupState(active=True, backend=Backend.DECLARATIVE)
        assert {p.requested for p in parsed.packages} == {"python", "gcc"}
        assert {p.attribute for p in parsed.packages} == {"python3", "gcc13"}

    def test_inactive_none_roundtrip(self):
        m = GroupManifest(name="G", state=GroupState(active=False, backend=Backend.NONE))
        parsed = parse(serialize(m))
        assert parsed.state == GroupState(active=False, backend=Backend.NONE)

    def test_unknown_backend_rejected(self):
        with pytest.raises(ManifestError):
            parse('version = 1\n[group]\nname = "G"\nbackend = "quantum"\n')


class TestSchemaVersion:
    def test_newer_schema_rejected(self):
        with pytest.raises(ManifestError) as exc:
            parse('version = 99\n[group]\nname = "G"\n')
        assert "schema version" in exc.value.diagnostic.message

    def test_missing_schema_rejected(self):
        with pytest.raises(ManifestError):
            parse('[group]\nname = "G"\n')

    def test_current_schema_accepted(self):
        parsed = parse(f'version = {SCHEMA_VERSION}\n[group]\nname = "G"\n')
        assert parsed.schema_version == SCHEMA_VERSION

    def test_invalid_toml_rejected(self):
        with pytest.raises(ManifestError):
            parse("this is ]not[ toml")


class TestRepository:
    def test_save_and_load(self, paths):
        repo = GroupRepository(paths=paths)
        m = GroupManifest(name="Programming", packages=[rp("python")])
        repo.save(m)
        loaded = repo.load("Programming")
        assert loaded is not None
        assert loaded.name == "Programming"
        assert {p.requested for p in loaded.packages} == {"python"}

    def test_load_case_insensitive(self, paths):
        repo = GroupRepository(paths=paths)
        repo.save(GroupManifest(name="Programming"))
        assert repo.load("programming") is not None
        assert repo.load("PROGRAMMING") is not None

    def test_load_missing_returns_none(self, paths):
        repo = GroupRepository(paths=paths)
        assert repo.load("NoSuch") is None

    def test_exists(self, paths):
        repo = GroupRepository(paths=paths)
        repo.save(GroupManifest(name="G"))
        assert repo.exists("g") is True
        assert repo.exists("H") is False

    def test_load_all_sorted(self, paths):
        repo = GroupRepository(paths=paths)
        repo.save(GroupManifest(name="zeta"))
        repo.save(GroupManifest(name="Alpha"))
        names = [m.name for m in repo.load_all()]
        assert names == ["Alpha", "zeta"]

    def test_delete(self, paths):
        repo = GroupRepository(paths=paths)
        repo.save(GroupManifest(name="G"))
        assert repo.delete("g") is True
        assert repo.delete("g") is False

    def test_stale_differently_cased_file_removed(self, paths):
        repo = GroupRepository(paths=paths)
        repo.save(GroupManifest(name="Programming"))
        # Simulate a stale file written with a different casing earlier.
        stale = GroupManifest(name="PROGRAMMING")
        stale_p = paths.groups_dir / "PROGRAMMING.toml"
        stale_p.write_text(serialize(stale), "utf-8")
        # Saving with display-name "Programming" removes the stale file.
        repo.save(GroupManifest(name="Programming"))
        assert not stale_p.exists()

    def test_file_per_group(self, paths):
        repo = GroupRepository(paths=paths)
        repo.save(GroupManifest(name="Server"))
        repo.save(GroupManifest(name="Workstation"))
        tomls = list(paths.groups_dir.glob("*.toml"))
        assert len(tomls) == 2