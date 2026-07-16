import pytest
from pydantic import ValidationError

from manyselves.core.reporting.agentic_models import WorkflowDecisionSubmission
from manyselves.core.reporting.models import ReviewIssue
from manyselves.core.reporting.review_gate import (
    ReviewGate,
    UnresolvedBlockingReviewError,
)


def _blocking_issue(**updates) -> ReviewIssue:
    values = {
        "module_id": "2.4",
        "submodule_id": "2.4.1.1",
        "claim_id": "C-2.4-001",
        "kind": "unsupported_fact",
        "message": "现场事实缺少可追溯证据。",
        "severity": "blocking",
        "affected_claim_ids": ["C-2.4-001"],
        "evidence_refs": ["E-0042"],
        "blocking_reason": "继续交付会把未经支持的内容写成现场事实。",
        "resolution_criteria": ["删除确定性表述或补充能够支持该事实的项目证据"],
        "owner_agent_id": "module-2.4-specialist",
    }
    values.update(updates)
    return ReviewIssue(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("affected_claim_ids", []),
        ("evidence_refs", []),
        ("blocking_reason", None),
        ("resolution_criteria", []),
        ("owner_agent_id", None),
    ],
)
def test_blocking_issue_requires_actionable_review_contract(field: str, value) -> None:
    with pytest.raises(ValidationError):
        _blocking_issue(**{field: value})


def test_warning_issue_does_not_require_blocking_contract() -> None:
    issue = ReviewIssue(
        module_id="2.4",
        kind="style",
        message="建议精简重复背景。",
        severity="warning",
    )

    decision = ReviewGate.evaluate([issue])
    assert decision.clear is True
    assert decision.warning_issue_ids == [issue.id]


def test_unresolved_blocking_issue_prevents_accept() -> None:
    issue = _blocking_issue()
    decision = WorkflowDecisionSubmission(
        decision="accept",
        rationale="直接接受。",
        target_module_ids=["2.4"],
    )

    with pytest.raises(UnresolvedBlockingReviewError, match=issue.id):
        ReviewGate.ensure_decision_allowed(decision, [issue])


def test_resolved_blocking_issue_no_longer_blocks() -> None:
    issue = _blocking_issue(
        status="resolved",
        resolved_by_agent_id="evidence-auditor",
        resolution_note="已删除无证据的确定性现场表述。",
        resolution_evidence_refs=["Work/runs/run-1/modules/2.4-r1.json"],
    )

    decision = ReviewGate.evaluate([issue])
    assert decision.clear is True
    assert decision.resolved_blocking_issue_ids == [issue.id]
