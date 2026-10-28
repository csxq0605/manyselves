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
    listed = archive_module._entries

    def files_then_write(root: Path):
        found = listed(root)
        # A write between listing and archiving, as an active account would do.
        brief.write_text("energy baseline, edited", encoding="utf-8")
        return found

    monkeypatch.setattr(archive_module, "_entries", files_then_write)
    archive = backup(source, tmp_path / "backups")
    monkeypatch.setattr(archive_module, "_entries", listed)
    target = tmp_path / "restored"
    target.mkdir()

    restore(archive, target, force=False)


def test_a_report_input_snapshot_is_backed_up_and_restored(tmp_path: Path) -> None:
    """A normal report run links input views to the content store; backups keep them."""
    import os

    from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
        RunInputSnapshotStore,
    )

    source = _fixture(tmp_path / "source")
    project = source / "default"
    (project / "Inputs" / "assessment.xlsx").write_bytes(b"workbook bytes")
    RunInputSnapshotStore(project).freeze("run-1")
    links = [path for path in source.rglob("*") if path.is_symlink()]
    if not links:
        pytest.skip("this filesystem made copies instead of links")

    archive = backup(source, tmp_path / "backups")
    target = tmp_path / "restored"
    restore(archive, target, force=False)

    for link in links:
        restored = target / link.relative_to(source)
        assert restored.is_symlink()
        assert os.readlink(restored) == os.readlink(link)
        assert restored.read_bytes() == link.read_bytes()
    assert _manifest(target) == _manifest(source)


def test_backup_still_refuses_links_that_leave_the_data(tmp_path: Path) -> None:
    source = _fixture(tmp_path / "source")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    import os

    (source / "default" / "escape").symlink_to(os.path.relpath(outside, source / "default"))
    with pytest.raises(ValueError, match="outside the data"):
        backup(source, tmp_path / "backups")
    (source / "default" / "escape").unlink()
    (source / "default" / "absolute").symlink_to(source / "manyselves.config.yaml")
    with pytest.raises(ValueError, match="absolute"):
        backup(source, tmp_path / "backups")


def test_restore_refuses_a_link_the_manifest_does_not_list(tmp_path: Path) -> None:
    import io
    import json
    import tarfile

    archive = tmp_path / "forged.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        link = tarfile.TarInfo("snapshot/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        bundle.addfile(link)
        manifest = json.dumps({"files": {}, "formatVersion": 2, "links": {}}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        bundle.addfile(info, io.BytesIO(manifest))

    with pytest.raises(ValueError, match="Unsafe archive member"):
        restore(archive, tmp_path / "restored", force=False)


def test_restore_reads_archives_made_before_links_were_kept(tmp_path: Path) -> None:
    import io
    import json
    import tarfile

    archive = tmp_path / "old.tar.gz"
    content = b"energy baseline"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("snapshot/default/brief.md")
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
        import hashlib

        manifest = json.dumps(
            {"files": {"default/brief.md": hashlib.sha256(content).hexdigest()}, "formatVersion": 1}
        ).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        bundle.addfile(info, io.BytesIO(manifest))

    restore(archive, tmp_path / "restored", force=False)
    assert (tmp_path / "restored" / "default" / "brief.md").read_bytes() == content
