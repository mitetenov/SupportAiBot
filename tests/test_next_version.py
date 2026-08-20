"""Tests for the CI helper that picks the next version to publish.

The version is assigned by looking at what is already on Docker Hub, so the
part worth testing is the choice itself, kept free of the HTTP call.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "next_version", Path(__file__).parent.parent / ".github" / "scripts" / "next_version.py"
)
assert _spec and _spec.loader
next_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(next_version)


class TestNextVersion:
    def test_first_release_of_a_major_line_starts_at_zero(self) -> None:
        assert next_version.next_version(2, []) == "2.0.0"

    def test_takes_the_minor_after_the_highest_published_one(self) -> None:
        assert next_version.next_version(2, ["2.0.0", "2.1.0"]) == "2.2.0"

    def test_a_published_patch_still_only_moves_the_minor(self) -> None:
        assert next_version.next_version(2, ["2.0.0", "2.0.1"]) == "2.1.0"

    def test_compares_numerically_rather_than_as_text(self) -> None:
        """The bug this exists to prevent: "2.9.0" > "2.10.0" as strings."""
        assert next_version.next_version(2, ["2.9.0", "2.10.0"]) == "2.11.0"

    def test_other_major_lines_do_not_count(self) -> None:
        assert next_version.next_version(2, ["1.7.0", "3.4.0"]) == "2.0.0"

    def test_ignores_tags_that_are_not_versions(self) -> None:
        assert next_version.next_version(2, ["latest", "abc123", "2.1.0", ""]) == "2.2.0"

    def test_ignores_prereleases_and_other_suffixes(self) -> None:
        assert next_version.next_version(2, ["2.4.0-rc1", "2.1.0"]) == "2.2.0"

    def test_a_bare_major_line_bump_resets_the_minor(self) -> None:
        assert next_version.next_version(3, ["2.9.0", "2.10.0"]) == "3.0.0"


class TestMajorFromPyproject:
    def test_reads_the_major_component(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\nversion = "2.0.0"\n', encoding="utf-8")
        assert next_version.major_from_pyproject(pyproject) == 2

    def test_rejects_a_version_it_cannot_read(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\nversion = "oops"\n', encoding="utf-8")
        with pytest.raises(ValueError):
            next_version.major_from_pyproject(pyproject)
