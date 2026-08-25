from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from manyselves.core.reporting.agentic_models import (
    ChapterScopedFinalReviewFinding,
    ChapterScopedFinalReviewTargetChange,
    ChiefChapterLaneRevisionSubmission,
    ChiefChapterLaneSubmission,
    ChiefSectionTextEdit,
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
    FinalChapterLaneVerdictSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.core.reporting.final_specialization import final_lane_specialization
from manyselves.core.reporting.input_contracts import (
    ChiefChapterLaneInput,
    FinalChapterLaneInput,
)
from manyselves.core.reporting.models import (
    CHIEF_SECTION_RESULT_PART_IDS,
    REPORT_MODULE_IDS,
    SpecialTopicPlan,
)
from manyselves.core.reporting.parallel_runtime import AggregateState, RecoveryStateStore
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import AgentWorkflowError, ReportWorkflowRunner


def _read_json(service: SimpleNamespace, ref: str) -> dict:
    return json.loads((service.workspace / ref).read_text(encoding="utf-8"))


def test_chief_chapter_lanes_dispatch_independently_and_reduce(tmp_path: Path) -> None:
    service = SimpleNamespace(workspace=tmp_path, store=ReportingStore(tmp_path))
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._budget = None
    runner._approved_module_text = lambda module: f"approved {module.module_id}"
    runner._chief_template_skill_context = (
        lambda _state, chapters: f"skill:chief-editor:{','.join(chapters)}"
    )
    calls: list[tuple[str, str | None, str]] = []
    envelopes = []

    async def fake_agent(self, _agent_id, envelope, _artifacts, _workflow_id, *, session_key=None):
        calls.append((envelope.task_id, session_key, envelope.inline_context))
        envelopes.append(envelope)
        chapter_id = envelope.task_id.rsplit("-", 1)[-1]
        section_ids = {
            "1": ("1.1", "1.2", "1.3"),
            "3": ("3.1.1", "3.1.2", "3.1.3", "3.2"),
            "4": ("4.1",),
        }[chapter_id]
        draft_root = (
            tmp_path
            / "Work/runs/run-chapters/drafts"
            / envelope.task_id
            / "r0"
        )
        draft_root.mkdir(parents=True)
        refs: dict[str, str] = {}
        for section_id in section_ids:
            part_id = "special_topic_analysis" if chapter_id == "4" else CHIEF_SECTION_RESULT_PART_IDS[section_id]
            part = draft_root / f"{part_id}.md"
            part.write_text(
                "### 4.1 Dynamic topic\n\n"
                "lane-local body 4.1 explains the requested project boundary and verification method in detail."
                if chapter_id == "4"
                else f"lane-local body {section_id}",
                encoding="utf-8",
            )
            refs[part_id] = part.relative_to(tmp_path).as_posix()
        return ChiefChapterLaneSubmission(
            run_id="run-chapters",
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            part_refs=refs,
            revision=0,
        )

    runner._agent = MethodType(fake_agent, runner)
    modules = {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={part_id: "approved" for part_id in REPORT_TAXONOMY[module_id].submodules},
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id in REPORT_MODULE_IDS
    }
    state = {
        "run_id": "run-chapters",
        "module_submissions": modules,
        "cross_review_completion_ref": "Work/runs/run-chapters/reviews/cross-completion.json",
        "evidence_items": [],
        "photo_assets": [],
        "preparation_refs": {},
        "special_topic_plan": SpecialTopicPlan(
            source_ref="Inputs/topic.md",
            source_sha256="0" * 64,
            sections=[
                {
                    "section_id": "4.1",
                    "title": "Dynamic topic",
                    "requirement": "Explain the project-specific topic and verification method.",
                }
            ],
        ),
    }

    asyncio.run(runner._chief_edit_chapter_lanes(state, "workflow"))

    assert calls == [
        ("chief-chapter-1", "chief-chapter-1", "skill:chief-editor:1"),
        ("chief-chapter-3", "chief-chapter-3", "skill:chief-editor:3"),
        ("chief-chapter-4", "chief-chapter-4", "skill:chief-editor:4"),
    ]
    module_refs = [
        f"Work/runs/run-chapters/context/chief-source-modules/{module_id}.md"
        for module_id in REPORT_MODULE_IDS
    ]
    for envelope in envelopes:
        assert envelope.input_refs[1:] == [
            "Work/runs/run-chapters/reviews/cross-completion.json",
            *module_refs,
        ]
        assert envelope.artifact_delivery_modes == {
            envelope.input_refs[0]: "inline",
            **{ref: "reference" for ref in envelope.input_refs[1:]},
        }
    for module_id, ref in zip(REPORT_MODULE_IDS, module_refs, strict=True):
        assert (tmp_path / ref).read_text(encoding="utf-8") == (
            f"approved {module_id}\n"
        )
    assert "lane-local body 1.1" == state["edited_report"].assessment_background
    assert "lane-local body 3.1.1" == state["edited_report"].risk_panorama
    assert "lane-local body 4.1" in (state["edited_report"].special_topic_analysis or "")
    assert "approved 2.1" == state["edited_report"].module_narratives["2.1"]
    assert "chief" in state["aggregate_refs"]


def test_final_chief_exact_edits_preserve_unassigned_sections_and_reject_rewrite(
    tmp_path: Path,
) -> None:
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = SimpleNamespace(workspace=tmp_path, store=ReportingStore(tmp_path))
    contract = ChiefChapterLaneInput(
        phase="revision",
        run_id="run-exact-edit",
        subject_ref="Work/runs/run-exact-edit/edited-revisions/chief-r0.json",
        chapter_id="1",
        section_ids=["1.2"],
        section_bodies={"1.2": "原有发现正文。"},
        assigned_findings=[
            ChapterScopedFinalReviewFinding(
                id="F-1.2",
                target_section_ids=["1.2"],
                target_changes=[
                    ChapterScopedFinalReviewTargetChange(
                        target_section_id="1.2",
                        required_change="补充当前发现对应的核验方法、责任主体和后续处置说明。",
                        reviewer_checks=["保留原正文并补充核验说明。"],
                    )
                ],
                category="traceability",
                impact="blocking",
                observation="当前发现正文缺少核验说明，无法形成可追溯的管理结论。",
                evidence_refs=[
                    "Work/runs/run-exact-edit/edited-revisions/chief-r0.json"
                ],
            )
        ],
        revision=1,
    )
    additive = ChiefChapterLaneRevisionSubmission(
        run_id=contract.run_id,
        base_subject_ref=contract.subject_ref,
        chapter_id="1",
        revision=1,
        section_ids=["1.2"],
        edits=[
            ChiefSectionTextEdit(
                target_section_id="1.2",
                old_text="原有发现正文。",
                new_text="原有发现正文。\n\n补充核验说明。",
            )
        ],
    )
    assert runner._apply_chief_revision_edits(contract, additive) == {
        "1.2": "原有发现正文。\n\n补充核验说明。"
    }

    rewrite = additive.model_copy(
        update={
            "edits": [
                ChiefSectionTextEdit(
                    target_section_id="1.2",
                    old_text="原有发现正文。",
                    new_text="XX主中心采用2N UPS，并配置柴油发电机。",
                )
            ]
        }
    )
    with pytest.raises(AgentWorkflowError, match="cannot replace an entire section"):
        runner._apply_chief_revision_edits(contract, rewrite)


def test_chief_resume_reuses_only_business_valid_completed_lanes(tmp_path: Path) -> None:
    run_id = "run-chief-resume-body-gate"
    service = SimpleNamespace(workspace=tmp_path, store=ReportingStore(tmp_path))
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._budget = None
    runner._approved_module_text = lambda module: f"approved {module.module_id}"
    modules = {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                part_id: "approved"
                for part_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id in REPORT_MODULE_IDS
    }
    plan = SpecialTopicPlan(
        source_ref="Inputs/topic.md",
        source_sha256="0" * 64,
        sections=[
            {
                "section_id": "4.1",
                "title": "Dynamic topic",
                "requirement": "Explain the project boundary and verification method.",
            }
        ],
    )
    state = {
        "run_id": run_id,
        "module_submissions": modules,
        "cross_review_completion_ref": f"Work/runs/{run_id}/reviews/cross-completion.json",
        "evidence_items": [],
        "photo_assets": [],
        "preparation_refs": {},
        "special_topic_plan": plan,
    }
    chapter_sections = {
        "1": ("1.1", "1.2", "1.3"),
        "3": ("3.1.1", "3.1.2", "3.1.3", "3.2"),
        "4": ("4.1",),
    }
    recovery = RecoveryStateStore(tmp_path, run_id)
    for chapter_id, section_ids in chapter_sections.items():
        task_root = (
            tmp_path
            / "Work/runs"
            / run_id
            / "drafts"
            / f"chief-chapter-{chapter_id}"
            / "r0"
        )
        task_root.mkdir(parents=True)
        refs: dict[str, str] = {}
        if chapter_id == "4":
            part = task_root / "special_topic_analysis.md"
            part.write_text(
                "## 4. Runtime-owned wrapper\n\nLegacy chapter introduction.\n\n"
                "### 4.1 Dynamic topic\n\n"
                "Valid Chapter 4 body with verification detail.\n\n"
                "#### 4.1.1 Verification steps\n\n"
                "Nested Chapter 4 detail remains inside its planned parent.",
                encoding="utf-8",
            )
            refs["special_topic_analysis"] = part.relative_to(tmp_path).as_posix()
        else:
            for section_id in section_ids:
                part_id = CHIEF_SECTION_RESULT_PART_IDS[section_id]
                part = task_root / f"{part_id}.md"
                part.write_text(
                    f"## {section_id} Invalid persisted heading\n\nOld body.",
                    encoding="utf-8",
                )
                refs[part_id] = part.relative_to(tmp_path).as_posix()
        payload = ChiefChapterLaneSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            part_refs=refs,
            revision=0,
        )
        result_ref = (
            f"Work/runs/{run_id}/reviews/"
            f"chief-chapter-lane-{chapter_id}-r0.json"
        )
        service.store.write_json(result_ref, payload.model_dump(mode="json"))
        recovery.record_lane_attempt(
            {
                "run_id": run_id,
                "stage": "chief",
                "lane_id": chapter_id,
                "task_id": chapter_id,
                "attempt": 1,
                "revision": 0,
                "status": "completed",
                "result_ref": result_ref,
            }
        )

    # Chapter 4 is the only business-valid recovered lane.  Its persisted
    # input binds the same Cross baseline and lane-local source projection.
    source_context, source_refs = runner._chief_chapter_source_projection(state, "4")
    contract = ChiefChapterLaneInput(
        phase="initial",
        run_id=run_id,
        subject_ref=state["cross_review_completion_ref"],
        chapter_id="4",
        section_ids=["4.1"],
        source_context=source_context,
        source_refs=source_refs,
        special_topic_plan=plan,
        revision=0,
    )
    service.store.write_json(
        f"Work/runs/{run_id}/context/chief-chapter-4-input.json",
        contract.model_dump(mode="json"),
    )

    calls: list[str] = []

    async def fake_agent(
        self, _agent_id, envelope, _artifacts, _workflow_id, *, session_key=None
    ):
        chapter_id = envelope.task_id.rsplit("-", 1)[-1]
        calls.append(chapter_id)
        assert chapter_id in {"1", "3"}
        task_root = (
            tmp_path / "Work/runs" / run_id / "drafts" / envelope.task_id / "r0"
        )
        refs: dict[str, str] = {}
        for section_id in chapter_sections[chapter_id]:
            part_id = CHIEF_SECTION_RESULT_PART_IDS[section_id]
            part = task_root / f"{part_id}.md"
            part.write_text(f"Correct body for {section_id}.", encoding="utf-8")
            refs[part_id] = part.relative_to(tmp_path).as_posix()
        return ChiefChapterLaneSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            section_ids=list(chapter_sections[chapter_id]),
            part_refs=refs,
            revision=0,
        )

    runner._agent = MethodType(fake_agent, runner)

    asyncio.run(runner._chief_edit_chapter_lanes(state, "workflow"))

    assert calls == ["1", "3"]
    assert state["edited_report"].assessment_background == "Correct body for 1.1."
    assert state["edited_report"].risk_panorama == "Correct body for 3.1.1."
    assert "Valid Chapter 4 body" in state["edited_report"].special_topic_analysis
    assert "#### 4.1.1 Verification steps" in state["edited_report"].special_topic_analysis
    assert "Runtime-owned wrapper" not in state["edited_report"].special_topic_analysis


