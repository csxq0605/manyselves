from pathlib import Path

import pytest

from deploy.backup.archive import backup, restore


def _fixture(root: Path) -> Path:
    (root / "default" / "Inputs").mkdir(parents=True)
    (root / "default" / "Inputs" / "brief.md").write_text("energy baseline", encoding="utf-8")
    (root / "manyselves.config.yaml").write_text("providers: {}\n", encoding="utf-8")
    return root


def _manifest(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_backup_restore_round_trip(tmp_path: Path) -> None:
    source = _fixture(tmp_path / "source")
    archive = backup(source, tmp_path / "backups")
    target = tmp_path / "restored"
    target.mkdir()
    restore(archive, target, force=False)
    assert _manifest(target) == _manifest(source)


def test_restore_refuses_nonempty_target_without_explicit_force(tmp_path: Path) -> None:
    source = _fixture(tmp_path / "source")
    archive = backup(source, tmp_path / "backups")
    target = tmp_path / "target"
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        restore(archive, target, force=False)
    assert keep.read_text("utf-8") == "keep"


def test_forced_restore_preserves_previous_target(tmp_path: Path) -> None:
    archive = backup(_fixture(tmp_path / "source"), tmp_path / "backups")
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    previous = restore(archive, target, force=True)
    assert previous is not None
    assert (previous / "keep.txt").read_text("utf-8") == "keep"
    assert (target / "default" / "Inputs" / "brief.md").is_file()
