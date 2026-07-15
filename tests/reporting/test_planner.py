from pathlib import Path

import pytest

from autoreport.core.reporting.coverage import evaluate_coverage
from autoreport.core.reporting.models import EvidenceItem, ReportRequest, SourceLocation
from autoreport.core.reporting.planner import CoveragePlanningError, plan_modules


def _grounding_evidence() -> EvidenceItem:
    return EvidenceItem(
        id="ev-ground",
        subject="总电箱",
        fact="进线缺少保护地线",
        source=SourceLocation(file_id="file-1", path=Path("Inputs/S4-4.xlsx")),
        module_id="2.4",
        submodule_id="2.4.2.2",
    )


@pytest.mark.parametrize("policy", ["ask", "block"])
def test_planner_stops_before_drafting_when_required_evidence_is_missing(
    policy: str,
) -> None:
    request = ReportRequest(
        instruction="生成 2.4",
        target_modules=["2.4"],
        missing_evidence_policy=policy,
    )
    coverage = evaluate_coverage(request, [])

    with pytest.raises(CoveragePlanningError, match="2.4.1.1"):
        plan_modules(request, coverage, [])


def test_skip_policy_plans_only_ready_submodules() -> None:
    request = ReportRequest(
        instruction="生成 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="skip",
    )
    evidence = [_grounding_evidence()]
    coverage = evaluate_coverage(request, evidence)

    task = plan_modules(request, coverage, evidence)[0]

    assert task.submodule_evidence == {"2.4.2.2": ["ev-ground"]}
    assert "2.4.2.1" in task.skipped_submodules
    assert task.allow_unverified is False


def test_draft_policy_records_missing_submodules_as_unverified() -> None:
    request = ReportRequest(
        instruction="生成 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="draft",
    )
    coverage = evaluate_coverage(request, [])

    task = plan_modules(request, coverage, [])[0]

    assert "2.4.1.1" in task.missing_submodules
    assert task.allow_unverified is True
    assert task.evidence_ids == []