def _final_plan() -> SpecialTopicPlan:
    return SpecialTopicPlan(
        source_ref="Inputs/topic.md",
        source_sha256="0" * 64,
        sections=[
            {
                "section_id": "4.1",
                "title": "Topic A",
                "requirement": "Explain topic A and its verification method.",
            },
            {
                "section_id": "4.2",
                "title": "Topic B",
                "requirement": "Explain topic B and its verification method.",
            },
        ],
    )


def _final_subject(plan: SpecialTopicPlan) -> EditedReportSubmission:
    return EditedReportSubmission(
        title="Report",
        assessment_background="background body",
        findings_overview="findings body",
        regional_executive_summary="regional body",
        module_narratives={module_id: f"module {module_id}" for module_id in REPORT_MODULE_IDS},
        risk_panorama="risk body",
        dimension_risk_analysis="dimension body",
        data_gap_analysis="gap body",
        improvement_action_plan="action body",
        special_topic_plan=plan,
        special_topic_analysis=(
            "### 4.1 Topic A\nTopic A body with enough substantive detail.\n\n"
            "### 4.2 Topic B\nTopic B body with enough substantive detail."
        ),
    )


def _final_state(tmp_path: Path, run_id: str) -> tuple[ReportWorkflowRunner, dict]:
    service = SimpleNamespace(workspace=tmp_path, store=ReportingStore(tmp_path))
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._budget = None
    plan = _final_plan()
    subject = _final_subject(plan)
    subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    service.store.write_json(subject_ref, subject.model_dump(mode="json"))
    modules = {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                part_id: "approved" for part_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id in REPORT_MODULE_IDS
    }
    state = {
        "run_id": run_id,
        "module_submissions": modules,
        "edited_report": subject,
        "chief_candidate_ref": subject_ref,
        "evidence_items": [],
        "photo_assets": [],
        "preparation_refs": {},
        "special_topic_plan": plan,
    }
    def delivery_projection(self, _state, _edited, _claims):
        for revision in (0, 1, 2, 3):
            self.service.store.write_text(
                f"Work/runs/{run_id}/validation/report-chief-candidate-r{revision}.md",
                "canonical markdown",
            )

        return f"Work/runs/{run_id}/validation/report-chief-candidate-r0.md", "canonical markdown"

    def validate_final(self, _state, _canonical, _phase):
        for revision in (0, 1, 2, 3):
            self.service.store.write_json(
                f"Work/runs/{run_id}/reviews/report-integrity-chief-candidate-r{revision}.json",
                {"status": "valid"},
            )

    runner._delivery_projection = MethodType(delivery_projection, runner)
    runner._validate_final_report_structure = MethodType(validate_final, runner)
    return runner, state


