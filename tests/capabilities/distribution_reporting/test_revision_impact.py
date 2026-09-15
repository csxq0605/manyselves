"""Impact analysis maps frozen input changes to subsection revision targets."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.impact import (
    RevisionImpactDecision,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.revision_impact import (
    apply_revision_impact,
    build_revision_impact_analysis,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY


def _state(tmp_path: Path) -> dict:
    modules = {
        key: {
            "module_id": key,
            "submodule_narratives": {sid: f"原有 {sid} 正文，引用 E-0001。" for sid in spec.submodules},
            "claims": [],
            "source_ids": ["E-0001"],
            "unresolved_questions": [],
            "revision": 1,
        }
        for key, spec in REPORT_TAXONOMY.items()
    }
    modules["2.3"]["submodule_narratives"]["2.3.1"] = "定值说明，依据 E-0317。"
    return {
        "run_id": "revision-1",
        "baseline_run_id": "baseline-1",
        "request": {
            "operation": "revise_report",
            "instruction": "按新巡检资料修订报告",
            "baseline_run_id": "baseline-1",
            "requested_changes": {},
            "impact_mode": "auto",
        },
        "module_submissions": modules,
        "evidence_items": [
            {
                "id": "E-0332",
                "subject": "巡检闭环统计",
                "fact": "2 completed 1 pending",
                "source": {"kind": "file", "path": "Inputs/REV-DEMO-巡检记录.txt", "locator": "L1"},
                "module_id": "2.3",
                "submodule_id": "2.3.1",
                "photo_refs": [],
            }
        ],
        "revision_input_changes": {
            "files": {"REV-DEMO-巡检记录.txt": {"kind": "modified", "path": "Inputs/REV-DEMO-巡检记录.txt"}},
            "superseded_evidence_ids": ["E-0317"],
            "current_evidence": [
                {
                    "evidence_id": "E-0332",
                    "title": "巡检闭环统计",
                    "locator": "Inputs/REV-DEMO-巡检记录.txt",
                    "content": "{}",
                }
            ],
        },
    }


def test_build_impact_analysis_binds_new_and_superseded_evidence(tmp_path):
    store = ReportingStore(tmp_path)
    analysis = build_revision_impact_analysis(_state(tmp_path), store=store)
    assert analysis.requested_changes
    assert "2.3.1" in analysis.requested_changes
    item_231 = next(item for item in analysis.impacts if item.subsection_id == "2.3.1")
    assert "E-0332" in item_231.evidence_ids
    assert "E-0317" in item_231.evidence_ids
    assert "evidence_added" in item_231.change_kinds
    assert "evidence_superseded" in item_231.change_kinds
    assert (tmp_path / analysis.impact_ref).is_file()
    saved = (tmp_path / analysis.impact_ref).read_text(encoding="utf-8")
    assert "2.3.1" in saved


def test_apply_impact_accept_selected_and_abort(tmp_path):
    store = ReportingStore(tmp_path)
    analysis = build_revision_impact_analysis(_state(tmp_path), store=store)
    state = _state(tmp_path)
    applied = apply_revision_impact(
        {
            "state": state,
            "analysis": analysis.model_dump(mode="json"),
            "decision": RevisionImpactDecision(
                action="accept_selected",
                selected_subsection_ids=["2.3.1"],
            ).model_dump(mode="json"),
        }
    )
    assert set(applied["revision_targets"]) == {"2.3.1"}
    assert applied["request"]["target_modules"] == ["2.3"]
    with pytest.raises(ValueError, match="aborted"):
        apply_revision_impact(
            {
                "state": state,
                "analysis": analysis.model_dump(mode="json"),
                "decision": {"kind": "revision_impact_decision", "action": "abort"},
            }
        )


def test_build_impact_analysis_parses_added_removed_lists_and_module_hint():
    from manyselves.capabilities.distribution_reporting.runtime.revision_impact import (
        build_revision_impact_analysis,
    )
    from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

    class _Store:
        def __init__(self):
            self.written = {}

        def write_json(self, ref, payload):
            self.written[ref] = payload
            return ref

    state = {
        "run_id": "r2",
        "baseline_run_id": "b2",
        "request": {
            "instruction": "根据新增的 2.3 保护定值核对资料修订报告",
            "baseline_run_id": "b2",
            "requested_changes": {},
            "impact_mode": "auto",
        },
        "module_submissions": {},
        "evidence_items": [],
        "revision_input_changes": {
            "files": {
                "added": ["Inputs/REV-DEMO-影响分析补充.txt"],
                "modified": [],
                "removed": [],
            },
            "superseded_evidence_ids": [],
            "current_evidence": [],
        },
    }
    analysis = build_revision_impact_analysis(state, store=_Store())
    assert analysis.changes[0].kind == "file_added"
    assert analysis.changes[0].locator.endswith("影响分析补充.txt")
    assert analysis.requested_changes
    assert set(analysis.requested_changes) <= set(
        __import__(
            "manyselves.capabilities.distribution_reporting.domain.taxonomy",
            fromlist=["REPORT_TAXONOMY"],
        ).REPORT_TAXONOMY["2.3"].submodules
    )


def test_report_request_allows_impact_mode_without_explicit_changes():
    request = ReportRequest(
        operation="revise_report",
        instruction="按新资料修订",
        baseline_run_id="baseline-1",
        impact_mode="auto",
    )
    assert request.impact_mode == "auto"
    assert request.target_modules == ["2.1", "2.2", "2.3", "2.4", "2.5"]
    with pytest.raises(ValueError):
        ReportRequest(
            operation="revise_report",
            instruction="改",
            baseline_run_id="baseline-1",
            impact_mode="none",
        )
