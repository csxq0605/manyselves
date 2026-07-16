import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from manyselves.core.tools.manifest_tool import ManifestManager, ManifestTool


def _run(coro):
    """Run an async coroutine from a sync test."""
    return asyncio.run(coro)


def _set_mtime(path: Path, epoch: float) -> None:
    """Set a file's mtime deterministically to avoid 1s-resolution races."""
    os.utime(path, (epoch, epoch))


def test_manifest_clear_resets_files_and_notes(tmp_path: Path):
    manager = ManifestManager(tmp_path)
    agent = "main"

    async def _run() -> None:
        await manager.touch_files(agent, ["Inputs/Processed/a.csv"])
        manifest = await manager.load(agent)
        manifest["notes"] = "old notes"
        await manager.save(agent, manifest)

        await manager.clear(agent)
        cleared = await manager.load(agent)

        assert cleared["agent_type"] == agent
        assert cleared["files"] == []
        assert cleared["notes"] == ""

    asyncio.run(_run())


class TestManifestFilesystemSync:
    """load() must reflect external add/modify/delete before returning."""

    def test_new_files_appear_with_timestamp(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")

        manifest = _run(manager.load("main"))

        assert [f["path"] for f in manifest["files"]] == ["Inputs/a.txt"]
        assert manifest["files"][0]["file_updated_at"]  # populated from mtime

    def test_external_deletion_removes_file(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        theory = tmp_path / "Inputs"
        theory.mkdir()
        (theory / "a.txt").write_text("A")
        (theory / "b.txt").write_text("B")
        _run(manager.load("main"))

        (theory / "b.txt").unlink()  # external deletion
        manifest = _run(manager.load("main"))

        assert [f["path"] for f in manifest["files"]] == ["Inputs/a.txt"]

    def test_external_modification_updates_file_timestamp(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        f = tmp_path / "Inputs" / "a.txt"
        f.parent.mkdir(parents=True)
        f.write_text("v1")
        _set_mtime(f, 1000)  # old mtime

        ts_before = _run(manager.load("main"))["files"][0]["file_updated_at"]

        f.write_text("v2")  # external modification
        _set_mtime(f, 2000)  # advance mtime well past second resolution
        ts_after = _run(manager.load("main"))["files"][0]["file_updated_at"]

        assert ts_before != ts_after
        assert ts_after == datetime.fromtimestamp(2000, tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    def test_descriptions_survive_filesystem_sync(self, tmp_path: Path):
        """Re-syncing the filesystem must not wipe agent-written descriptions."""
        manager = ManifestManager(tmp_path)
        f = tmp_path / "Inputs" / "a.txt"
        f.parent.mkdir(parents=True)
        f.write_text("A")
        tool = ManifestTool(manager, "main")
        _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "", "description_new": "keep me"}],
            )
        )

        _set_mtime(f, 5000)  # force a re-sync on next load
        manifest = _run(manager.load("main"))
        entry = {x["path"]: x for x in manifest["files"]}["Inputs/a.txt"]

        assert entry["description"] == "keep me"
        assert entry["description_updated_at"]  # preserved, not reset


class TestManifestTimestamps:
    """The update action must maintain every timestamp field."""

    def test_update_sets_all_timestamps_and_persists(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")

        result = _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "", "description_new": "D"}],
                notes_patch="+N\n",
            )
        )
        manifest = result["manifest"]
        entry = {f["path"]: f for f in manifest["files"]}["Inputs/a.txt"]

        assert entry["file_updated_at"]
        assert entry["description"] == "D" and entry["description_updated_at"]
        assert manifest["notes"] == "N\n" and manifest["notes_updated_at"]
        assert manifest["updated_at"]

        # Persisted to disk with the same top-level timestamp.
        disk = json.loads((manager.base_dir / "main.json").read_text(encoding="utf-8"))
        assert disk["updated_at"] == manifest["updated_at"]

    def test_update_without_description_keeps_old_timestamp(self, tmp_path: Path):
        """Re-submitting a record without description_new must not touch description_updated_at."""
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")

        first = _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "", "description_new": "D"}],
            )
        )
        first_ts = {f["path"]: f for f in first["manifest"]["files"]}["Inputs/a.txt"][
            "description_updated_at"
        ]

        # Later update references the file but omits description_new.
        second = _run(tool(action="update", files=[{"path": "Inputs/a.txt"}]))
        second_ts = {f["path"]: f for f in second["manifest"]["files"]}["Inputs/a.txt"][
            "description_updated_at"
        ]

        assert first_ts == second_ts