def test_final_chapter_lanes_split_dynamic_topic_and_reduce(tmp_path: Path) -> None:
    runner, state = _final_state(tmp_path, "run-final-no-findings")
    runner._final_template_skill_context = (
        lambda _state, _chapter_id: "skill:final-auditor"
    )
    calls: list[tuple[str, str | None, dict, str]] = []

    async def fake_agent(self, _agent_id, envelope, _artifacts, _workflow_id, *, session_key=None):
        input_payload = _read_json(self.service, envelope.input_contract_ref)
        calls.append((envelope.task_id, session_key, input_payload, envelope.inline_context))
        chapter_id = input_payload["chapter_id"]
        return FinalChapterLaneFindingSubmission(
            run_id=state["run_id"],
            chapter_id=chapter_id,
            checked_section_ids=list(input_payload["section_ids"]),
            findings=[],
        )

    runner._agent = MethodType(fake_agent, runner)
    asyncio.run(runner._run_final_chapter_lanes(state, "workflow"))

    assert [task_id for task_id, _session, _input, _skill in calls] == [
        "final-chapter-1-r0",
        "final-chapter-3-r0",
        "final-chapter-4-r0",
    ]
    assert {session for _task, session, _input, _skill in calls} == {
        "final-chapter-1",
        "final-chapter-3",
        "final-chapter-4",
    }
    assert all("skill:final-auditor" in skill for _task, _session, _input, skill in calls)
    assert all("<final_lane_specialization" not in skill for _task, _session, _input, skill in calls)
    assert len({tuple(payload["review_focus"]) for _task, _session, payload, _skill in calls}) == 3
    chapter4_input = next(
        payload for task, _session, payload, _skill in calls if task.endswith("-4-r0")
    )
    assert set(chapter4_input["section_bodies"]) == {"4.1", "4.2"}
    assert "4.2" not in chapter4_input["section_bodies"]["4.1"]
    assert "4.1" not in chapter4_input["section_bodies"]["4.2"]
    assert "final" in state["aggregate_refs"]
    assert state["edited_report"].special_topic_analysis.startswith("### 4.1 Topic A")
    completion = _read_json(runner.service, state["final_review_completion_ref"])
    assert completion["reviewer_session_key"] == "final-chapter-wave"
    loaded, artifacts = runner._load_current_review_completion(
        run_id=state["run_id"],
        completion_ref=state["final_review_completion_ref"],
        lifecycle="final",
        reviewer_agent_id="chief-editor-auditor",
        reviewer_session_key={"chief-editor-auditor", "final-chapter-wave"},
    )
    assert loaded.model_dump(mode="json") == completion
    assert len(artifacts) == 3


