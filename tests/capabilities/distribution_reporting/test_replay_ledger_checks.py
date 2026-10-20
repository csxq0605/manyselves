"""The offline replay re-runs the program steps of a finished run without models."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.capabilities.distribution_reporting.test_ledger_report import run_ledger_report

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
