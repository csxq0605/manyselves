from pathlib import Path

import pytest

from deploy.backup.archive import accounts, backup, restore


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


def test_accounts_lists_each_username_and_password_variable(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "version: 1\naccounts:\n"
        "  - id: a\n    username: alice\n    passwordEnv: PW_A  # note\n"
        "  - id: b\n    username: 'bob'\n    passwordEnv: PW_B\n",
        encoding="utf-8",
    )

    assert accounts(path) == [("alice", "PW_A"), ("bob", "PW_B")]


def test_manifest_matches_the_archived_bytes_of_a_file_that_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deploy.backup.archive as archive_module

    source = _fixture(tmp_path / "source")
    brief = source / "default" / "Inputs" / "brief.md"
    listed = archive_module._files

    def files_then_write(root: Path) -> list[Path]:
        found = listed(root)
        # A write between listing and archiving, as an active account would do.
        brief.write_text("energy baseline, edited", encoding="utf-8")
        return found

    monkeypatch.setattr(archive_module, "_files", files_then_write)
    archive = backup(source, tmp_path / "backups")
    monkeypatch.setattr(archive_module, "_files", listed)
    target = tmp_path / "restored"
    target.mkdir()

    restore(archive, target, force=False)