def test_final_findings_revise_only_affected_chapter_lanes(tmp_path: Path) -> None:
    runner, state = _final_state(tmp_path, "run-final-findings")
    runner._chief_template_skill_context = (
        lambda _state, _chapters: "skill:chief-revision"
    )
    runner._final_template_skill_context = (
        lambda _state, _chapter_id: "skill:final-auditor"
    )
    calls: list[tuple[str, str | None, str]] = []
    # Seed the initial Chief aggregate so the strict RecoveryStateStore can
    # accept the revision aggregate emitted by this Final-focused test.
    recovery = RecoveryStateStore(tmp_path, state["run_id"])
    for chapter_id in ("1", "3", "4"):
        ref = f"Work/runs/{state['run_id']}/recovery/seed-chief-{chapter_id}.json"
        runner.service.store.write_json(ref, {})
        recovery.record_lane_attempt(
            {
                "run_id": state["run_id"],
                "stage": "chief",
                "lane_id": chapter_id,
                "task_id": chapter_id,
                "attempt": 1,
                "revision": 0,
                "status": "completed",
                "result_ref": ref,
            }
        )
    recovery.record_aggregate(
        AggregateState(
            run_id=state["run_id"],
            stage="chief",
            lane_ids=["1", "3", "4"],
            result_ref=state["chief_candidate_ref"],
            revision=0,
            status="completed",
        )
    )

    def finding(chapter_id: str, section_id: str, finding_id: str) -> ChapterScopedFinalReviewFinding:
        return ChapterScopedFinalReviewFinding(
            id=finding_id,
            target_section_ids=[section_id],
            target_changes=[
                ChapterScopedFinalReviewTargetChange(
                    target_section_id=section_id,
                    required_change="Add the missing explanation and verification detail.",
                    reviewer_checks=["The revised section states the check and outcome."],
                )
            ],
            category="completeness",
            impact="blocking",
            observation="The section does not explain the required verification detail.",
            evidence_refs=[state["chief_candidate_ref"]],
        )

    async def fake_agent(self, _agent_id, envelope, _artifacts, _workflow_id, *, session_key=None):
        calls.append((envelope.task_id, session_key, envelope.inline_context))
        payload = _read_json(self.service, envelope.input_contract_ref)
        chapter_id = payload["chapter_id"]
        section_ids = list(payload["section_ids"])
        if envelope.input_contract_kind == "final_chapter_lane_input" and payload["phase"] == "initial":
            findings = []
            if chapter_id == "1":
                findings = [finding("1", "1.1", "F-1")]
            elif chapter_id == "4":
                findings = [finding("4", "4.1", "F-4")]
            return FinalChapterLaneFindingSubmission(
                run_id=state["run_id"],
                chapter_id=chapter_id,
                checked_section_ids=section_ids,
                findings=findings,
            )
        if envelope.input_contract_kind == "chief_chapter_lane_input":
            return ChiefChapterLaneRevisionSubmission(
                run_id=state["run_id"],
                base_subject_ref=state["chief_candidate_ref"],
                chapter_id=chapter_id,
                revision=1,
                section_ids=section_ids,
                edits=[
                    ChiefSectionTextEdit(
                        target_section_id=section_id,
                        old_text=payload["section_bodies"][section_id],
                        new_text=(
                            payload["section_bodies"][section_id]
                            + "\n\nRevised section body with verification detail."
                        ),
                    )
                    for section_id in section_ids
                ],
                revision_responses=[
                    RevisionResponse(
                        finding_id=item["id"],
                        action="implemented",
                        summary="The requested chapter-local change was implemented.",
                        changed_target_ids=list(item["target_section_ids"]),
                    )
                    for item in payload["assigned_findings"]
                ],
            )
        # Final recheck only receives the two affected chapter lanes.
        finding_ids = [finding_item["id"] for finding_item in payload["required_findings"]]
        return FinalChapterLaneVerdictSubmission(
            run_id=state["run_id"],
            chapter_id=chapter_id,
            checked_section_ids=section_ids,
            verdicts=[
                ResolutionVerdict(
                    finding_id=finding_id,
                    verdict="resolved",
                    reason="The revised lane now includes the requested verification detail.",
                    evidence_refs=[state["chief_candidate_ref"]],
                )
                for finding_id in finding_ids
            ],
        )

    runner._agent = MethodType(fake_agent, runner)
    asyncio.run(runner._run_final_chapter_lanes(state, "workflow"))

    assert {task for task, _session, _skill in calls if task.startswith("chief-chapter-")} == {
        "chief-chapter-1-r1",
        "chief-chapter-4-r1",
    }
    assert {task for task, _session, _skill in calls if task.startswith("final-chapter-")} == {
        "final-chapter-1-r0",
        "final-chapter-3-r0",
        "final-chapter-4-r0",
        "final-chapter-1-r1",
        "final-chapter-4-r1",
    }
    assert {session for task, session, _skill in calls if task.startswith("chief-chapter-")} == {
        "chief-chapter-1",
        "chief-chapter-4",
    }
    assert {session for task, session, _skill in calls if task.endswith("-r1")} == {
        "chief-chapter-1",
        "chief-chapter-4",
        "final-chapter-1",
        "final-chapter-4",
    }
    assert {
        skill for task, _session, skill in calls if task.startswith("chief-chapter-")
    } == {None}
    chief_inputs = {
        task: _read_json(runner.service, f"Work/runs/{state['run_id']}/context/{task}-input-r1.json")
        for task in ("chief-chapter-1", "chief-chapter-4")
    }
    assert chief_inputs["chief-chapter-1"]["section_ids"] == ["1.1"]
    assert chief_inputs["chief-chapter-4"]["section_ids"] == ["4.1"]
    assert all(
        "skill:final-auditor" in skill
        for task, _session, skill in calls
        if task.startswith("final-chapter-")
    )
    assert "final" in state["aggregate_refs"]


