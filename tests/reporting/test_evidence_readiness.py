from manyselves.core.reporting.coverage import evaluate_coverage
from manyselves.core.reporting.evidence_readiness import EvidenceReadinessPolicy
from manyselves.core.reporting.models import ReportRequest


def _request(policy: str) -> ReportRequest:
    return ReportRequest(
        operation="module_report",
        instruction="生成设备模块",
        target_modules=["2.4"],
        missing_evidence_policy=policy,
    )


def test_ask_and_block_pause_before_professional_agents() -> None:
    for policy in ("ask", "block"):
        request = _request(policy)
        decision = EvidenceReadinessPolicy.evaluate(
            request, evaluate_coverage(request, [])
        )

        assert decision.should_block is True
        assert decision.missing_evidence
        assert all(item.startswith("2.4") for item in decision.missing_evidence)


def test_readiness_identifies_affected_modules_for_resume_decision() -> None:
    request = _request("ask")
    decision = EvidenceReadinessPolicy.evaluate(
        request, evaluate_coverage(request, [])
    )

    assert decision.affected_modules == ["2.4"]


def test_draft_and_skip_continue_with_explicit_pending_coverage() -> None:
    for policy in ("draft", "skip"):
        request = _request(policy)
        decision = EvidenceReadinessPolicy.evaluate(
            request, evaluate_coverage(request, [])
        )

        assert decision.should_block is False
        assert decision.missing_evidence
