"""Archived Run versions are listed in version order, not name order."""

from pathlib import Path

from manyselves.runtime.generic_binding import _version_order


def test_versions_sort_by_number_from_the_directory_or_its_version_file() -> None:
    paths = [Path(f"versions/v{n}/version.json") for n in (10, 2, 1, 5)]

    assert [_version_order(p) for p in sorted(paths, key=_version_order)] == [1, 2, 5, 10]
    assert _version_order(Path("versions/v3")) == 3
    assert _version_order(Path("versions/notes")) == 0
