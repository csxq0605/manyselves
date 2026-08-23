from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceDecisionRequest,
)
from manyselves.core.reporting.decisions import EvidenceDecisionStore


def _decision() -> EvidenceDecisionRequest:
    return EvidenceDecisionRequest(
        decision_id="evidence-decision-001",
        run_id="report-run-001",
        missing_items=["2.4.1.1 缺少设备铭牌证据"],
        affected_modules=["2.4"],
    )


def test_decision_store_persists_and_loads_after_process_restart(tmp_path: Path) -> None:
    first = EvidenceDecisionStore(tmp_path)
    created = first.create(_decision())

    second = EvidenceDecisionStore(tmp_path)
    loaded = second.load(created.decision_id)

    assert loaded == created
    assert loaded.status == "pending"
    assert (tmp_path / "Work/runs/report-run-001/decisions/evidence-decision-001.json").is_file()


@pytest.mark.parametrize("action", ["supplement", "draft", "skip", "stop"])
def test_decision_store_resolves_each_allowed_action(tmp_path: Path, action: str) -> None:
    store = EvidenceDecisionStore(tmp_path)
    store.create(_decision())

    resolved = store.resolve(
        "evidence-decision-001", action, decision_note="用户选择"
    )

    assert resolved.status == "resolved"
    assert resolved.selected_action == action
    assert resolved.decision_note == "用户选择"
    assert resolved.resolved_at is not None
    with pytest.raises(ValueError, match="already resolved"):
        store.resolve("evidence-decision-001", action)
