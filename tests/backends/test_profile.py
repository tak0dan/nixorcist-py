"""Backend tests for NixProfile: list_entries dict-shaped JSON (Nix 2.34),
store-path removal, and attribute/bucket matching.

These run offline — no real ``nix profile`` calls are made.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from nixorcist.backends.profile import NixProfile
from nixorcist.core.models import ResolvedPackage
from nixorcist.nix.command import CommandResult


# ---------------------------------------------------------------------------
# Stub NixCommand
# ---------------------------------------------------------------------------

@dataclass
class FakeNixCommand:
    """Stubs ``NixCommand`` — records calls, returns pre-configured results."""

    results: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    history: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return True

    def run(self, args: list[str], *, check: bool = True, timeout: int = 900) -> CommandResult:
        key = tuple(args)
        self.history.append(key)
        result = self.results.get(key)
        if result is None:
            # Default: return success (for calls we only need to record)
            return CommandResult(key, 0, "", "")
        if check and not result.ok:
            raise AssertionError(f"FakeNixCommand: non-zero return for {key}: {result.returncode}")
        return result


# ---------------------------------------------------------------------------
# Helpers to build the key with or without --profile
# ---------------------------------------------------------------------------

def _list_key(profile_path=None):
    args = ["profile", "list"]
    if profile_path:
        args.extend(["--profile", profile_path])
    args.append("--json")
    return tuple(args)


def _make_list_result(elements, profile_path=None):
    payload = json.dumps({"version": 3, "elements": elements})
    return {_list_key(profile_path): CommandResult(("nix",), 0, payload, "")}


def _remove_key(*store_paths, profile_path=None):
    args = ["profile", "remove"]
    if profile_path:
        args.extend(["--profile", profile_path])
    args.extend(store_paths)
    return tuple(args)


# ---------------------------------------------------------------------------
# Canned element fixtures
# ---------------------------------------------------------------------------

def _dict_elements():
    """Nix 2.34: elements is a dict keyed by index."""
    return {
        "0": {
            "attrPath": "legacyPackages.x86_64-linux.cowsay",
            "name": None,
            "originalUrl": "flake:nixpkgs",
            "storePaths": ["/nix/store/cp8sx00y1p8c6gjkzn6zwjw44i18v9zr-cowsay-3.8.4"],
        },
        "1": {
            "attrPath": "legacyPackages.x86_64-linux.hello",
            "name": None,
            "originalUrl": "flake:nixpkgs",
            "storePaths": ["/nix/store/xl1h9i29pgq2q5cszjhm5wpfxfbbqwyi-hello-2.12.3"],
        },
    }


def _list_elements():
    """Classic Nix: elements is a list."""
    return [
        {
            "attrPath": "legacyPackages.x86_64-linux.cowsay",
            "name": None,
            "originalUrl": "flake:nixpkgs",
            "storePaths": ["/nix/store/cp8sx00y1p8c6gjkzn6zwjw44i18v9zr-cowsay-3.8.4"],
        },
        {
            "attrPath": "legacyPackages.x86_64-linux.hello",
            "name": None,
            "originalUrl": "flake:nixpkgs",
            "storePaths": ["/nix/store/xl1h9i29pgq2q5cszjhm5wpfxfbbqwyi-hello-2.12.3"],
        },
    ]


def _nested_attrpath_elements():
    """attrPath as a nested list (some Nix versions)."""
    return {
        "0": {
            "attrPath": ["legacyPackages", "x86_64-linux", "cowsay"],
            "name": "my-cowsay",
            "originalUrl": "flake:nixpkgs",
            "storePaths": ["/nix/store/aaa-cowsay-3.8.4"],
        },
    }


def _empty_elements():
    return {}


# ---------------------------------------------------------------------------
# list_entries tests
# ---------------------------------------------------------------------------

class TestListEntries:
    def _build(self, elements, profile_path=None):
        cmd = FakeNixCommand(results=_make_list_result(elements, profile_path))
        backend = NixProfile(command=cmd, profile_path=profile_path)
        return backend, cmd

    def test_dict_elements(self):
        backend, _ = self._build(_dict_elements())
        entries = backend.list_entries()
        assert len(entries) == 2
        attrs = [e.attribute for e in entries]
        assert "legacyPackages.x86_64-linux.cowsay" in attrs
        assert "legacyPackages.x86_64-linux.hello" in attrs

    def test_list_elements(self):
        backend, _ = self._build(_list_elements())
        entries = backend.list_entries()
        assert len(entries) == 2
        names = [e.name for e in entries]
        assert all(n == "" for n in names)

    def test_dict_elements_empty(self):
        backend, _ = self._build(_empty_elements())
        entries = backend.list_entries()
        assert entries == []

    def test_nested_attrpath_joined(self):
        backend, _ = self._build(_nested_attrpath_elements())
        entries = backend.list_entries()
        assert entries[0].attribute == "legacyPackages.x86_64-linux.cowsay"
        assert entries[0].name == "my-cowsay"

    def test_store_paths_first_element_used(self):
        backend, _ = self._build(_dict_elements())
        entries = backend.list_entries()
        by_attr = {e.attribute: e for e in entries}
        assert by_attr["legacyPackages.x86_64-linux.cowsay"].store_path == "/nix/store/cp8sx00y1p8c6gjkzn6zwjw44i18v9zr-cowsay-3.8.4"


# ---------------------------------------------------------------------------
# remove tests
# ---------------------------------------------------------------------------

class TestRemove:
    def _build(self, elements, profile_path=None):
        cmd = FakeNixCommand(results=_make_list_result(elements, profile_path))
        backend = NixProfile(command=cmd, profile_path=profile_path)
        return backend, cmd

    def test_remove_matches_by_bucket(self):
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        pkgs = [ResolvedPackage(requested="cowsay", attribute="cowsay")]
        backend.remove(pkgs)
        key = _remove_key("/nix/store/cp8sx00y1p8c6gjkzn6zwjw44i18v9zr-cowsay-3.8.4", profile_path="/tmp/test-profile")
        assert key in cmd.history

    def test_remove_matches_by_full_attribute(self):
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        pkgs = [ResolvedPackage(requested="hello", attribute="legacyPackages.x86_64-linux.hello")]
        backend.remove(pkgs)
        key = _remove_key("/nix/store/xl1h9i29pgq2q5cszjhm5wpfxfbbqwyi-hello-2.12.3", profile_path="/tmp/test-profile")
        assert key in cmd.history

    def test_remove_matches_by_bucket_when_attr_short(self):
        """removing attribute 'hello' matches entry 'legacyPackages.x86_64-linux.hello'."""
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        pkgs = [ResolvedPackage(requested="hello", attribute="hello")]
        backend.remove(pkgs)
        key = _remove_key("/nix/store/xl1h9i29pgq2q5cszjhm5wpfxfbbqwyi-hello-2.12.3", profile_path="/tmp/test-profile")
        assert key in cmd.history

    def test_remove_no_match_no_profile_call(self):
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        pkgs = [ResolvedPackage(requested="nonexistent", attribute="nonexistent")]
        backend.remove(pkgs)
        assert not any(k[0:2] == ("profile", "remove") for k in cmd.history)

    def test_remove_empty_packages_noop(self):
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        backend.remove([])
        assert not any(k[0:2] == ("profile", "remove") for k in cmd.history)

    def test_remove_uses_store_path_not_name(self):
        """Removal uses store_path, NOT the element name (which can be '' from None)."""
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        pkgs = [ResolvedPackage(requested="cowsay", attribute="legacyPackages.x86_64-linux.cowsay")]
        backend.remove(pkgs)
        remove_calls = [k for k in cmd.history if k[0:2] == ("profile", "remove")]
        assert len(remove_calls) == 1
        args = list(remove_calls[0])
        assert "/nix/store/cp8sx00y1p8c6gjkzn6zwjw44i18v9zr-cowsay-3.8.4" in args
        assert "None" not in args

    def test_remove_deduplicates_store_paths(self):
        """Multiple entries with same store_path only remove it once."""
        dup_elements = {
            "0": {"attrPath": "a", "name": None, "storePaths": ["/nix/store/dup-1"]},
            "1": {"attrPath": "b", "name": None, "storePaths": ["/nix/store/dup-1"]},
        }
        backend, cmd = self._build(dup_elements, "/tmp/test-profile")
        backend.remove([
            ResolvedPackage(requested="a", attribute="a"),
            ResolvedPackage(requested="b", attribute="b"),
        ])
        remove_calls = [k for k in cmd.history if k[0:2] == ("profile", "remove")]
        assert len(remove_calls) == 1
        assert list(remove_calls[0]).count("/nix/store/dup-1") == 1


# ---------------------------------------------------------------------------
# remove_all tests
# ---------------------------------------------------------------------------

class TestRemoveAll:
    def _build(self, elements, profile_path=None):
        cmd = FakeNixCommand(results=_make_list_result(elements, profile_path))
        backend = NixProfile(command=cmd, profile_path=profile_path)
        return backend, cmd

    def test_remove_all_uses_store_paths(self):
        backend, cmd = self._build(_dict_elements(), "/tmp/test-profile")
        backend.remove_all()
        expected = _remove_key(
            "/nix/store/cp8sx00y1p8c6gjkzn6zwjw44i18v9zr-cowsay-3.8.4",
            "/nix/store/xl1h9i29pgq2q5cszjhm5wpfxfbbqwyi-hello-2.12.3",
            profile_path="/tmp/test-profile",
        )
        assert expected in cmd.history

    def test_remove_all_empty_profile_noop(self):
        backend, cmd = self._build(_empty_elements(), "/tmp/test-profile")
        backend.remove_all()
        assert not any(k[0:2] == ("profile", "remove") for k in cmd.history)

    def test_remove_all_no_nix_remove_when_empty(self):
        backend, cmd = self._build(_empty_elements())
        backend.remove_all()
        assert not any(k[0:2] == ("profile", "remove") for k in cmd.history)
