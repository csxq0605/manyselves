import json
from pathlib import Path

import pytest

from autoreport.core.reporting.source_ledger import SourceLedger


def test_source_ids_are_stable_per_kind_and_persisted(tmp_path: Path):
    ledger = SourceLedger(tmp_path, "run-1")
    local = ledger.register_local(
        "标准摘录", "Knowledge/任意目录/a.md", "正文"
    )
    same_local = SourceLedger(tmp_path, "run-1").register_local(
        "标准摘录", "Knowledge/任意目录/a.md", "正文"
    )
    web = SourceLedger(tmp_path, "run-1").register_web(
        "机构说明", "https://example.org/guide", "网页正文", publisher="机构"
    )

    assert local.id == same_local.id == "R-001"
    assert web.id == "W-001"
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/ledgers/sources.json").read_text(encoding="utf-8")
    )
    assert [record["id"] for record in persisted] == ["R-001", "W-001"]


def test_project_evidence_keeps_e_id_and_rejects_conflict(tmp_path: Path):
    ledger = SourceLedger(tmp_path, "run-1")
    source = ledger.register_project(
        "E-014", "红外检测记录", "Inputs/红外.xlsx#Sheet1!B2", "连接点 86℃"
    )

    assert source.id == "E-014"
    with pytest.raises(ValueError, match="conflicting"):
        ledger.register_project(
            "E-014", "红外检测记录", "Inputs/红外.xlsx#Sheet1!B3", "连接点 90℃"
        )


def test_source_ledger_rejects_run_path_escape(tmp_path: Path):
    with pytest.raises(ValueError, match="run_id"):
        SourceLedger(tmp_path, "../outside")


def test_source_ledger_accepts_any_knowledge_path_and_rejects_other_roots(tmp_path: Path):
    ledger = SourceLedger(tmp_path, "run-1")

    source = ledger.register_local("参考", "Knowledge/供应商/手册.md", "正文")

    assert source.locator == "Knowledge/供应商/手册.md"
    with pytest.raises(ValueError, match="Knowledge"):
        ledger.register_local("现场", "Inputs/检测.md", "正文")
