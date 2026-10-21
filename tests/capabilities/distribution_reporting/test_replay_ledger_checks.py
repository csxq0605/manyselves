"""The offline replay re-runs the program steps of a finished run without models."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from tests.capabilities.distribution_reporting.test_ledger_report import (
    RUN_ID,
    run_ledger_report,
)

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/maintenance/replay_ledger_checks.py"


def _replay_module():
    spec = importlib.util.spec_from_file_location("replay_ledger_checks", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_replay_reruns_checks_and_delivery_on_a_copy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    binding, accepted, _loops = await run_ledger_report(project)
    assert binding.get_run(accepted["run_id"])["run"]["status"] == "completed"
    # Facts recorded by an older code version lack fields added since.
    run_dir = project / "Work/runs" / RUN_ID
    ledger = json.loads((run_dir / "ledgers/sources.json").read_text(encoding="utf-8"))
    for record in ledger:
        if record["kind"] == "project_evidence":
            content = json.loads((run_dir / f"sources/{record['id']}.txt").read_text("utf-8"))
            content.pop("summary_entry")
            old = json.dumps(content, ensure_ascii=False)
            (run_dir / f"sources/{record['id']}.txt").write_text(old, encoding="utf-8")
            record["content_sha256"] = hashlib.sha256(old.encode("utf-8")).hexdigest()
    (run_dir / "ledgers/sources.json").write_text(json.dumps(ledger), encoding="utf-8")
    before = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}

    summary = _replay_module().replay(project, accepted["run_id"], tmp_path / "replay", None)

    # The source project is untouched; everything is written to the copy.
    assert {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()} == before
    assert summary["ledger_checks"]["2.4"]["old_blocking"] == 0
    assert set(summary["edit_checks"]) >= {"2.1", "chief"}
    delivery = summary["delivery"]
    assert delivery["render_verification"]["protected_prose_verified"] is True
    assert delivery["markdown"]["补充补充"] == 0
    assert delivery["markdown"]["各表行数"]
    assert delivery["markdown_tables_without_equal_word_table"] == []
    replayed = (tmp_path / "replay/Outputs/Reports/配电安全专家咨询报告.md").read_text(
        encoding="utf-8"
    )
    assert "设备1检查情况良好，无需处理。" in replayed
    assert (tmp_path / "replay/replay-summary.json").is_file()


def test_a_copied_project_gets_its_verified_inputs_rebound(tmp_path: Path) -> None:
    import shutil

    from manyselves.runtime.artifacts.content_store import ContentAddressedStore

    project = tmp_path / "project"
    (project / "Inputs").mkdir(parents=True)
    source = project / "Inputs/table.xlsx"
    source.write_bytes(b"table bytes")
    store = ContentAddressedStore(project)
    handle = store.issue_trusted_handle(store.ingest_file(source), lineage_id="run-1")
    copy = tmp_path / "copy"
    shutil.copytree(project, copy, symlinks=True)
    copied = ContentAddressedStore(copy)
    with pytest.raises(ValueError, match="changed since"):
        copied.resolve_trusted_handle(copied.load_trusted_handle(handle.manifest_ref))

    assert _replay_module()._rebind_trusted_blobs(copy) == 1

    resolved = copied.resolve_trusted_handle(copied.load_trusted_handle(handle.manifest_ref))
    assert resolved.is_relative_to(copy.resolve())
    # The source project's record is untouched and still valid.
    assert store.resolve_trusted_handle(store.load_trusted_handle(handle.manifest_ref))
