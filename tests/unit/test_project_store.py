import json
import shutil
from pathlib import Path

import pytest

from pds_report.infrastructure.project_store import ProjectStore, ProjectStoreError

TEST_ROOT = Path(".test-projects/project-store")


def fresh_root(name: str) -> Path:
    root = (TEST_ROOT / name).resolve()
    if root.exists():
        shutil.rmtree(root)
    return root


def test_create_project_uses_only_project_root() -> None:
    project_root = fresh_root("layout")

    ProjectStore(project_root).create()

    assert (project_root / "Inputs").is_dir()
    assert (project_root / "Knowledge").is_dir()
    assert (project_root / "Work" / "runs").is_dir()
    assert (project_root / "Outputs" / "Modules").is_dir()
    assert (project_root / "Outputs" / "Reviews").is_dir()
    assert (project_root / "Outputs" / "Reports").is_dir()


def test_json_write_is_atomic_and_leaves_no_pending_file() -> None:
    project_root = fresh_root("json")
    store = ProjectStore(project_root)
    store.create()

    output = store.write_json(Path("Work/coverage.json"), {"status": "ready"})

    assert output == project_root / "Work" / "coverage.json"
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "ready"}
    assert not (output.parent / ".coverage.json.pending").exists()


def test_jsonl_write_preserves_unicode_rows() -> None:
    project_root = fresh_root("jsonl")
    store = ProjectStore(project_root)
    store.create()

    output = store.write_jsonl(
        Path("Work/evidence.jsonl"),
        [{"id": "ev-1", "fact": "主柜温度为 80°C"}],
    )

    assert json.loads(output.read_text(encoding="utf-8").strip()) == {
        "id": "ev-1",
        "fact": "主柜温度为 80°C",
    }


def test_store_rejects_path_escape() -> None:
    store = ProjectStore(fresh_root("escape"))
    store.create()

    with pytest.raises(ProjectStoreError, match="inside the project"):
        store.write_text(Path("../outside.txt"), "no")


def test_save_run_writes_snapshot_below_work_runs() -> None:
    project_root = fresh_root("run")
    store = ProjectStore(project_root)
    store.create()

    output = store.save_run("run-123", {"status": "completed"})

    assert output == project_root / "Work" / "runs" / "run-123.json"


def test_new_store_instance_can_restore_saved_run() -> None:
    project_root = fresh_root("restore")
    first = ProjectStore(project_root)
    first.create()
    first.save_run("run-123", {"status": "failed", "tasks": [{"id": "task-1"}]})

    restored = ProjectStore(project_root).load_run("run-123")

    assert restored == {"status": "failed", "tasks": [{"id": "task-1"}]}
