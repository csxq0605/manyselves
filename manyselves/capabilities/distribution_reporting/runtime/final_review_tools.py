"""Capability-owned deterministic tools for the opening Final review round."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalReviewContext,
)


def _restore_state(value: Mapping[str, Any]) -> dict[str, Any]:
    state = deepcopy(dict(value))
    if state.get("edited_report") is not None:
        state["edited_report"] = EditedReportSubmission.model_validate(
            state["edited_report"]
        )
    return state


class FinalReviewTools:
    """Initialize and advance the typed Final review cycle without a Runner."""

    def start_cycle(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalReviewContext:
        state = _restore_state(values["state"])
        current = EditedReportSubmission.model_validate(state["edited_report"])
        subject_ref = str(
            state.get("chief_candidate_ref")
            or f"Work/runs/{state['run_id']}/edited-revisions/chief-r0.json"
        )
        outcomes = {
            chapter_id: DeclarativeFinalChapterOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        if "final_review_completion_ref" in state:
            return DeclarativeFinalReviewContext(
                state=state,
                current=current,
                subject_ref=subject_ref,
                findings_by_chapter={},
                pending_by_chapter={},
                initial_lane_refs={},
                already_completed=True,
            )
        active = tuple(
            chapter_id
            for chapter_id in ("1", "3", "4")
            if chapter_id in outcomes and outcomes[chapter_id].status != "skipped"
        )
        submissions = {
            chapter_id: cast(
                FinalChapterLaneFindingSubmission,
                outcomes[chapter_id].submission,
            )
            for chapter_id in active
        }
        findings = {
            chapter_id: list(submissions[chapter_id].findings) for chapter_id in active
        }
        return DeclarativeFinalReviewContext(
            state=state,
            current=current,
            subject_ref=subject_ref,
            findings_by_chapter=findings,
            pending_by_chapter={
                chapter_id: list(chapter_findings)
                for chapter_id, chapter_findings in findings.items()
                if chapter_findings
            },
            initial_lane_refs={
                chapter_id: cast(str, outcomes[chapter_id].output_ref)
                for chapter_id in active
            },
            initial_residual_risks=[
                risk
                for chapter_id in active
                for risk in submissions[chapter_id].residual_risks
            ],
        )

    @staticmethod
    def needs_round(value: Any) -> bool:
        review = DeclarativeFinalReviewContext.model_validate(value)
        return bool(review.pending_by_chapter) and not review.already_completed

    @staticmethod
    def advance_round(value: Any) -> DeclarativeFinalReviewContext:
        review = DeclarativeFinalReviewContext.model_validate(value)
        if not review.pending_by_chapter:
            return review
        maximum = max(1, int(review.state.get("max_final_review_rounds", 3)))
        next_round = review.revision_number + 1
        if next_round > maximum:
            raise RuntimeError("final chapter review exceeded the maximum revision rounds")
        return review.model_copy(
            update={
                "revision_number": next_round,
                "revision_responses": {},
            }
        )


def build_final_review_tool_implementations() -> dict[str, Any]:
    tools = FinalReviewTools()
    return {
        "start-final-review-cycle": tools.start_cycle,
        "final-review-needs-round": tools.needs_round,
        "advance-final-review-round": tools.advance_round,
    }


__all__ = [
    "FinalReviewTools",
    "build_final_review_tool_implementations",
]
