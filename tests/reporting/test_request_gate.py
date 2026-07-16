from autoreport.core.reporting.coverage import evaluate_coverage
from autoreport.core.reporting.models import ReportRequest
from autoreport.core.reporting.request_gate import RequestGate


def _request(policy: str) -> ReportRequest:
    return ReportRequest(
        instruction="生成设备模块",
        target_modules=["2.4"],
        missing_evidence_policy=policy,
    )


def test_ask_and_block_stop_before_professional_agents() -> None:
    for policy in ("ask", "block"):
        request = _request(policy)
        decision = RequestGate.evaluate(request, evaluate_coverage(request, []))

        assert decision.proceed is False
        assert decision.status == "blocked"
        assert decision.missing_evidence
        assert all(item.startswith("2.4") for item in decision.missing_evidence)


def test_draft_and_skip_continue_with_explicit_pending_coverage() -> None:
    for policy in ("draft", "skip"):
        request = _request(policy)
        decision = RequestGate.evaluate(request, evaluate_coverage(request, []))

        assert decision.proceed is True
        assert decision.status == "running"
        assert decision.missing_evidence