def test_final_recheck_new_finding_runs_second_affected_wave(tmp_path: Path) -> None:
    runner, state = _final_state(tmp_path, "run-final-regression")
    calls: list[tuple[str, str | None]] = []
    recheck_rounds: dict[str, int] = {}

    def finding(section_id: str, finding_id: str) -> ChapterScopedFinalReviewFinding:
        return ChapterScopedFinalReviewFinding(
            id=finding_id,
            target_section_ids=[section_id],
            target_changes=[
                ChapterScopedFinalReviewTargetChange(
                    target_section_id=section_id,
                    required_change="Add the missing explanation and verification detail.",
                    reviewer_checks=["The revised section states the check and outcome."],
                )
            ],
            category="regression",
            impact="blocking",
            observation="The section does not explain the required verification detail.",
            evidence_refs=[state["chief_candidate_ref"]],
        )

    async def fake_agent(self, _agent_id, envelope, _artifacts, _workflow_id, *, session_key=None):
        calls.append((envelope.task_id, session_key))
        payload = _read_json(self.service, envelope.input_contract_ref)
        chapter_id = payload["chapter_id"]
        section_ids = list(payload["section_ids"])
        phase = payload["phase"]
        if phase == "initial":
            initial_findings = [finding("1.1", "F-1")] if chapter_id == "1" else []
            return FinalChapterLaneFindingSubmission(
                run_id=state["run_id"],
                chapter_id=chapter_id,
                checked_section_ids=section_ids,
                findings=initial_findings,
            )
        if envelope.input_contract_kind == "chief_chapter_lane_input":
            revision = int(payload["revision"])
            return ChiefChapterLaneRevisionSubmission(
                run_id=state["run_id"],
                base_subject_ref=payload["subject_ref"],
                chapter_id=chapter_id,
                revision=revision,
                section_ids=section_ids,
                edits=[
                    ChiefSectionTextEdit(
                        target_section_id=section_id,
                        old_text=payload["section_bodies"][section_id],
                        new_text=(
                            payload["section_bodies"][section_id]
                            + f"\n\nRevised section body {section_id} with verification detail."
                        ),
                    )
                    for section_id in section_ids
                ],
                revision_responses=[
                    RevisionResponse(
                        finding_id=item["id"],
                        action="implemented",
                        summary="The requested chapter-local change was implemented.",
                        changed_target_ids=list(item["target_section_ids"]),
                    )
                    for item in payload["assigned_findings"]
                ],
            )

        revision = int(payload["revision"])
        recheck_rounds[chapter_id] = recheck_rounds.get(chapter_id, 0) + 1
        required_ids = [item["id"] for item in payload["required_findings"]]
        new_findings = (
            [finding("1.1", "F-1-new")]
            if chapter_id == "1" and recheck_rounds[chapter_id] == 1
            else []
        )
        return FinalChapterLaneVerdictSubmission(
            run_id=state["run_id"],
            chapter_id=chapter_id,
            checked_section_ids=section_ids,
            verdicts=[
                ResolutionVerdict(
                    finding_id=finding_id,
                    verdict="resolved",
                    reason="The revised lane now includes the requested verification detail.",
                    evidence_refs=[state["chief_candidate_ref"]],
                )
                for finding_id in required_ids
            ],
            new_findings=new_findings,
        )

    runner._agent = MethodType(fake_agent, runner)
    asyncio.run(runner._run_final_chapter_lanes(state, "workflow"))

    assert "chief-chapter-1-r1" in {task for task, _session in calls}
    assert "final-chapter-1-r1" in {task for task, _session in calls}
    assert "chief-chapter-1-r2" in {task for task, _session in calls}
    assert "final-chapter-1-r2" in {task for task, _session in calls}
    assert "chief-chapter-3-r1" not in {task for task, _session in calls}
    assert "chief-chapter-4-r2" not in {task for task, _session in calls}
    assert {session for task, session in calls if task.endswith("-r2")} == {
        "chief-chapter-1",
        "final-chapter-1",
    }
    first_recheck = _read_json(
        runner.service,
        f"Work/runs/{state['run_id']}/context/final-chapter-1-input-r1.json",
    )
    assert set(first_recheck["section_bodies"]) == {"1.1"}
    assert set(first_recheck["unchanged_section_sha256"]) == {"1.2", "1.3"}
    assert all(
        len(digest) == 64
        for digest in first_recheck["unchanged_section_sha256"].values()
    )
    completion, artifacts = runner._load_current_review_completion(
        run_id=state["run_id"],
        completion_ref=state["final_review_completion_ref"],
        lifecycle="final",
        reviewer_agent_id="chief-editor-auditor",
        reviewer_session_key="final-chapter-wave",
    )
    assert completion.resolved_finding_ids == ["F-1", "F-1-new"]
    assert artifacts


