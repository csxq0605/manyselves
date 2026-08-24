"""Capability-owned deterministic tools for Final review and Chief revision."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ChiefChapterLaneRevisionSubmission,
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
    FinalChapterLaneVerdictSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalChiefRevisionAgentResult,
    DeclarativeFinalChiefRevisionContext,
    DeclarativeFinalChiefRevisionOutcome,
    DeclarativeFinalRecheckAgentResult,
    DeclarativeFinalRecheckContext,
    DeclarativeFinalRecheckOutcome,
    DeclarativeFinalReviewContext,
    DeclarativeFinalVerdictRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
    FinalAuditSnapshot,
    FinalChapterLaneInput,
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    SpecialTopicPlan,
    chapter_section_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

from .chief_sources import materialize_chief_module_sources
from .delivery_projection import build_delivery_projection
from .report_validation import validate_final_report_structure
from .template_skill_paths import template_skill_ref

_STATIC_SECTION_BODIES = {
    "1.1": "assessment_background",
    "1.2": "findings_overview",
    "1.3": "regional_executive_summary",
    "3.1.1": "risk_panorama",
    "3.1.2": "dimension_risk_analysis",
    "3.1.3": "data_gap_analysis",
    "3.2": "improvement_action_plan",
}


def _template_skill_context(
    state: Mapping[str, Any],
    workspace: Any,
    skill_id: str,
) -> str:
    """Embed one materialized template Skill in the Agent task envelope."""

    texts = state.get("template_skill_text", {})
    content = texts.get(skill_id, "") if isinstance(texts, Mapping) else ""
    if not content:
        path = workspace / template_skill_ref(skill_id)
        if path.is_file():
            content = path.read_text(encoding="utf-8")
    if not content:
        return ""
    return (
        f'<template_role_skill id="{skill_id}" delivery_mode="inline">\n'
        f"{content}\n"
        "</template_role_skill>"
    )


def _chief_template_skill_context(
    state: Mapping[str, Any],
    workspace: Any,
    chapter_ids: tuple[str, ...] | list[str] | set[str],
) -> str:
    """Embed complete Chief Skills for exactly the requested chapters."""

    requested = set(chapter_ids)
    return "\n\n".join(
        context
        for chapter_id in ("1", "3", "4")
        if chapter_id in requested
        for context in (
            _template_skill_context(
                state,
                workspace,
                f"chief-editor-chapter-{chapter_id}",
            ),
        )
        if context
    )


def _restore_state(value: Mapping[str, Any]) -> dict[str, Any]:
    state = deepcopy(dict(value))
    modules = state.get("module_submissions")
    if isinstance(modules, Mapping):
        state["module_submissions"] = {
            module_id: ModuleSubmission.model_validate(module)
            for module_id, module in modules.items()
        }
    if state.get("edited_report") is not None:
        state["edited_report"] = EditedReportSubmission.model_validate(
            state["edited_report"]
        )
    if isinstance(state.get("special_topic_plan"), Mapping):
        state["special_topic_plan"] = SpecialTopicPlan.model_validate(
            state["special_topic_plan"]
        )
    return state


def _split_special_topic_analysis(
    markdown: str,
    plan: Any,
    *,
    allow_single_body: bool = True,
) -> dict[str, str]:
    plan.validate_analysis(markdown, allow_chapter_heading=True)
    expected = [section.section_id for section in plan.sections]
    lines = markdown.strip().splitlines()
    heading_re = re.compile(r"^\s*#{1,6}\s+(4\.\d+)\s+.+?\s*$")
    matches = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := heading_re.match(line))
    ]
    if not matches:
        if allow_single_body and len(expected) == 1:
            return {expected[0]: markdown.strip()}
        raise ValueError("Chapter 4 result must contain one heading per planned subsection")
    sections: dict[str, str] = {}
    for position, (start, section_id) in enumerate(matches):
        end = matches[position + 1][0] if position + 1 < len(matches) else len(lines)
        sections[section_id] = "\n".join(lines[start + 1 : end]).strip()
    if set(sections) != set(expected):
        raise ValueError("Chapter 4 result headings must match the active special-topic plan")
    return sections


def _section_ids(
    edited: EditedReportSubmission,
    chapter_id: Literal["1", "3", "4"],
) -> tuple[str, ...]:
    return chapter_section_ids(chapter_id, edited.special_topic_plan)


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
    return {
        section_id: getattr(edited, _STATIC_SECTION_BODIES[section_id])
        for section_id in _section_ids(edited, chapter_id)
    }


def _render_special_topic_analysis(
    section_bodies: dict[str, str],
    plan: Any,
) -> str | None:
    if plan is None:
        return None
    expected = [section.section_id for section in plan.sections]
    if set(section_bodies) != set(expected):
        raise ValueError("Chapter 4 reducer received an incomplete subsection set")
    return "\n\n".join(
        f"### {section.section_id} {section.title}\n"
        f"{section_bodies[section.section_id].strip()}"
        for section in plan.sections
    )


def _source_projection(
    state: Mapping[str, Any],
    chapter_id: Literal["1", "3", "4"],
    plan: Any,
    *,
    store: ReportingStore,
) -> tuple[dict[str, str], list[str]]:
    source_refs = [
        str(state["cross_review_completion_ref"])
    ] if state.get("cross_review_completion_ref") else []
    modules = state.get("module_submissions", {})
    if chapter_id == "1":
        source_context = {
            f"module-{module_id}": json.dumps(
                {
                    "module_id": module_id,
                    "revision": getattr(module, "revision", 0),
                    "submodule_ids": sorted(getattr(module, "submodule_narratives", {})),
                    "unresolved_questions": list(
                        getattr(module, "unresolved_questions", [])
                    ),
                    "claim_ids": [
                        claim.id for claim in getattr(module, "claims", [])
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )[:2400]
            for module_id, module in sorted(
                modules.items(), key=lambda item: float(item[0])
            )
        }
        source_context["approved_markers"] = ",".join(
            f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_MODULE_IDS
        )
    elif chapter_id == "3":
        source_context = {
            "cross_synthesis": json.dumps(
                [
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else item
                    for item in state.get("cross_synthesis_inputs", [])
                ],
                ensure_ascii=False,
                sort_keys=True,
            )[:6000],
            "module_boundaries": ",".join(
                f"{module_id}:{','.join(sorted(getattr(module, 'submodule_narratives', {})))}"
                for module_id, module in sorted(
                    modules.items(), key=lambda item: float(item[0])
                )
            ),
        }
    else:
        source_context = {
            "special_topic_plan": json.dumps(
                plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan,
                ensure_ascii=False,
                sort_keys=True,
            )[:12_000]
        }
        if state.get("special_topic_knowledge_ref"):
            source_refs.append(str(state["special_topic_knowledge_ref"]))
    evidence_ref = state.get("preparation_refs", {}).get("evidence")
    if evidence_ref:
        source_refs.append(str(evidence_ref))
    source_refs.extend(materialize_chief_module_sources(store, state))
    return source_context, list(dict.fromkeys(ref for ref in source_refs if ref))


class FinalReviewTools:
    """Initialize and advance the typed Final review cycle without a Runner."""

    def __init__(
        self,
        *,
        workspace: Any = ".",
        store: ReportingStore | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.store = store or ReportingStore(workspace)

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

    def prepare_chief_revision(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChiefRevisionContext:
        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        chapter_id = cast(Literal["1", "3", "4"], str(values["chapter_id"]))
        findings = review.pending_by_chapter.get(chapter_id, [])
        if not findings:
            return DeclarativeFinalChiefRevisionContext(
                chapter_id=chapter_id,
                status="skipped",
            )
        state = _restore_state(review.state)
        edited = review.current
        chapter_section_ids = _section_ids(edited, chapter_id)
        target_section_ids = {
            section_id
            for finding in findings
            for section_id in finding.target_section_ids
        }
        section_ids = tuple(
            section_id
            for section_id in chapter_section_ids
            if section_id in target_section_ids
        )
        current_section_bodies = _section_bodies(edited, chapter_id)
        source_context, source_refs = _source_projection(
            state,
            chapter_id,
            edited.special_topic_plan,
            store=self.store,
        )
        revision = review.revision_number
        run_id = str(state["run_id"])
        contract = ChiefChapterLaneInput(
            phase="revision",
            run_id=run_id,
            subject_ref=review.subject_ref,
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            section_bodies={
                section_id: current_section_bodies[section_id]
                for section_id in section_ids
            },
            source_context=source_context,
            source_refs=source_refs,
            assigned_findings=list(findings),
            special_topic_plan=edited.special_topic_plan,
            revision=revision,
        )
        input_ref = f"Work/runs/{run_id}/context/chief-chapter-{chapter_id}-input-r{revision}.json"
        self.store.write_json(input_ref, contract.model_dump(mode="json"))
        envelope = TaskEnvelope(
            task_id=f"chief-chapter-{chapter_id}-r{revision}",
            run_id=run_id,
            agent_id="chief-editor",
            objective=f"只修订 Chapter {chapter_id} 被 Final 指定的 finding 小节。",
            input_refs=[input_ref, *contract.source_refs],
            constraints=[
                "只提交 chief_chapter_lane_revision_submission 精确文本补丁，禁止提交完整报告或完整小节正文",
                (
                    "edits.target_section_id 和 section_ids 只能覆盖本轮 finding 指定的小节："
                    + ",".join(section_ids)
                ),
                "未列入 section_ids 的原章节正文由 Runtime 原样保留，不得重写或重复提交",
                "old_text 必须从对应 section_bodies 原样复制且唯一出现；new_text 只实现 assigned finding",
                "若 old_text 是完整小节正文，new_text 必须逐字包含完整 old_text，只能增补指定内容",
                "revision_responses 必须对应本章 findings",
            ],
            allowed_outputs=["chief_chapter_lane_revision_submission"],
            allowed_tools=["open_artifact", "search_text", "submit_result"],
            revision=revision,
            prior_result_ref=review.subject_ref,
            input_contract_kind="chief_chapter_lane_input",
            input_contract_ref=input_ref,
            artifact_delivery_modes={
                input_ref: "inline",
                **{
                    source_ref: "reference"
                    for source_ref in contract.source_refs
                },
            },
        )
        return DeclarativeFinalChiefRevisionContext(
            chapter_id=chapter_id,
            status="ready",
            contract=contract,
            input_ref=input_ref,
            envelope=envelope,
        )

    @staticmethod
    def chief_revision_requires_agent(value: Any) -> bool:
        return (
            DeclarativeFinalChiefRevisionContext.model_validate(value).status == "ready"
        )

    def accept_chief_revision(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalChiefRevisionContext:
        context = DeclarativeFinalChiefRevisionContext.model_validate(
            values["context"]
        )
        result = DeclarativeFinalChiefRevisionAgentResult.model_validate(
            values["result"]
        )
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(ChiefChapterLaneRevisionSubmission, result.submission)
        contract = cast(ChiefChapterLaneInput, context.contract)
        try:
            matches_identity = (
                submission.run_id == contract.run_id
                and submission.base_subject_ref == contract.subject_ref
                and submission.chapter_id == context.chapter_id
                and submission.revision == contract.revision
                and set(submission.section_ids).issubset(set(contract.section_ids))
            )
            if contract.revision == 1:
                matches_identity = matches_identity and {
                    response.finding_id for response in submission.revision_responses
                } == {finding.id for finding in contract.assigned_findings}
            if not matches_identity:
                raise ValueError(
                    f"chief chapter {context.chapter_id} revision identity mismatch"
                )
            parts = self._apply_chief_revision_edits(
                contract,
                submission,
            )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/chief-chapter-lane-"
                f"{context.chapter_id}-r{contract.revision}.json"
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
                "parts": parts,
            }
        )

    @staticmethod
    def _apply_chief_revision_edits(
        contract: ChiefChapterLaneInput,
        submission: ChiefChapterLaneRevisionSubmission,
    ) -> dict[str, str]:
        """Apply exact edits without allowing an entire section to be replaced."""

        bodies = dict(contract.section_bodies)
        for edit in submission.edits:
            body = bodies[edit.target_section_id]
            occurrences = body.count(edit.old_text)
            if occurrences != 1:
                raise ValueError(
                    "Chief revision old_text must occur exactly once in its target section"
                )
            if edit.old_text == body and edit.old_text not in edit.new_text:
                raise ValueError(
                    "Chief revision cannot replace an entire section; retain the current "
                    "body verbatim and add only the assigned change"
                )
            bodies[edit.target_section_id] = body.replace(
                edit.old_text,
                edit.new_text,
                1,
            )
        return {
            section_id: bodies[section_id]
            for section_id in submission.section_ids
        }

    @staticmethod
    def complete_chief_revision(
        value: Any,
    ) -> DeclarativeFinalChiefRevisionOutcome:
        context = DeclarativeFinalChiefRevisionContext.model_validate(value)
        if context.status == "failed":
            return DeclarativeFinalChiefRevisionOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeFinalChiefRevisionOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeFinalChiefRevisionOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
            parts=context.parts,
        )

    def reduce_chief_revisions(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalReviewContext:
        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        outcomes = {
            chapter_id: DeclarativeFinalChiefRevisionOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            chapter_id: outcome.error or "Final Chief revision lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=int)
            raise RuntimeError(failures[first])
        active = tuple(review.pending_by_chapter)
        parts_by_chapter = {
            chapter_id: outcomes[chapter_id].parts for chapter_id in active
        }
        updates = {
            section_id: body
            for chapter_parts in parts_by_chapter.values()
            for section_id, body in chapter_parts.items()
        }
        field_for_section = {
            "1.1": "assessment_background",
            "1.2": "findings_overview",
            "1.3": "regional_executive_summary",
            "3.1.1": "risk_panorama",
            "3.1.2": "dimension_risk_analysis",
            "3.1.3": "data_gap_analysis",
            "3.2": "improvement_action_plan",
        }
        state = _restore_state(review.state)
        plan = state.get("special_topic_plan") or review.current.special_topic_plan
        current = review.current.model_copy(
            update={
                **{
                    field_for_section[section_id]: body
                    for section_id, body in updates.items()
                    if section_id in field_for_section
                },
                **(
                    {
                        "special_topic_analysis": _render_special_topic_analysis(
                            {
                                **_section_bodies(review.current, "4"),
                                **parts_by_chapter["4"],
                            },
                            plan,
                        )
                    }
                    if "4" in parts_by_chapter
                    else {}
                ),
            }
        )
        run_id = str(state["run_id"])
        revision = review.revision_number
        subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r{revision}.json"
        self.store.write_json(subject_ref, current.model_dump(mode="json"))
        state["edited_report"] = current
        state["chief_candidate_ref"] = subject_ref
        return review.model_copy(
            update={
                "state": state,
                "current": current,
                "subject_ref": subject_ref,
                "revision_responses": {
                    chapter_id: list(
                        cast(
                            ChiefChapterLaneRevisionSubmission,
                            outcomes[chapter_id].submission,
                        ).revision_responses
                    )
                    for chapter_id in active
                },
            }
        )

    def accept_recheck(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalRecheckContext:
        """Accept one typed Final Auditor verdict for its affected chapter lane."""

        context = DeclarativeFinalRecheckContext.model_validate(values["context"])
        result = DeclarativeFinalRecheckAgentResult.model_validate(
            values["result"]
        )
        if result.status == "failed":
            return context.model_copy(update={"status": "failed", "error": result.error})
        submission = cast(FinalChapterLaneVerdictSubmission, result.submission)
        contract = cast(FinalChapterLaneInput, context.contract)
        try:
            expected_ids = {finding.id for finding in contract.required_findings}
            actual_ids = {verdict.finding_id for verdict in submission.verdicts}
            if (
                submission.run_id != contract.run_id
                or submission.chapter_id != context.chapter_id
                or set(submission.checked_section_ids) != set(contract.section_ids)
                or actual_ids != expected_ids
            ):
                raise ValueError(
                    f"final chapter {context.chapter_id} verdict does not close its lane findings"
                )
            output_ref = (
                f"Work/runs/{contract.run_id}/reviews/final-chapter-lane-"
                f"{context.chapter_id}-r{contract.revision}.json"
            )
            self.store.write_json(output_ref, submission.model_dump(mode="json"))
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
    def complete_recheck(value: Any) -> DeclarativeFinalRecheckOutcome:
        """Project an accepted Final recheck lane into its cohort outcome."""

        context = DeclarativeFinalRecheckContext.model_validate(value)
        if context.status == "failed":
            return DeclarativeFinalRecheckOutcome(
                chapter_id=context.chapter_id,
                status="failed",
                error=context.error,
            )
        if context.status == "skipped":
            return DeclarativeFinalRecheckOutcome(
                chapter_id=context.chapter_id,
                status="skipped",
            )
        return DeclarativeFinalRecheckOutcome(
            chapter_id=context.chapter_id,
            status="completed",
            submission=context.submission,
            output_ref=context.output_ref,
        )

    def reduce_rechecks(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeFinalReviewContext:
        """Reduce Final verdicts while retaining history and next-round findings."""

        review = DeclarativeFinalReviewContext.model_validate(values["review"])
        outcomes = {
            chapter_id: DeclarativeFinalRecheckOutcome.model_validate(outcome)
            for chapter_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            chapter_id: outcome.error or "Final recheck lane failed"
            for chapter_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if failures:
            first = min(failures, key=int)
            raise RuntimeError(failures[first])
        active = tuple(review.pending_by_chapter)
        next_pending: dict[str, list[Any]] = {}
        history = list(review.verdict_history)
        latest_refs = dict(review.latest_verdict_refs)
        for chapter_id in active:
            outcome = outcomes[chapter_id]
            payload = cast(FinalChapterLaneVerdictSubmission, outcome.submission)
            output_ref = cast(str, outcome.output_ref)
            history.append(
                DeclarativeFinalVerdictRecord(
                    submission=payload,
                    output_ref=output_ref,
                )
            )
            latest_refs[chapter_id] = output_ref
            verdict_by_id = {
                verdict.finding_id: verdict for verdict in payload.verdicts
            }
            pending = [
                finding
                for finding in review.pending_by_chapter[chapter_id]
                if verdict_by_id[finding.id].verdict != "resolved"
            ]
            pending.extend(payload.new_findings)
            if pending:
                next_pending[chapter_id] = pending
        run_id = str(review.state["run_id"])
        revision = review.revision_number
        aggregate_ref = (
            f"Work/runs/{run_id}/reviews/final-recheck-r{revision}-aggregate.json"
        )
        self.store.write_json(
            aggregate_ref,
            {
                "run_id": run_id,
                "stage": f"final-recheck-r{revision}",
                "status": "completed",
                "lane_ids": list(active),
                "result_ref": review.subject_ref,
            },
        )
        return review.model_copy(
            update={
                "pending_by_chapter": next_pending,
                "verdict_history": history,
                "latest_verdict_refs": latest_refs,
            }
        )

    def complete_review(self, value: Any) -> dict[str, Any]:
        """Persist the existing Final completion handoff before Delivery."""

        review = DeclarativeFinalReviewContext.model_validate(value)
        state = _restore_state(review.state)
        if review.already_completed:
            return state
        run_id = str(state["run_id"])
        revision = review.revision_number
        state["edited_report"] = review.current
        state["chief_candidate_ref"] = review.subject_ref
        state["final_review_restart_round"] = revision or 1
        completion = ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="final-chapter-wave",
            subject_refs=[review.subject_ref],
            finding_refs=list(review.initial_lane_refs.values()),
            verdict_refs=[record.output_ref for record in review.verdict_history],
            resolved_finding_ids=sorted(
                {
                    finding.id
                    for findings in review.findings_by_chapter.values()
                    for finding in findings
                }
                | {
                    finding.id
                    for record in review.verdict_history
                    for finding in record.submission.new_findings
                }
            ),
        )
        completion_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
        self.store.write_json(completion_ref, completion.model_dump(mode="json"))
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in getattr(
                state.get("module_submissions", {}).get(module_id),
                "claims",
                [],
            )
        ]
        canonical_ref = (
            f"Work/runs/{run_id}/validation/report-chief-candidate-r{revision}.md"
        )
        _, canonical = build_delivery_projection(
            self.workspace,
            state,
            review.current,
            claims,
        )
        validate_final_report_structure(
            store=self.store,
            state=state,
            markdown=canonical,
            phase=f"chief-candidate-r{revision}",
        )
        validation_ref = (
            f"Work/runs/{run_id}/reviews/"
            f"report-integrity-chief-candidate-r{revision}.json"
        )
        snapshot_ref = f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        snapshot = FinalAuditSnapshot(
            run_id=run_id,
            subject_ref=review.subject_ref,
            subject_revision=revision,
            canonical_markdown_ref=canonical_ref,
            validation_report_ref=validation_ref,
            completion_ref=completion_ref,
        )
        self.store.write_json(snapshot_ref, snapshot.model_dump(mode="json"))
        state["final_review_completion_ref"] = completion_ref
        state["final_audit_snapshot_ref"] = snapshot_ref
        state["final_residual_risks"] = list(review.initial_residual_risks)
        state["final_chapter_lane_refs"] = dict(review.initial_lane_refs)
        state["aggregate_refs"] = {
            **dict(state.get("aggregate_refs", {})),
            "final": completion_ref,
        }
        return state


def build_final_review_tool_implementations(
    *,
    workspace: Any = ".",
    store: ReportingStore | None = None,
) -> dict[str, Any]:
    tools = FinalReviewTools(workspace=workspace, store=store)
    return {
        "start-final-review-cycle": tools.start_cycle,
        "final-review-needs-round": tools.needs_round,
        "advance-final-review-round": tools.advance_round,
        "prepare-current-final-chief-revision": tools.prepare_chief_revision,
        "final-chief-revision-requires-agent": tools.chief_revision_requires_agent,
        "accept-current-final-chief-revision": tools.accept_chief_revision,
        "complete-current-final-chief-revision": tools.complete_chief_revision,
        "reduce-final-chief-revision-cohort": tools.reduce_chief_revisions,
        "accept-current-final-recheck": tools.accept_recheck,
        "complete-current-final-recheck": tools.complete_recheck,
        "reduce-final-recheck-cohort": tools.reduce_rechecks,
        "complete-final-review": tools.complete_review,
    }


__all__ = [
    "FinalReviewTools",
    "build_final_review_tool_implementations",
]
