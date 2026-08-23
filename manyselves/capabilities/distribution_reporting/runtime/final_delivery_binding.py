"""Capability-owned bindings for the first declared Final chapter boundary.

This module contains only the deterministic preparation needed to enter the
file-defined Final chapter lanes.  Agent invocation, Final review rounds, and
Delivery remain separate declared boundaries until their Capability-owned
adapters are available.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

from manyselves.capabilities.distribution_reporting.domain.final_specialization import (
    final_lane_specialization,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterAgentResult,
    DeclarativeFinalChapterContext,
    DeclarativeFinalChapterOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    FinalChapterLaneInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    chapter_section_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

_STATIC_SECTION_BODIES = {
    "1.1": "assessment_background",
    "1.2": "findings_overview",
    "1.3": "regional_executive_summary",
    "3.1.1": "risk_panorama",
    "3.1.2": "dimension_risk_analysis",
    "3.1.3": "data_gap_analysis",
    "3.2": "improvement_action_plan",
}


def _restore_state(value: Mapping[str, Any]) -> dict[str, Any]:
    state = deepcopy(dict(value))
    if state.get("edited_report") is not None:
        state["edited_report"] = EditedReportSubmission.model_validate(
            state["edited_report"]
        )
    return state


def _split_special_topic_analysis(
    markdown: str,
    plan: Any,
) -> dict[str, str]:
    plan.validate_analysis(markdown, allow_chapter_heading=True)
    expected = [section.section_id for section in plan.sections]
    heading_re = re.compile(r"^\s*#{1,6}\s+(4\.\d+)\s+.+?\s*$")
    matches = [
        (index, match.group(1))
        for index, line in enumerate(markdown.strip().splitlines())
        if (match := heading_re.match(line))
    ]
    if not matches:
        if len(expected) == 1:
            return {expected[0]: markdown.strip()}
        raise ValueError("Chapter 4 result must contain one heading per planned subsection")
    lines = markdown.strip().splitlines()
    sections: dict[str, str] = {}
    for position, (start, section_id) in enumerate(matches):
        end = matches[position + 1][0] if position + 1 < len(matches) else len(lines)
        sections[section_id] = "\n".join(lines[start + 1 : end]).strip()
    if set(sections) != set(expected):
        raise ValueError("Chapter 4 result headings must match the active special-topic plan")
    return sections


def _section_bodies(
    edited: EditedReportSubmission,
    chapter_id: Literal["1", "3", "4"],
) -> dict[str, str]:
    if chapter_id == "4":
        if edited.special_topic_plan is None or edited.special_topic_analysis is None:
            return {}
        return _split_special_topic_analysis(
            edited.special_topic_analysis,
            edited.special_topic_plan,
        )
    section_ids = CHAPTER1_SECTION_IDS if chapter_id == "1" else CHAPTER3_SECTION_IDS
    return {
        section_id: getattr(edited, _STATIC_SECTION_BODIES[section_id])
        for section_id in section_ids
    }


class FinalChapterTools:
    """Prepare Final lane contracts without a Reporting Runner dependency."""

    def __init__(self, *, workspace: Path, store: ReportingStore) -> None:
        self.workspace = Path(workspace)
        self.store = store

    def prepare_cohort(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Restore the typed report subject before the parallel Final lanes."""

        return _restore_state(value)

    def prepare_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChapterContext:
        state = _restore_state(values["state"])
        typed_chapter = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        edited = EditedReportSubmission.model_validate(state["edited_report"])
        if typed_chapter == "4" and edited.special_topic_plan is None:
            return DeclarativeFinalChapterContext(
                chapter_id=typed_chapter,
                status="skipped",
            )
        section_ids = chapter_section_ids(
            typed_chapter,
            edited.special_topic_plan,
        )
        contract = FinalChapterLaneInput(
            phase="initial",
            run_id=str(state["run_id"]),
            subject_ref=str(
                state.get("chief_candidate_ref")
                or f"Work/runs/{state['run_id']}/edited-revisions/chief-r0.json"
            ),
            chapter_id=typed_chapter,
            review_focus=list(final_lane_specialization(typed_chapter).review_focus),
            section_ids=list(section_ids),
            section_bodies=_section_bodies(edited, typed_chapter),
            special_topic_plan=edited.special_topic_plan,
            revision=0,
        )
        input_ref = (
            f"Work/runs/{contract.run_id}/context/"
            f"final-chapter-{typed_chapter}-input-r0.json"
        )
        self.store.write_json(input_ref, contract.model_dump(mode="json"))
        envelope = TaskEnvelope(
            task_id=f"final-chapter-{typed_chapter}-r0",
            run_id=contract.run_id,
            agent_id="chief-editor-auditor",
            objective=(
                f"只审查报告第{typed_chapter}章指定小节并提交 lane-local findings。"
            ),
            input_refs=[input_ref],
            constraints=[
                f"只覆盖 Chapter {typed_chapter} section_ids={','.join(section_ids)}",
                "不得复制其他章节正文、全局 EditedReport 或跨章节 finding",
                "提交 final_chapter_lane_finding_submission，findings target_section_ids 必须留在本 lane",
            ],
            allowed_outputs=["final_chapter_lane_finding_submission"],
            allowed_tools=["submit_result"],
            revision=0,
            input_contract_kind="final_chapter_lane_input",
            input_contract_ref=input_ref,
            artifact_delivery_modes={input_ref: "inline"},
            inline_context=final_lane_specialization(typed_chapter).prompt_context(),
        )
        return DeclarativeFinalChapterContext(
            chapter_id=typed_chapter,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def requires_agent(value: Any) -> bool:
        return DeclarativeFinalChapterContext.model_validate(value).status == "ready"

    def accept_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChapterContext:
        """Persist one completed Auditor lane and accept its typed result."""

        context = DeclarativeFinalChapterContext.model_validate(values["context"])
        result = DeclarativeFinalChapterAgentResult.model_validate(values["result"])
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(FinalChapterLaneFindingSubmission, result.submission)
        contract = cast(FinalChapterLaneInput, context.contract)
        try:
            if (
                submission.run_id != contract.run_id
                or submission.chapter_id != context.chapter_id
                or set(submission.checked_section_ids) != set(contract.section_ids)
            ):
                raise ValueError(
                    f"final chapter {context.chapter_id} returned an out-of-scope finding lane"
                )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/"
                f"final-chapter-lane-{context.chapter_id}-r0.json"
            )
            self.store.write_json(
                output_ref,
                submission.model_dump(mode="json"),
            )
        except BaseException as exc:
            return context.model_copy(update={"status": "failed", "error": str(exc)})
        return context.model_copy(
            update={
                "status": "accepted",
                "submission": submission,
                "output_ref": output_ref,
            }
        )

    @staticmethod
    def complete_lane(value: Any) -> DeclarativeFinalChapterOutcome:
        """Project an accepted lane context into its terminal cohort outcome."""

        context = DeclarativeFinalChapterContext.model_validate(value)
        if context.status == "failed":
            return DeclarativeFinalChapterOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeFinalChapterOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeFinalChapterOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
        )

    def reduce_cohort(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the completed initial Final lane projection for the next workflow."""

        state = _restore_state(values["state"])
        outcomes = {
            chapter_id: DeclarativeFinalChapterOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            chapter_id: outcome.error or "Final chapter lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=int)
            raise RuntimeError(failures[first])
        active_chapters = tuple(
            chapter_id
            for chapter_id in ("1", "3", "4")
            if chapter_id in outcomes and outcomes[chapter_id].status != "skipped"
        )
        run_id = str(state["run_id"])
        initial_projection_ref = f"Work/runs/{run_id}/reviews/final-initial-aggregate.json"
        self.store.write_json(
            initial_projection_ref,
            {
                "run_id": run_id,
                "stage": "final-initial",
                "status": "completed",
                "lane_ids": list(active_chapters),
                "result_refs": {
                    chapter_id: outcomes[chapter_id].output_ref
                    for chapter_id in active_chapters
                },
            },
        )
        return state


def build_final_chapter_tool_implementations(
    *,
    workspace: Path,
    store: ReportingStore | None = None,
) -> dict[str, Any]:
    """Bind the deterministic Final preparation Tools used by aggregate tail."""

    tools = FinalChapterTools(
        workspace=workspace,
        store=store or ReportingStore(workspace),
    )
    return {
        "prepare-final-chapter-cohort": tools.prepare_cohort,
        "prepare-current-final-chapter": tools.prepare_lane,
        "final-chapter-initial-requires-agent": tools.requires_agent,
        "accept-current-final-chapter-initial": tools.accept_lane,
        "complete-current-final-chapter": tools.complete_lane,
        "reduce-final-chapter-cohort": tools.reduce_cohort,
    }


__all__ = [
    "FinalChapterTools",
    "build_final_chapter_tool_implementations",
]