def test_final_resume_partial_recheck_skips_initial_and_chief_provider_calls(tmp_path: Path) -> None:
    runner, state = _final_state(tmp_path, "run-final-resume")
    run_id = state["run_id"]
    plan = state["special_topic_plan"]
    recovery = RecoveryStateStore(tmp_path, run_id)
    calls: list[str] = []

    def finding(section_id: str, finding_id: str) -> ChapterScopedFinalReviewFinding:
        return ChapterScopedFinalReviewFinding(
            id=finding_id,
            target_section_ids=[section_id],
            target_changes=[
                ChapterScopedFinalReviewTargetChange(
                    target_section_id=section_id,
                    required_change="Add the missing explanation and verification detail.",
                    reviewer_checks=["The revised section states the check and outcome."],
                )
            ],
            category="completeness",
            impact="blocking",
            observation="The section does not explain the required verification detail.",
            evidence_refs=[state["chief_candidate_ref"]],
        )

    def record(stage: str, lane_id: str, result_ref: str, revision: int) -> None:
        recovery.record_lane_attempt(
            {
                "run_id": run_id,
                "stage": stage,
                "lane_id": lane_id,
                "task_id": lane_id,
                "attempt": 1,
                "revision": revision,
                "status": "completed",
                "result_ref": result_ref,
            }
        )

    # Persist all initial findings (F-1/F-4), so a resumed run can rehydrate
    # them without dispatching any Final initial lanes.
    initial_findings = {"1": [finding("1.1", "F-1")], "3": [], "4": [finding("4.1", "F-4")]}
    section_ids = {"1": ["1.1", "1.2", "1.3"], "3": ["3.1.1", "3.1.2", "3.1.3", "3.2"], "4": ["4.1", "4.2"]}
    for chapter_id, findings in initial_findings.items():
        ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
        payload = FinalChapterLaneFindingSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            checked_section_ids=section_ids[chapter_id],
            findings=findings,
        )
        runner.service.store.write_json(ref, payload.model_dump(mode="json"))
        initial_contract = FinalChapterLaneInput(
            phase="initial",
            run_id=run_id,
            subject_ref=state["chief_candidate_ref"],
            chapter_id=chapter_id,
            review_focus=list(final_lane_specialization(chapter_id).review_focus),
            section_ids=section_ids[chapter_id],
            section_bodies=runner._final_chapter_section_bodies(
                state["edited_report"], chapter_id
            ),
            special_topic_plan=plan,
            revision=0,
        )
        runner.service.store.write_json(
            f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r0.json",
            initial_contract.model_dump(mode="json"),
        )
        record("final-initial", chapter_id, ref, 0)

    # Persist both Chief revision lanes and their aggregate.  The resumed run
    # must consume these refs and dispatch only the missing Final recheck lane.
    chief_revision_ref = f"Work/runs/{run_id}/edited-revisions/chief-r1.json"
    runner.service.store.write_json(chief_revision_ref, state["edited_report"].model_dump(mode="json"))
    for chapter_id in ("1", "4"):
        target_ids = [
            section_id
            for finding_item in initial_findings[chapter_id]
            for section_id in finding_item.target_section_ids
        ]
        current_bodies = runner._final_chapter_section_bodies(
            state["edited_report"], chapter_id
        )
        payload = ChiefChapterLaneRevisionSubmission(
            run_id=run_id,
            base_subject_ref=state["chief_candidate_ref"],
            chapter_id=chapter_id,
            revision=1,
            section_ids=target_ids,
            edits=[
                ChiefSectionTextEdit(
                    target_section_id=section_id,
                    old_text=current_bodies[section_id],
                    new_text=(
                        current_bodies[section_id]
                        + f"\n\nRevised body {section_id} with verification detail."
                    ),
                )
                for section_id in target_ids
            ],
            revision_responses=[
                RevisionResponse(
                    finding_id=item.id,
                    action="implemented",
                    summary="The requested chapter-local change was implemented.",
                    changed_target_ids=list(item.target_section_ids),
                )
                for item in initial_findings[chapter_id]
            ],
        )
        ref = (
            f"Work/runs/{run_id}/reviews/"
            f"chief-chapter-lane-{chapter_id}-r1.json"
        )
        runner.service.store.write_json(ref, payload.model_dump(mode="json"))
        record("chief-revision-r1", chapter_id, ref, 1)
    chief_projection = f"Work/runs/{run_id}/reviews/resume-chief-revision-aggregate.json"
    runner.service.store.write_json(chief_projection, {"run_id": run_id, "stage": "chief-revision-r1", "status": "completed"})
    recovery.record_aggregate(
        AggregateState(
            run_id=run_id,
            stage="chief-revision-r1",
            lane_ids=["1", "4"],
            result_ref=chief_revision_ref,
            revision=1,
            status="completed",
        )
    )

    # Bind the recovered Chapter 1 verdict to the exact r1 subject and Chief
    # response that the recovered chapter lanes deterministically reduce.
    revised = state["edited_report"].model_copy(
        update={
            "assessment_background": (
                "background body\n\nRevised body 1.1 with verification detail."
            ),
            "special_topic_analysis": (
                "### 4.1 Topic A\nTopic A body with enough substantive detail.\n\n"
                "Revised body 4.1 with verification detail.\n\n"
                "### 4.2 Topic B\nTopic B body with enough substantive detail."
            ),
        }
    )
    chapter_one_bodies = runner._final_chapter_section_bodies(revised, "1")
    chapter_one_response = RevisionResponse(
        finding_id="F-1",
        action="implemented",
        summary="The requested chapter-local change was implemented.",
        changed_target_ids=["1.1"],
    )
    recheck_contract = FinalChapterLaneInput(
        phase="recheck",
        run_id=run_id,
        subject_ref=chief_revision_ref,
        chapter_id="1",
        review_focus=list(final_lane_specialization("1").review_focus),
        section_ids=section_ids["1"],
        section_bodies={"1.1": chapter_one_bodies["1.1"]},
        unchanged_section_sha256={
            section_id: hashlib.sha256(body.encode("utf-8")).hexdigest()
            for section_id, body in chapter_one_bodies.items()
            if section_id != "1.1"
        },
        required_findings=initial_findings["1"],
        revision_responses=[chapter_one_response],
        special_topic_plan=plan,
        revision=1,
    )
    runner.service.store.write_json(
        f"Work/runs/{run_id}/context/final-chapter-1-input-r1.json",
        recheck_contract.model_dump(mode="json"),
    )

    # Only Chapter 1's Final recheck was persisted before the crash.
    verdict_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-1-r1.json"
    verdict = FinalChapterLaneVerdictSubmission(
        run_id=run_id,
        chapter_id="1",
        checked_section_ids=section_ids["1"],
        verdicts=[
            ResolutionVerdict(
                finding_id="F-1",
                verdict="resolved",
                reason="The revised lane now includes the requested verification detail.",
                evidence_refs=[chief_revision_ref],
            )
        ],
    )
    runner.service.store.write_json(verdict_ref, verdict.model_dump(mode="json"))
    record("final-recheck-r1", "1", verdict_ref, 1)

    async def fake_agent(self, _agent_id, envelope, _artifacts, _workflow_id, *, session_key=None):
        calls.append(envelope.task_id)
        payload = _read_json(self.service, envelope.input_contract_ref)
        assert payload["phase"] == "recheck"
        assert payload["chapter_id"] == "4"
        return FinalChapterLaneVerdictSubmission(
            run_id=run_id,
            chapter_id="4",
            checked_section_ids=section_ids["4"],
            verdicts=[
                ResolutionVerdict(
                    finding_id="F-4",
                    verdict="resolved",
                    reason="The revised lane now includes the requested verification detail.",
                    evidence_refs=[chief_revision_ref],
                )
            ],
        )

    runner._agent = MethodType(fake_agent, runner)
    asyncio.run(runner._run_final_chapter_lanes(state, "workflow"))

    assert calls == ["final-chapter-4-r1"]