class TestManifestCrossAgentAccess:
    def test_read_other_agent_manifest(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "x.csv").write_text("h")
        tool = ManifestTool(manager, "main")

        manifest = _run(tool(action="read", agent="main"))

        assert any(f["path"] == "Inputs/x.csv" for f in manifest["files"])

    def test_cannot_update_unknown_agent_manifest(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        tool = ManifestTool(manager, "main")

        result = _run(tool(action="update", agent="legacy-agent", notes_patch="+x\n"))

        assert result["status"] == "error"


class TestManifestPathLookupOnly:
    """Req #2: the file list is driven by the local filesystem; path is lookup-only."""

    def test_unknown_path_is_skipped_and_reported(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")

        result = _run(
            tool(
                action="update",
                files=[
                    {"path": "Inputs/a.txt", "description_old": "", "description_new": "real"},
                    {"path": "Inputs/ghost.txt", "description_old": "", "description_new": "fake"},
                ],
            )
        )

        assert result["status"] == "ok"
        assert result["not_found"] == ["Inputs/ghost.txt"]
        paths = {f["path"] for f in result["manifest"]["files"]}
        assert paths == {"Inputs/a.txt"}  # ghost was never registered

    def test_unknown_path_not_persisted(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")

        _run(
            tool(
                action="update",
                files=[{"path": "Inputs/ghost.txt", "description_old": "", "description_new": "x"}],
            )
        )

        # A fresh load must not contain the ghost entry.
        manifest = _run(manager.load("main"))
        assert all("ghost" not in f["path"] for f in manifest["files"])


class TestManifestDiffEdit:
    """Req #4: descriptions and notes change only via diffs (explicit old -> new / line patch)."""

    def test_description_change_requires_matching_old(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")
        _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "", "description_new": "v1"}],
            )
        )

        result = _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "v1", "description_new": "v2"}],
            )
        )

        assert result["description_changes"] == [{"path": "Inputs/a.txt", "old": "v1", "new": "v2"}]

    def test_description_old_mismatch_is_rejected(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")
        _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "", "description_new": "v1"}],
            )
        )

        # Wrong description_old -> rejected, current description unchanged.
        result = _run(
            tool(
                action="update",
                files=[{"path": "Inputs/a.txt", "description_old": "WRONG", "description_new": "v2"}],
            )
        )

        assert result["description_mismatches"] == [
            {"path": "Inputs/a.txt", "expected": "WRONG", "actual": "v1"}
        ]
        assert result["description_changes"] == []
        entry = {f["path"]: f for f in result["manifest"]["files"]}["Inputs/a.txt"]
        assert entry["description"] == "v1"

    def test_notes_line_patch_replaces_a_line(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")
        _run(tool(action="update", notes_patch="+alpha\n+beta\n+gamma\n"))

        result = _run(tool(action="update", notes_patch="-beta\n+BETA\n"))

        assert result["status"] == "ok"
        assert result["manifest"]["notes"] == "alpha\nBETA\ngamma\n"
        assert result["notes_diff"]

    def test_notes_patch_mismatch_is_error_and_no_change(self, tmp_path: Path):
        manager = ManifestManager(tmp_path)
        (tmp_path / "Inputs").mkdir()
        (tmp_path / "Inputs" / "a.txt").write_text("A")
        tool = ManifestTool(manager, "main")
        _run(tool(action="update", notes_patch="+alpha\n+beta\n+gamma\n"))

        result = _run(tool(action="update", notes_patch="-zzz\n+QQQ\n"))

        assert result["status"] == "error"
        # Original notes survive untouched.
        assert _run(manager.load("main"))["notes"] == "alpha\nbeta\ngamma\n"
