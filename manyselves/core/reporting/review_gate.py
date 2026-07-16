"""Structural review gate; professional correctness remains owned by reviewers."""

from __future__ import annotations

from pydantic import Field

from .agentic_models import StrictModel, WorkflowDecisionSubmission
from .models import ReviewIssue


class UnresolvedBlockingReviewError(RuntimeError):
    """Raised when orchestration attempts to bypass an open blocking review issue."""


class ReviewGateDecision(StrictModel):
    clear: bool
    open_blocking_issue_ids: list[str] = Field(default_factory=list)
    resolved_blocking_issue_ids: list[str] = Field(default_factory=list)
    warning_issue_ids: list[str] = Field(default_factory=list)


class ReviewGate:
    """Evaluate issue lifecycle state without making a professional judgment."""

    @staticmethod
    def evaluate(issues: list[ReviewIssue]) -> ReviewGateDecision:
        open_blocking = [
            issue.id for issue in issues if issue.severity == "blocking" and issue.status == "open"
        ]
        resolved_blocking = [
            issue.id
            for issue in issues
            if issue.severity == "blocking" and issue.status == "resolved"
        ]
        warnings = [issue.id for issue in issues if issue.severity == "warning"]
        return ReviewGateDecision(
            clear=not open_blocking,
            open_blocking_issue_ids=open_blocking,
            resolved_blocking_issue_ids=resolved_blocking,
            warning_issue_ids=warnings,
        )

    @classmethod
    def require_clear(cls, issues: list[ReviewIssue]) -> ReviewGateDecision:
        decision = cls.evaluate(issues)
        if not decision.clear:
            raise UnresolvedBlockingReviewError(
                "unresolved blocking review issues: " + ", ".join(decision.open_blocking_issue_ids)
            )
        return decision

    @classmethod
    def ensure_decision_allowed(
        cls,
        decision: WorkflowDecisionSubmission,
        issues: list[ReviewIssue],
    ) -> None:
        if decision.decision == "accept":
            cls.require_clear(issues)
