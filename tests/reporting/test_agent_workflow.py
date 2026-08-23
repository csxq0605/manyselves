from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    report_taxonomy_snapshot,
    reset_report_taxonomy,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CROSS_REVIEW_DIMENSIONS,
    FINAL_AUDIT_SECTION_IDS,
    FINAL_REPORT_SECTION_IDS,
    AgentResult,
    ChiefRevisionSubmission,
    ClaimRecord,
    CrossReviewCoverageEntry,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    EditedReportSubmission,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    TableSubmission,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefEditorInput,
    CrossReviewInput,
    FinalReviewInput,
    RequestedModuleChange,
    ReviewCompletionRecord,
    ValidationReport,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CoverageMatrix,
    ProjectManifest,
    ReportRequest,
    RevisionRequest,
    SpecialTopicPlan,
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    ArtifactRef,
    CrossOwnerCompletion,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting import review_lifecycle as review_lifecycle_module
from manyselves.core.reporting.assets import validate_final_report_markdown
from manyselves.core.reporting.prompts import PromptAssembler
from manyselves.core.reporting.review_lifecycle import (
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleRecheckAcceptance,
    ModuleRecheckPreparation,
    ModuleRevisionPreparation,
    ReviewLifecycleError,
    _apply_chief_patch,
    _require_validation_binding,
    accept_module_initial_review,
    accept_module_initial_review_preflight_revision,
    accept_module_recheck,
    accept_module_recheck_preflight_revision,
    accept_module_revision,
    prepare_module_initial_review,
    prepare_module_initial_review_step,
    prepare_module_recheck,
    prepare_module_revision,
    request_module_revision,
    resume_module_initial_review,
    resume_module_recheck,
    run_cross_review,
    run_final_review,
    run_module_review,
)
from manyselves.core.reporting.versions import ReportVersion
from manyselves.core.reporting.workflow import (
    AgentWorkflowError,
    FullReportCheckpoint,
    ReportWorkflowRunner,
)
from manyselves.core.tools.reporting_collaboration_tools import SubmitResultTool
from manyselves.core.tools.task_board import TaskBoard


def _artifact_hashes(root: Path, refs: list[str]) -> dict[str, str]:
    return {ref: hashlib.sha256((root / ref).read_bytes()).hexdigest() for ref in refs}


@pytest.mark.asyncio
async def test_workflow_task_board_uses_visible_lane_identity(tmp_path: Path) -> None:
    board = TaskBoard()
    service = SimpleNamespace(task_board=board)
    runner = ReportWorkflowRunner.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._budget = None
    runner._raise_if_cancel_requested = lambda _run_id: None
    runner.agents = {"cross-module-reviewer": SimpleNamespace()}
    payload = CrossReviewFindingSubmission(
        coverage=[
            CrossReviewCoverageEntry(
                module_id=module_id,
                checked_dimensions=["terminology"],
            )
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        ],
        findings=[],
        synthesis_inputs=[],
    )

    class FakeAgentRunner:
        async def run(self, *_args, **_kwargs):
            return AgentResult(
                task_id="cross-owner-2.3-r0-initial",
                run_id="run-visible-lane",
                agent_id="cross-module-reviewer",
                session_id="session-visible-lane",
                status="completed",
                payload=payload,
            )

    runner.agent_runner = FakeAgentRunner()
    envelope = TaskEnvelope(
        task_id="cross-owner-2.3-r0-initial",
        run_id="run-visible-lane",
        agent_id="cross-module-reviewer",
        objective="检查 2.3 owner 的跨模块关系",
        allowed_outputs=["cross_review_finding_submission"],
    )

    await runner._agent(
        "cross-module-reviewer",
        envelope,
        [],
        "workflow-visible-lane",
        session_key="cross-owner-2.3",
    )

    tasks = board.get_all()
    assert len(tasks) == 1
    assert tasks[0].target_agent == "cross-owner-2.3"
    assert tasks[0].status.value == "completed"


def _special_topic_plan() -> SpecialTopicPlan:
    return SpecialTopicPlan(
        source_ref="Inputs/专项问题分析.md",
        source_sha256="0" * 64,
        sections=[
            {
                "section_id": "4.1",
                "title": "动态专项问题",
                "requirement": "分析项目边界、方案条件和验证方法。",
            }
        ],
    )


def _module(module_id: str, revision: int = 0) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: (
                f"### {submodule_id}\n\n当前模块正文说明现状、判断、风险机理、建议责任和验收方法。"
            )
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
    )


def _cross_synthesis_inputs() -> list[dict]:
    shared = {
        "related_module_ids": ["2.1", "2.3"],
        "root_causes": ["2.1 供电架构约束与 2.3 保护信息不完整共同削弱屏障"],
        "propagation_steps": [
            "2.1 架构约束提高单点故障影响范围",
            "2.3 保护不确定性延长故障识别与隔离时间",
        ],
        "causal_chain": (
            "2.1 的供电架构约束与 2.3 的保护信息不完整叠加，"
            "导致故障影响范围扩大并延长隔离恢复时间。"
        ),
        "decision_implication": "整改必须先确认保护边界，再安排架构切换和联合停送电验证。",
        "action_dependencies": ["先完成 2.3 保护核查，再实施 2.1 架构切换"],
        "joint_actions": ["由供配电与保护责任人联合完成方案、操作和验收"],
        "verification_method": "通过联合模拟、保护动作记录和恢复时间指标完成闭环验证。",
        "acceptance_criteria": ["保护动作顺序正确且恢复时间满足批准目标"],
        "module_statement_refs": ["2.1.1", "2.3.1"],
        "confidence_and_boundary": "当前关系由已批准模块判断支持，具体动作时限仍以现场复核为准。",
        "target_report_section_ids": ["3.1.1", "3.1.2", "3.2"],
        "evidence_refs": [
            "E-0001",
            "Work/runs/run-x/modules/2.1-r0.json",
            "Work/runs/run-x/modules/2.3-r0.json",
        ],
    }
    return [
        {
            "id": "SI-RISK",
            "cluster_type": "risk_cluster",
            **shared,
        },
        {
            "id": "SI-GLOBAL",
            "cluster_type": "global_propagation",
            **shared,
        },
    ]


def _edited(
    module_text: dict[str, str],
    *,
    responses: list | None = None,
    include_special_topics: bool = True,
) -> EditedReportSubmission:
    synthesis = (
        "综合当前证据，明确责任、优先顺序、依赖关系、风险影响、验证指标、验收方式和剩余边界。" * 24
    )
    return EditedReportSubmission(
        title="示例配电安全专家咨询报告",
        assessment_background=synthesis,
        findings_overview=synthesis,
        regional_executive_summary=synthesis,
        module_narratives=module_text,
        risk_panorama=synthesis,
        dimension_risk_analysis=(
            synthesis + "系统架构、电能质量、保护、设备和运维之间存在共同、叠加、依赖和传播关系。"
        ),
        data_gap_analysis=synthesis + "数据不足会限制判断并影响置信度，应优先补证。",
        improvement_action_plan=(
            synthesis + "责任部门牵头，按依赖优先实施，以指标、复测和验收关闭。"
        ),
        special_topic_plan=(_special_topic_plan() if include_special_topics else None),
        special_topic_analysis=(
            (
                "### 4.1 动态专项问题\n\n"
                + synthesis
                + "结合项目边界比较方案条件，并明确验证方法和知识适用限制。"
            )
            if include_special_topics
            else None
        ),
        protected_claim_ids=[],
        tables=[],
        photo_ids=[],
        unresolved_editorial_issues=[],
        revision_responses=responses or [],
    )


class _FakeService:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.store = ReportingStore(workspace)
        self.notices: list[str] = []

    async def _notice(self, message: str) -> None:
        self.notices.append(message)


class _ScriptedRunner:
    def __init__(self, workspace: Path, scripted: list[tuple[str, str, object]]):
        self.service = _FakeService(workspace)
        self.scripted = list(scripted)
        self.calls: list[tuple[str, str, str | None]] = []
        self.envelopes: list[TaskEnvelope] = []
        self._parallel_final_seen: set[str] = set()
        self._parallel_cross_seen: set[str] = set()

    async def _agent(
        self,
        agent_id,
        envelope,
        artifacts,
        workflow_id,
        *,
        session_key=None,
    ):
        expected_agent, expected_output, result = self.scripted[0]
        assert agent_id == expected_agent
        assert envelope.allowed_outputs == [expected_output]
        self.calls.append((agent_id, expected_output, session_key))
        self.envelopes.append(envelope)
        if (
            envelope.input_contract_kind == "cross_review_input"
            and "cross-review-2." in envelope.task_id
        and isinstance(
            result,
            (CrossReviewFindingSubmission, CrossReviewVerdictSubmission),
        )
        ):
            contract = CrossReviewInput.model_validate_json(
                (self.service.workspace / envelope.input_contract_ref).read_text(
                    encoding="utf-8"
                )
            )
            owner = contract.owner_module_id
            if isinstance(result, CrossReviewFindingSubmission):
                lane_result = CrossReviewFindingSubmission(
                    coverage=result.coverage,
                    findings=[
                        finding
                        for finding in result.findings
                        if finding.owner_module_id == owner
                    ],
                    synthesis_inputs=[
                        item
                        for item in result.synthesis_inputs
                        if min(item.related_module_ids) == owner
                    ],
                )
                expected_lanes = set(REPORT_TAXONOMY)
            else:
                required_ids = {finding.id for finding in contract.required_findings}
                lane_result = CrossReviewVerdictSubmission(
                    coverage=result.coverage,
                    verdicts=[
                        verdict
                        for verdict in result.verdicts
                        if verdict.finding_id in required_ids
                    ],
                    new_findings=[
                        finding
                        for finding in result.new_findings
                        if finding.owner_module_id == owner
                    ],
                    synthesis_inputs=[
                        item
                        for item in result.synthesis_inputs
                        if min(item.related_module_ids) == owner
                    ],
                )
                expected_lanes = {
                    finding.owner_module_id for finding in result.new_findings
                } | {
                    finding.owner_module_id
                    for finding in contract.required_findings
                }
            self._parallel_cross_seen.add(owner)
            if self._parallel_cross_seen == expected_lanes:
                self.scripted.pop(0)
                self._parallel_cross_seen.clear()
            return lane_result
        if (
            envelope.input_contract_kind == "final_review_input"
            and "final-review-chapter-" in envelope.task_id
            and isinstance(
                result,
                (FinalReviewFindingSubmission, FinalReviewVerdictSubmission),
            )
        ):
            contract = FinalReviewInput.model_validate_json(
                (self.service.workspace / envelope.input_contract_ref).read_text(
                    encoding="utf-8"
                )
            )
            chapter_id = contract.chapter_id
            assert chapter_id is not None
            owned = set(contract.required_section_ids)
            expected_chapters = {
                section_id.split(".", 1)[0] for section_id in result.checked_section_ids
            }
            if isinstance(result, FinalReviewFindingSubmission):
                selected = [
                    finding
                    for finding in result.findings
                    if set(finding.target_section_ids).issubset(owned)
                ]
                assigned = {
                    finding.id
                    for candidate in expected_chapters
                    for finding in result.findings
                    if all(
                        target.split(".", 1)[0] == candidate
                        for target in finding.target_section_ids
                    )
                }
                if chapter_id == "1":
                    selected.extend(
                        finding for finding in result.findings if finding.id not in assigned
                    )
                lane_result = FinalReviewFindingSubmission(
                    checked_section_ids=contract.required_section_ids,
                    findings=selected,
                    residual_risks=result.residual_risks if chapter_id == "1" else [],
                )
            else:
                required_ids = {finding.id for finding in contract.required_findings}
                lane_result = FinalReviewVerdictSubmission(
                    checked_section_ids=contract.required_section_ids,
                    verdicts=[
                        verdict for verdict in result.verdicts
                        if verdict.finding_id in required_ids
                    ],
                    new_findings=[
                        finding for finding in result.new_findings
                        if set(finding.target_section_ids).issubset(owned)
                    ],
                    residual_risks=result.residual_risks if chapter_id == "1" else [],
                )
            self._parallel_final_seen.add(chapter_id)
            if self._parallel_final_seen == expected_chapters:
                self.scripted.pop(0)
                self._parallel_final_seen.clear()
            return lane_result
        self.scripted.pop(0)
        return result

    def _validate_module_structure(self, state, module, phase):
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module.module_id}-r{module.revision}.json"
        )
        ref = (
            f"Work/runs/{state['run_id']}/reviews/"
            f"module-quality-{module.module_id}-r{module.revision}-{phase}.json"
        )
        self.service.store.write_json(
            ref,
            ValidationReport(
                validation_protocol_version=2,
                run_id=state["run_id"],
                subject_ref=subject_ref,
                subject_revision=module.revision,
                validator="test-module-structure/v2",
                check_ids=["module.canonical_markdown"],
                passed=True,
            ).model_dump(mode="json"),
        )
        return ref

    @staticmethod
    def _user_supplement_constraints(state, **kwargs):
        return []

    @staticmethod
    def _context_refs(state, agent_id):
        return []

    @staticmethod
    def _template_skill_context(state, *parts):
        return ""

    @staticmethod
    def _chief_template_skill_context(state, chapter_ids):
        return ""

    @staticmethod
    def _canonical_markdown(edited):
        return ReportWorkflowRunner._canonical_markdown(edited)

    def _delivery_projection(self, state, edited, claims=None):
        if claims == []:
            return None, ReportWorkflowRunner._canonical_markdown(edited)
        return ReportWorkflowRunner._delivery_projection(self, state, edited, claims)

    def _validate_final_report_structure(self, state, markdown, phase):
        subject_ref = f"Work/runs/{state['run_id']}/validation/report-{phase}.md"
        self.service.store.write_text(subject_ref, markdown)
        revision_text = phase.removeprefix("chief-candidate-r")
        subject_revision = (
            int(revision_text)
            if phase.startswith("chief-candidate-r") and revision_text.isdigit()
            else None
        )
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/reviews/report-integrity-{phase}.json",
            ValidationReport(
                validation_protocol_version=2,
                run_id=state["run_id"],
                subject_ref=subject_ref,
                subject_revision=subject_revision,
                validator="test-final-report-structure/v2",
                check_ids=["final_report.fixed_sections_and_markdown"],
                passed=True,
            ).model_dump(mode="json"),
        )

    @staticmethod
    def _final_revision_diff(previous, revised):
        return ReportWorkflowRunner._final_revision_diff(previous, revised)


class _ToolBackedModuleReviewRunner(_ScriptedRunner):
    """Exercise the real submit-result normalization inside the lifecycle loop."""

    def __init__(self, workspace: Path, patch: ModuleRevisionSubmission):
        super().__init__(workspace, [])
        self.patch = patch
        self.bus = MessageBus()

    async def _agent(
        self,
        agent_id,
        envelope,
        artifacts,
        workflow_id,
        *,
        session_key=None,
    ):
        self.calls.append((agent_id, envelope.allowed_outputs[0], session_key))
        self.envelopes.append(envelope)
        if agent_id == "module-2.1-specialist":
            return self.patch

        assert agent_id == "evidence-auditor"
        assert envelope.input_contract_ref is not None
        if envelope.allowed_outputs == ["module_review_finding_submission"]:
            payload = {
                "kind": "module_review_finding_submission",
                "findings": [
                    {
                        "target_submodule_id": envelope.target_submodule_ids[0],
                        "category": "analysis_depth",
                        "impact": "advisory",
                        "observation": "当前建议缺少责任接口和可由原审查者复核的验收方法。",
                        "evidence_refs": [envelope.input_contract_ref],
                        "required_change": "在目标小节补充责任接口、执行动作和可验证验收方法。",
                        "reviewer_checks": ["责任、动作和验收方法已经形成闭环"],
                    }
                ],
            }
        else:
            payload = {
                "kind": "module_review_verdict_submission",
                "verdicts": [
                    {
                        "verdict": "resolved",
                        "reason": "当前修订已经补充责任、执行动作和验收方法，可以关闭。",
                        "evidence_refs": [envelope.input_contract_ref],
                    }
                ],
                "new_findings": [],
            }
        tool = SubmitResultTool(
            agent_id,
            session_key or "session",
            envelope.run_id,
            envelope.task_id,
            self.service.store,
            self.bus,
            workflow_id,
            allowed_outputs=envelope.allowed_outputs,
            revision=envelope.revision,
            input_contract_kind=envelope.input_contract_kind,
            input_contract_ref=envelope.input_contract_ref,
        )
        outcome = await tool(**payload)
        assert outcome["status"] == "completed", outcome
        result = AgentResult.model_validate_json(
            (self.service.workspace / outcome["result_path"]).read_text(encoding="utf-8")
        )
        assert result.payload is not None
        return result.payload


@pytest.mark.asyncio
async def test_initial_module_review_boundary_is_typed_and_resume_aware(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    runner = _ScriptedRunner(tmp_path, [])
    state = {"run_id": "run-initial-review-boundary"}

    preparation = await prepare_module_initial_review(
        runner,
        module_id="2.1",
        payload=module,
        state=state,
        workflow_id="workflow-initial-review-boundary",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert isinstance(preparation, ModuleInitialReviewPreparation)
    assert preparation.mode == "invoke_agent"
    assert preparation.reviewer_session_key == "module-auditor-2.1"
    assert preparation.envelope is not None
    assert preparation.envelope.task_id == "module-2.1-initial-review-r0"
    assert preparation.review_input is not None
    assert preparation.review_input.required_submodule_ids == [target]

    accepted = accept_module_initial_review(
        runner,
        preparation=preparation,
        result=ModuleReviewFindingSubmission(
            coverage={"submodule_ids": [target]},
            findings=[],
        ),
        state=state,
    )
    assert accepted.next_action == "completed"
    assert accepted.completion_ref is not None

    resumed = await prepare_module_initial_review(
        runner,
        module_id="2.1",
        payload=module,
        state={**state, "resume": True},
        workflow_id="workflow-initial-review-boundary",
        initial_scope={target},
        lifecycle_id="initial",
    )
    assert resumed.mode == "continue_existing"
    assert resumed.progress is not None
    assert resumed.progress.next_action == "completed"
    resumed_acceptance = resume_module_initial_review(
        preparation=resumed,
        state=state,
    )
    assert resumed_acceptance.next_action == "completed"
    assert resumed_acceptance.current == module
    assert resumed_acceptance.completion_ref == accepted.completion_ref


def test_module_dispatch_uses_configured_global_knowledge_root(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    global_root.mkdir()
    content = "2.1.1 shared global protection rule"
    (global_root / "shared.md").write_text(content, encoding="utf-8")
    service = _FakeService(tmp_path)
    service.global_root = global_root
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._load_template_skill = lambda state: True
    runner._role_skill_context = lambda state, role: "template guidance"
    state = {
        "run_id": "run-global-dispatch",
        "request": SimpleNamespace(
            instruction="analyse module",
            missing_evidence_policy="draft",
            execution_requirements=[],
            user_supplements=[],
        ),
        "preparation_refs": {
            "coverage": "Work/coverage.json",
            "evidence": "Work/evidence.jsonl",
            "manifest": "Work/manifest.json",
        },
    }

    dispatch = runner._build_module_dispatch(state, ("2.1",))
    manifest = json.loads(
        (
            tmp_path
            / "Work/runs/run-global-dispatch/context-manifests/knowledge-sources.json"
        ).read_text(encoding="utf-8")
    )

    assert "shared global protection rule" in dispatch.module_tasks[0].inline_context
    assert manifest["sources"] == [
        {
            "logicalPath": "GlobalKnowledge/shared.md",
            "namespace": "global",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
    ]


def test_preparation_resume_uses_hash_verified_run_snapshot(tmp_path: Path) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    state = {
        "run_id": "run-preparation",
        "project_manifest": ProjectManifest(files=[]),
        "evidence_items": [],
        "photo_assets": [],
        "mapping_gaps": [],
        "coverage_matrix": CoverageMatrix(entries={}),
        "report_taxonomy": report_taxonomy_snapshot(
            REPORT_TAXONOMY,
            source_ref="Inputs/S4-6测试目录.xlsx",
            source_sha256="0" * 64,
            sheet="评估信息汇总表",
        ),
        "special_topic_plan": _special_topic_plan(),
    }
    runner._persist_preparation_snapshot(state)
    service.store.write_json(
        "Work/runs/run-preparation/workflow-state.json",
        FullReportCheckpoint(
            workflow_id="full-power-distribution-report:run-preparation",
            run_id="run-preparation",
            activity="preparation",
            status="completed",
            preparation_refs=state["preparation_refs"],
            preparation_sha256=state["preparation_sha256"],
        ).model_dump(mode="json"),
    )

    restored = {"run_id": "run-preparation", "resume": True}
    taxonomy_token = runner._restore_preparation_snapshot(restored)
    try:
        assert restored["project_manifest"].files == []
        assert "_report_taxonomy_token" not in restored
        assert deepcopy(restored)["report_taxonomy"] == restored["report_taxonomy"]
    finally:
        reset_report_taxonomy(taxonomy_token)

    (tmp_path / state["preparation_refs"]["coverage"]).write_text(
        '{"entries": {"tampered": {}}}', encoding="utf-8"
    )
    with pytest.raises(Exception, match="hash mismatch"):
        runner._restore_preparation_snapshot(restored)


@pytest.mark.parametrize("failure_point", ["after_evidence", "before_publish"])
def test_preparation_staging_failure_never_exposes_partial_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    state = {
        "run_id": "run-preparation-fault",
        "project_manifest": ProjectManifest(files=[]),
        "evidence_items": [],
        "photo_assets": [],
        "mapping_gaps": [],
        "coverage_matrix": CoverageMatrix(entries={}),
        "report_taxonomy": report_taxonomy_snapshot(
            REPORT_TAXONOMY,
            source_ref="Inputs/S4-6测试目录.xlsx",
            source_sha256="0" * 64,
            sheet="评估信息汇总表",
        ),
    }
    if failure_point == "after_evidence":
        original_write_json = service.store.write_json

        def fail_photo_staging(ref, payload):
            if str(ref).endswith("photo-manifest.json"):
                raise RuntimeError("fault after evidence staging")
            return original_write_json(ref, payload)

        monkeypatch.setattr(service.store, "write_json", fail_photo_staging)
    else:
        monkeypatch.setattr(
            "manyselves.core.reporting.workflow.os.replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("fault before completion publish")
            ),
        )

    with pytest.raises(RuntimeError, match="fault"):
        runner._persist_preparation_snapshot(state)

    assert not (tmp_path / "Work/runs/run-preparation-fault/preparation").exists()
    assert not list(
        (tmp_path / "Work/runs/run-preparation-fault").glob(".preparation-*")
    )


@pytest.mark.asyncio
async def test_real_submit_result_ids_pass_module_review_lifecycle(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding_id = "M-2.1-initial-r0-001"
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={target: "### 修订\n\n已补充责任接口、执行动作和可验证验收方法。"},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": finding_id,
                "action": "implemented",
                "summary": "已在目标小节补充责任接口、执行动作和验收方法。",
                "changed_target_ids": [target],
            }
        ],
    )
    runner = _ToolBackedModuleReviewRunner(tmp_path, patch)

    result = await run_module_review(
        runner,
        "2.1",
        module,
        {"run_id": "run-tool-backed"},
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert result.revision == 1
    finding_result = AgentResult.model_validate_json(
        (
            tmp_path / "Work/runs/run-tool-backed/results/module-2.1-initial-review-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert finding_result.payload is not None
    assert finding_result.payload.findings[0].id == finding_id
    verdict_result = AgentResult.model_validate_json(
        (
            tmp_path / "Work/runs/run-tool-backed/results/module-2.1-initial-review-r1.json"
        ).read_text(encoding="utf-8")
    )
    assert verdict_result.payload is not None
    assert verdict_result.payload.verdicts[0].finding_id == finding_id


@pytest.mark.asyncio
async def test_module_review_requires_author_response_and_original_reviewer_verdict(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding = {
        "id": "M-2.1-initial-r0-001",
        "target_submodule_id": target,
        "category": "analysis_depth",
        "impact": "advisory",
        "observation": "当前建议没有明确责任接口和可以由原审查者复核的验收方式。",
        "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
        "required_change": "在目标小节补充责任接口、执行动作和可验证验收方法。",
        "reviewer_checks": ["核对责任、动作和验收是否形成闭环"],
    }
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={target: "### 修订\n\n已补充责任接口、执行动作和可验证验收方法。"},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": "M-2.1-initial-r0-001",
                "action": "implemented",
                "summary": "已在目标小节补充责任接口、执行动作和验收方法。",
                "changed_target_ids": [target],
            }
        ],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[finding],
                ),
            ),
            ("module-2.1-specialist", "module_revision_submission", patch),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [target]},
                    verdicts=[
                        {
                            "finding_id": "M-2.1-initial-r0-001",
                            "verdict": "resolved",
                            "reason": "当前修订已形成责任、动作和验收闭环，可以关闭。",
                            "evidence_refs": ["Work/runs/run-1/modules/2.1-r1.json"],
                        }
                    ],
                    new_findings=[],
                ),
            ),
        ],
    )
    state = {"run_id": "run-1"}
    result = await run_module_review(
        runner,
        "2.1",
        module,
        state,
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )
    assert result.revision == 1
    reviewer_calls = [call for call in runner.calls if call[0] == "evidence-auditor"]
    assert [call[2] for call in reviewer_calls] == [
        "module-auditor-2.1",
        "module-auditor-2.1",
    ]
    completion = ReviewCompletionRecord.model_validate_json(
        (tmp_path / state["module_review_completion_refs"]["2.1"]).read_text(encoding="utf-8")
    )
    assert completion.resolved_finding_ids == ["M-2.1-initial-r0-001"]


@pytest.mark.asyncio
async def test_module_revision_boundary_is_typed_and_accepts_one_patch(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    change = RequestedModuleChange(
        id="USER-2.1-R1",
        instruction="补充目标小节的责任、动作和验收闭环。",
        target_submodule_ids=[target],
    )
    runner = _ScriptedRunner(tmp_path, [])
    state = {"run_id": "run-module-revision-boundary"}

    preparation = await prepare_module_revision(
        runner,
        state=state,
        workflow_id="workflow-module-revision-boundary",
        subject=module,
        requested_changes=[change],
    )

    assert isinstance(preparation, ModuleRevisionPreparation)
    assert (
        ModuleRevisionPreparation.model_validate_json(preparation.model_dump_json())
        == preparation
    )
    assert preparation.specialist_id == "module-2.1-specialist"
    assert preparation.session_key == "module-2.1"
    assert preparation.revision == 1
    assert preparation.target_submodule_ids == [target]
    assert preparation.required_finding_ids == [change.id]
    assert preparation.envelope.task_id == "module-revision-r1-2.1"

    revised, subject_ref = accept_module_revision(
        runner,
        preparation=preparation,
        result=ModuleRevisionSubmission(
            module_id="2.1",
            base_revision=0,
            revision=1,
            submodule_narratives={
                target: f"### {target}\n\n已补充责任、动作和验收闭环。",
            },
            claims_upsert=[],
            claim_ids_remove=[],
            source_ids=[],
            unresolved_questions=[],
            revision_responses=[
                {
                    "finding_id": change.id,
                    "action": "implemented",
                    "summary": "目标小节已经按照指定要求完成责任、动作和验收闭环修订。",
                    "changed_target_ids": [target],
                }
            ],
        ),
    )

    assert revised.revision == 1
    assert subject_ref == "Work/runs/run-module-revision-boundary/modules/2.1-r1.json"
    assert (tmp_path / subject_ref).is_file()
    assert (
        tmp_path
        / "Work/runs/run-module-revision-boundary/reviews/module-revision-input-2.1-r1.json"
    ).is_file()
    assert (
        tmp_path
        / "Work/runs/run-module-revision-boundary/reviews/module-diff-2.1-r1.json"
    ).is_file()
    barrier = json.loads(
        (
            tmp_path
            / "Work/runs/run-module-revision-boundary/reviews/module-revisions/"
            "2.1/r1/module-barrier.json"
        ).read_text(encoding="utf-8")
    )
    assert barrier["subject_ref"] == subject_ref


@pytest.mark.asyncio
async def test_module_five_revision_returns_one_module_patch_for_all_targets(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    first, second = tuple(REPORT_TAXONOMY["2.1"].submodules)[:2]
    change = RequestedModuleChange(
        id="USER-2.1-R1",
        instruction="以完整模块作者身份同时更新两个明确小节。",
        target_submodule_ids=[first, second],
    )
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={
            first: f"### {first}\n\n{first} 已由模块作者完成修订。",
            second: f"### {second}\n\n{second} 已由模块作者完成修订。",
        },
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": change.id,
                "action": "implemented",
                "summary": "两个目标小节已在同一次模块级修订中完成，并保持其他小节内容不变。",
                "changed_target_ids": [first, second],
            }
        ],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [("module-2.1-specialist", "module_revision_submission", patch)],
    )
    state = {
        "run_id": "run-module-five-revision",
        "request": SimpleNamespace(user_supplements=[]),
    }

    revised, _ = await request_module_revision(
        runner,
        state=state,
        workflow_id="workflow-module-five-revision",
        subject=module,
        requested_changes=[change],
    )

    assert len(runner.calls) == 1
    assert runner.calls[0][2] == "module-2.1"
    assert runner.envelopes[0].task_id == "module-revision-r1-2.1"
    assert runner.envelopes[0].target_submodule_ids == [first, second]
    assert runner.envelopes[0].inline_context == ""
    assert revised.revision_responses[0].changed_target_ids == [first, second]
    barrier = json.loads(
        (
            tmp_path
            / "Work/runs/run-module-five-revision/reviews/module-revisions/"
            "2.1/r1/module-barrier.json"
        ).read_text(encoding="utf-8")
    )
    assert barrier["kind"] == "module_revision_barrier"
    assert barrier["target_submodule_ids"] == [first, second]


@pytest.mark.asyncio
async def test_module_five_review_closure_never_expands_author_or_auditor_by_leaf(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    first, second = tuple(REPORT_TAXONOMY["2.1"].submodules)[:2]
    first_id = "M-2.1-initial-r0-001"
    second_id = "M-2.1-initial-r0-002"
    findings = [
        {
            "id": first_id,
            "target_submodule_id": first,
            "category": "analysis_depth",
            "impact": "advisory",
            "observation": "第一个目标小节缺少明确责任、执行动作和可复核的验收闭环。",
            "evidence_refs": [f"Work/runs/run-module-five-review/modules/2.1-r0.json"],
            "required_change": "在第一个目标小节补充明确责任、执行动作和可复核验收方法。",
            "reviewer_checks": ["核对第一个小节的责任、动作和验收闭环"],
        },
        {
            "id": second_id,
            "target_submodule_id": second,
            "category": "analysis_depth",
            "impact": "advisory",
            "observation": "第二个目标小节缺少明确责任、执行动作和可复核的验收闭环。",
            "evidence_refs": [f"Work/runs/run-module-five-review/modules/2.1-r0.json"],
            "required_change": "在第二个目标小节补充明确责任、执行动作和可复核验收方法。",
            "reviewer_checks": ["核对第二个小节的责任、动作和验收闭环"],
        },
    ]
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={
            first: f"### {first}\n\n已补充责任、执行动作和验收闭环。",
            second: f"### {second}\n\n已补充责任、执行动作和验收闭环。",
        },
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": first_id,
                "action": "implemented",
                "summary": "第一个目标小节已补充责任、执行动作与可复核的验收闭环。",
                "changed_target_ids": [first],
            },
            {
                "finding_id": second_id,
                "action": "implemented",
                "summary": "第二个目标小节已补充责任、执行动作与可复核的验收闭环。",
                "changed_target_ids": [second],
            },
        ],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [first, second]},
                    findings=findings,
                ),
            ),
            ("module-2.1-specialist", "module_revision_submission", patch),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [first, second]},
                    verdicts=[
                        {
                            "finding_id": first_id,
                            "verdict": "resolved",
                            "reason": "第一个小节的责任、动作和验收闭环已经补齐。",
                            "evidence_refs": [
                                "Work/runs/run-module-five-review/modules/2.1-r1.json"
                            ],
                        },
                        {
                            "finding_id": second_id,
                            "verdict": "resolved",
                            "reason": "第二个小节的责任、动作和验收闭环已经补齐。",
                            "evidence_refs": [
                                "Work/runs/run-module-five-review/modules/2.1-r1.json"
                            ],
                        },
                    ],
                    new_findings=[],
                ),
            ),
        ],
    )
    state = {
        "run_id": "run-module-five-review",
        "request": SimpleNamespace(user_supplements=[]),
    }

    revised = await run_module_review(
        runner,
        "2.1",
        module,
        state,
        "workflow-module-five-review",
        initial_scope={first, second},
        lifecycle_id="initial",
    )

    assert revised.revision == 1
    assert [call[0] for call in runner.calls] == [
        "evidence-auditor",
        "module-2.1-specialist",
        "evidence-auditor",
    ]
    assert [call[2] for call in runner.calls] == [
        "module-auditor-2.1",
        "module-2.1",
        "module-auditor-2.1",
    ]
    assert all(
        not (session_key or "").startswith("submodule-")
        for _agent, _output, session_key in runner.calls
    )
    assert runner.envelopes[0].target_submodule_ids == [first, second]
    assert runner.envelopes[1].target_submodule_ids == [first, second]
    assert runner.envelopes[2].target_submodule_ids == [first, second]


@pytest.mark.asyncio
async def test_module_preflight_machine_correction_precedes_paid_review(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    module = module.model_copy(
        update={
            "submodule_narratives": {
                **module.submodule_narratives,
                target: (
                    module.submodule_narratives[target]
                    + "\n\n[[APPROVED_MODULE:2.1]]"
                ),
            }
        }
    )
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={
            target: "已移除运行时控制标记，保留现状、风险、行动和验收正文。"
        },
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            ("module-2.1-specialist", "module_revision_submission", patch),
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[],
                ),
            ),
        ],
    )
    state = {"run_id": "run-machine-preflight"}

    result = await run_module_review(
        runner,
        "2.1",
        module,
        state,
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert result.revision == 1
    assert [call[0] for call in runner.calls] == [
        "module-2.1-specialist",
        "evidence-auditor",
    ]
    correction_input = json.loads(
        (
            tmp_path
            / (
                "Work/runs/run-machine-preflight/reviews/"
                "module-revision-input-2.1-r1.json"
            )
        ).read_text(encoding="utf-8")
    )
    assert correction_input["module_findings"] == []
    assert correction_input["cross_findings"] == []
    assert correction_input["requested_changes"] == []
    assert correction_input["target_submodule_ids"] == [target]
    assert correction_input["validation_report"]["passed"] is False
    assert all(
        failure["finding_id"] is None
        for failure in correction_input["validation_report"]["failures"]
    )
    assert runner.envelopes[0].target_submodule_ids == [target]
    assert runner.calls[0][2] == "module-2.1"
    assert any(
        "显式机器检查未通过" in constraint
        for constraint in runner.envelopes[0].constraints
    )
    paid_review_input = json.loads(
        (
            tmp_path
            / (
                "Work/runs/run-machine-preflight/reviews/module/"
                "initial/2.1/input-r0.json"
            )
        ).read_text(encoding="utf-8")
    )
    assert paid_review_input["subject_revision"] == 1
    assert paid_review_input["validation_report"]["passed"] is True


def _preflight_revision_patch(
    module: ModuleSubmission,
    target: str,
    *,
    preserve_failure: bool,
) -> ModuleRevisionSubmission:
    narrative = (
        module.submodule_narratives[target]
        if preserve_failure
        else "### 修订后正文\n\n已移除运行时控制标记，保留现状、风险、行动和验收正文。"
    )
    return ModuleRevisionSubmission(
        module_id=module.module_id,
        base_revision=module.revision,
        revision=module.revision + 1,
        submodule_narratives={target: narrative},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[],
    )


@pytest.mark.asyncio
async def test_module_initial_review_step_returns_machine_correction_before_auditor(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    module = module.model_copy(
        update={
            "submodule_narratives": {
                **module.submodule_narratives,
                target: module.submodule_narratives[target] + "\n\n[[APPROVED_MODULE:2.1]]",
            }
        }
    )
    runner = _ScriptedRunner(tmp_path, [])
    state = {"run_id": "run-initial-review-machine-step"}

    correction = await prepare_module_initial_review_step(
        runner,
        module_id="2.1",
        payload=module,
        state=state,
        workflow_id="workflow-initial-review-machine-step",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert isinstance(correction, ModuleInitialReviewPreparation)
    assert correction.mode == "preflight_revision"
    assert correction.validation_ref is not None
    assert correction.validation_target_submodule_ids == [target]
    assert correction.preflight_progress is not None
    assert correction.envelope is None
    assert correction.review_input is None
    assert runner.calls == []

    revision_preparation = await prepare_module_revision(
        runner,
        state=state,
        workflow_id="workflow-initial-review-machine-step",
        subject=module,
        validation_ref=correction.validation_ref,
        validation_target_submodule_ids=set(correction.validation_target_submodule_ids),
    )
    revised, subject_ref = accept_module_initial_review_preflight_revision(
        runner,
        state=state,
        preparation=correction,
        revision_preparation=revision_preparation,
        result=_preflight_revision_patch(module, target, preserve_failure=False),
    )
    assert revised.revision == 1
    assert subject_ref.endswith("/modules/2.1-r1.json")

    review = await prepare_module_initial_review_step(
        runner,
        module_id="2.1",
        payload=revised,
        state={**state, "resume": True},
        workflow_id="workflow-initial-review-machine-step",
        initial_scope={target},
        lifecycle_id="initial",
        preflight_progress=correction.preflight_progress,
    )

    assert isinstance(review, ModuleInitialReviewPreparation)
    assert review.mode == "invoke_agent"
    assert review.envelope is not None
    assert review.envelope.agent_id == "evidence-auditor"
    assert review.envelope.allowed_outputs == ["module_review_finding_submission"]
    assert review.review_input is not None
    assert review.review_input.subject_ref.endswith("/modules/2.1-r1.json")
    assert runner.calls == []

    assert review.preflight_progress is not None
    assert review.preflight_progress.current == revised


@pytest.mark.asyncio
async def test_module_initial_review_step_stops_repeated_machine_failure_before_second_correction(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    module = module.model_copy(
        update={
            "submodule_narratives": {
                **module.submodule_narratives,
                target: module.submodule_narratives[target] + "\n\n[[APPROVED_MODULE:2.1]]",
            }
        }
    )
    runner = _ScriptedRunner(tmp_path, [])
    state = {"run_id": "run-initial-review-machine-repeat"}

    correction = await prepare_module_initial_review_step(
        runner,
        module_id="2.1",
        payload=module,
        state=state,
        workflow_id="workflow-initial-review-machine-repeat",
        initial_scope={target},
        lifecycle_id="initial",
    )
    assert isinstance(correction, ModuleInitialReviewPreparation)
    assert correction.mode == "preflight_revision"
    assert correction.validation_ref is not None
    assert correction.validation_target_submodule_ids == [target]
    assert correction.preflight_progress is not None

    revision_preparation = await prepare_module_revision(
        runner,
        state=state,
        workflow_id="workflow-initial-review-machine-repeat",
        subject=module,
        validation_ref=correction.validation_ref,
        validation_target_submodule_ids=set(correction.validation_target_submodule_ids),
    )
    revised, subject_ref = accept_module_initial_review_preflight_revision(
        runner,
        state=state,
        preparation=correction,
        revision_preparation=revision_preparation,
        result=_preflight_revision_patch(module, target, preserve_failure=True),
    )
    assert revised.revision == 1
    assert subject_ref.endswith("/modules/2.1-r1.json")

    with pytest.raises(ReviewLifecycleError, match="repeated"):
        await prepare_module_initial_review_step(
            runner,
            module_id="2.1",
            payload=revised,
            state={**state, "resume": True},
            workflow_id="workflow-initial-review-machine-repeat",
            initial_scope={target},
            lifecycle_id="initial",
            preflight_progress=correction.preflight_progress,
        )

    assert runner.calls == []
    assert not (
        tmp_path / "Work/runs/run-initial-review-machine-repeat/modules/2.1-r2.json"
    ).exists()


@pytest.mark.asyncio
async def test_module_recheck_boundary_prepares_and_accepts_without_provider(
    tmp_path: Path,
) -> None:
    run_id = "run-module-recheck-boundary"
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding = ModuleReviewFindingSubmission(
        coverage={"submodule_ids": [target]},
        findings=[
            {
                "id": "M-2.1-initial-r0-RECHECK",
                "target_submodule_id": target,
                "category": "analysis_depth",
                "impact": "advisory",
                "observation": "当前建议缺少责任接口和可由原审查者复核的验收方法。",
                "evidence_refs": ["Work/runs/run-module-recheck-boundary/modules/2.1-r0.json"],
                "required_change": "在目标小节补充责任接口、执行动作和可验证验收方法。",
                "reviewer_checks": ["责任、动作和验收方法已经形成闭环"],
            }
        ],
    )
    runner = _ScriptedRunner(tmp_path, [])
    state = {"run_id": run_id}
    initial = await prepare_module_initial_review(
        runner,
        module_id="2.1",
        payload=module,
        state=state,
        workflow_id="workflow-module-recheck-boundary",
        initial_scope={target},
        lifecycle_id="initial",
    )
    accepted_initial = accept_module_initial_review(
        runner,
        preparation=initial,
        result=finding,
        state=state,
    )
    assert accepted_initial.next_action == "revise"

    revision = await prepare_module_revision(
        runner,
        state=state,
        workflow_id="workflow-module-recheck-boundary",
        subject=module,
        module_findings=accepted_initial.findings,
    )
    revised, subject_ref = accept_module_revision(
        runner,
        preparation=revision,
        result=ModuleRevisionSubmission(
            module_id="2.1",
            base_revision=0,
            revision=1,
            submodule_narratives={
                target: "### 修订后正文\n\n已补充责任接口和可验证验收方法。",
            },
            claims_upsert=[],
            claim_ids_remove=[],
            source_ids=[],
            unresolved_questions=[],
            revision_responses=[
                {
                    "finding_id": "M-2.1-initial-r0-RECHECK",
                    "action": "implemented",
                    "summary": "已在目标小节补充责任接口、执行动作和可复核的验收方法。",
                    "changed_target_ids": [target],
                }
            ],
        ),
    )
    assert subject_ref.endswith("/modules/2.1-r1.json")

    preparation = await prepare_module_recheck(
        runner,
        module_id="2.1",
        current=revised,
        state=state,
        workflow_id="workflow-module-recheck-boundary",
        initial_scope={target},
    )
    assert isinstance(preparation, ModuleRecheckPreparation)
    assert (
        ModuleRecheckPreparation.model_validate_json(preparation.model_dump_json())
        == preparation
    )
    assert preparation.mode == "invoke_agent"
    assert preparation.reviewer_session_key == "module-auditor-2.1"
    assert preparation.review_round == 1
    assert preparation.subject_ref == subject_ref
    assert preparation.review_input is not None
    assert preparation.review_input.phase == "recheck"
    assert preparation.review_input.baseline_subject_ref.endswith("/modules/2.1-r0.json")
    assert preparation.review_input.required_findings[0].id == (
        "M-2.1-initial-r0-RECHECK"
    )
    assert preparation.envelope is not None
    assert preparation.envelope.allowed_outputs == ["module_review_verdict_submission"]
    assert runner.calls == []

    resumed_preparation = await prepare_module_recheck(
        runner,
        module_id="2.1",
        current=revised,
        state=state,
        workflow_id="workflow-module-recheck-boundary",
        initial_scope={target},
    )
    assert resumed_preparation.mode == "invoke_agent"
    assert resumed_preparation.review_round == preparation.review_round
    assert resumed_preparation.reviewer_session_key == preparation.reviewer_session_key

    accepted = await accept_module_recheck(
        runner,
        preparation=resumed_preparation,
        result=ModuleReviewVerdictSubmission(
            coverage={"submodule_ids": [target]},
            verdicts=[
                {
                    "finding_id": "M-2.1-initial-r0-RECHECK",
                    "verdict": "resolved",
                    "reason": "当前修订已经补充责任、执行动作和验收方法，可以关闭。",
                    "evidence_refs": [subject_ref],
                }
            ],
            new_findings=[],
        ),
        state=state,
    )
    assert isinstance(accepted, ModuleRecheckAcceptance)
    assert accepted.next_action == "completed"
    assert accepted.resolved_ids == ["M-2.1-initial-r0-RECHECK"]
    assert accepted.completion_ref is not None
    assert (tmp_path / accepted.verdict_refs[-1]).is_file()
    progress = json.loads((tmp_path / preparation.progress_ref).read_text(encoding="utf-8"))
    assert progress["next_action"] == "completed"
    assert progress["current"]["revision"] == 1
    assert runner.calls == []

    completed_preparation = await prepare_module_recheck(
        runner,
        module_id="2.1",
        current=revised,
        state=state,
        workflow_id="workflow-module-recheck-boundary",
        initial_scope={target},
    )
    resumed_completion = resume_module_recheck(
        preparation=completed_preparation,
        state=state,
    )
    assert resumed_completion.next_action == "completed"
    assert resumed_completion.current == revised
    assert resumed_completion.completion_ref == accepted.completion_ref


async def _prepare_recheck_machine_candidate(
    tmp_path: Path,
    run_id: str,
    *,
    marker: str,
) -> tuple[
    _ScriptedRunner,
    dict,
    ModuleSubmission,
    str,
    ModuleInitialReviewAcceptance,
    ModuleSubmission,
]:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding_id = "M-2.1-initial-r0-RECHECK"
    finding = ModuleReviewFindingSubmission(
        coverage={"submodule_ids": [target]},
        findings=[
            {
                "id": finding_id,
                "target_submodule_id": target,
                "category": "analysis_depth",
                "impact": "advisory",
                "observation": "当前建议缺少责任接口和可由原审查者复核的验收方法。",
                "evidence_refs": [f"Work/runs/{run_id}/modules/2.1-r0.json"],
                "required_change": "在目标小节补充责任接口、执行动作和可验证验收方法。",
                "reviewer_checks": ["责任、动作和验收方法已经形成闭环"],
            }
        ],
    )
    runner = _ScriptedRunner(tmp_path, [])
    state = {"run_id": run_id}
    initial = await prepare_module_initial_review(
        runner,
        module_id="2.1",
        payload=module,
        state=state,
        workflow_id=f"workflow-{run_id}",
        initial_scope={target},
        lifecycle_id="initial",
    )
    accepted_initial = accept_module_initial_review(
        runner,
        preparation=initial,
        result=finding,
        state=state,
    )
    revision = await prepare_module_revision(
        runner,
        state=state,
        workflow_id=f"workflow-{run_id}",
        subject=module,
        module_findings=accepted_initial.findings,
    )
    revised, _subject_ref = accept_module_revision(
        runner,
        preparation=revision,
        result=ModuleRevisionSubmission(
            module_id="2.1",
            base_revision=0,
            revision=1,
            submodule_narratives={
                target: f"### 修订后正文\n\n已补充责任接口和可验证验收方法。\n\n{marker}",
            },
            claims_upsert=[],
            claim_ids_remove=[],
            source_ids=[],
            unresolved_questions=[],
            revision_responses=[
                {
                    "finding_id": finding_id,
                    "action": "implemented",
                    "summary": "已在目标小节补充责任接口、执行动作和可复核的验收方法。",
                    "changed_target_ids": [target],
                }
            ],
        ),
    )
    return runner, state, module, target, accepted_initial, revised


def _recheck_machine_revision(
    subject: ModuleSubmission,
    target: str,
    finding_id: str,
    *,
    marker: str,
) -> ModuleRevisionSubmission:
    return ModuleRevisionSubmission(
        module_id=subject.module_id,
        base_revision=subject.revision,
        revision=subject.revision + 1,
        submodule_narratives={
            target: f"### 再次修订正文\n\n已继续补充责任接口和可验证验收方法。\n\n{marker}",
        },
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": finding_id,
                "action": "implemented",
                "summary": "已继续修订目标小节并保留可复核的验收方法。",
                "changed_target_ids": [target],
            }
        ],
    )


@pytest.mark.asyncio
async def test_module_recheck_step_returns_machine_correction_before_original_auditor(
    tmp_path: Path,
) -> None:
    run_id = "run-module-recheck-machine-step"
    runner, state, _module_v0, target, accepted_initial, revised = (
        await _prepare_recheck_machine_candidate(
            tmp_path,
            run_id,
            marker="[[RECHECK_PREFLIGHT_A]]",
        )
    )
    finding_id = accepted_initial.findings[0].id

    correction = await prepare_module_recheck(
        runner,
        module_id="2.1",
        current=revised,
        state=state,
        workflow_id=f"workflow-{run_id}",
        initial_scope={target},
    )

    assert isinstance(correction, ModuleRecheckPreparation)
    assert correction.mode == "preflight_revision"
    assert correction.validation_ref is not None
    assert correction.validation_target_submodule_ids == [target]
    assert correction.preflight_progress is not None
    assert correction.envelope is None
    assert correction.review_input is None
    assert correction.pending[0].id == finding_id
    assert runner.calls == []

    revision = await prepare_module_revision(
        runner,
        state=state,
        workflow_id=f"workflow-{run_id}",
        subject=revised,
        module_findings=correction.pending,
        validation_ref=correction.validation_ref,
        validation_target_submodule_ids=set(correction.validation_target_submodule_ids),
    )
    fixed, subject_ref = accept_module_recheck_preflight_revision(
        runner,
        state=state,
        preparation=correction,
        revision_preparation=revision,
        result=_recheck_machine_revision(
            revised,
            target,
            finding_id,
            marker="",
        ),
    )
    assert fixed.revision == 2
    assert subject_ref.endswith("/modules/2.1-r2.json")

    review = await prepare_module_recheck(
        runner,
        module_id="2.1",
        current=fixed,
        state=state,
        workflow_id=f"workflow-{run_id}",
        initial_scope={target},
        preflight_progress=correction.preflight_progress,
    )

    assert isinstance(review, ModuleRecheckPreparation)
    assert review.mode == "invoke_agent"
    assert review.review_round == 1
    assert review.reviewer_session_key == "module-auditor-2.1"
    assert review.review_input is not None
    assert review.review_input.phase == "recheck"
    assert review.review_input.subject_ref.endswith("/modules/2.1-r2.json")
    assert review.envelope is not None
    assert review.envelope.allowed_outputs == ["module_review_verdict_submission"]
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("markers", "stop_attempt"),
    [
        (["[[RECHECK_PREFLIGHT_A]]", "[[RECHECK_PREFLIGHT_A]]"], 2),
        (
            [
                "[[RECHECK_PREFLIGHT_A]]",
                "[[RECHECK_PREFLIGHT_B]]",
                "[[RECHECK_PREFLIGHT_C]]",
            ],
            3,
        ),
    ],
)
async def test_module_recheck_preflight_preserves_legacy_repeat_and_total_stop(
    tmp_path: Path,
    markers: list[str],
    stop_attempt: int,
) -> None:
    run_id = f"run-module-recheck-machine-stop-{stop_attempt}"
    runner, state, _module_v0, target, accepted_initial, revised = (
        await _prepare_recheck_machine_candidate(
            tmp_path,
            run_id,
            marker=markers[0],
        )
    )
    finding_id = accepted_initial.findings[0].id
    preflight_progress = None
    validation_ref = None
    validation_target_submodule_ids: list[str] = []
    current = revised

    for attempt, marker in enumerate(markers, start=1):
        if attempt > 1:
            revision = await prepare_module_revision(
                runner,
                state=state,
                workflow_id=f"workflow-{run_id}",
                subject=current,
                module_findings=accepted_initial.findings,
                validation_ref=validation_ref,
                validation_target_submodule_ids=set(validation_target_submodule_ids),
            )
            current, _subject_ref = accept_module_recheck_preflight_revision(
                runner,
                state=state,
                preparation=correction,
                revision_preparation=revision,
                result=_recheck_machine_revision(
                    current,
                    target,
                    finding_id,
                    marker=marker,
                ),
            )

        if attempt == stop_attempt:
            with pytest.raises(ReviewLifecycleError, match="repeated"):
                await prepare_module_recheck(
                    runner,
                    module_id="2.1",
                    current=current,
                    state=state,
                    workflow_id=f"workflow-{run_id}",
                    initial_scope={target},
                    preflight_progress=preflight_progress,
                )
            break

        correction = await prepare_module_recheck(
            runner,
            module_id="2.1",
            current=current,
            state=state,
            workflow_id=f"workflow-{run_id}",
            initial_scope={target},
            preflight_progress=preflight_progress,
        )
        assert correction.mode == "preflight_revision"
        assert correction.preflight_progress is not None
        preflight_progress = correction.preflight_progress
        validation_ref = correction.validation_ref
        validation_target_submodule_ids = correction.validation_target_submodule_ids

    assert runner.calls == []
    assert not (
        tmp_path / f"Work/runs/{run_id}/modules/2.1-r{stop_attempt + 1}.json"
    ).exists()


@pytest.mark.asyncio
async def test_module_recheck_sends_only_changed_claim_semantics_and_evidence(
    tmp_path: Path,
) -> None:
    run_id = "run-module-delta"
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    ledger = SourceLedger(tmp_path, run_id)
    for evidence_id in ("E-0001", "E-0002", "E-0003"):
        ledger.register_project(
            evidence_id,
            f"{evidence_id} title",
            f"Inputs/source.xlsx#{evidence_id}",
            f"{evidence_id} bounded content",
        )
    changed_v0 = ClaimRecord(
        id="C-CHANGED",
        module_id="2.1",
        submodule_id=target,
        text="修订前的风险判断。",
        claim_type="risk_judgment",
        source_ids=["E-0001"],
    )
    stable = ClaimRecord(
        id="C-STABLE",
        module_id="2.1",
        submodule_id=target,
        text="未变化的稳定判断。",
        claim_type="technical_interpretation",
        source_ids=["E-0003"],
    )
    module = _module("2.1").model_copy(
        update={
            "submodule_narratives": {
                **_module("2.1").submodule_narratives,
                target: (
                    "修订前正文。\n\n"
                    "[[CLAIM:C-CHANGED]]\n\n[[CLAIM:C-STABLE]]"
                ),
            },
            "claims": [changed_v0, stable],
            "source_ids": ["E-0001", "E-0003"],
        }
    )
    finding_id = "M-2.1-initial-r0-DELTA"
    finding = {
        "id": finding_id,
        "target_submodule_id": target,
        "category": "evidence_boundary",
        "impact": "advisory",
        "observation": "当前风险判断需要更新证据边界并保持未变化判断。",
        "evidence_refs": ["E-0001"],
        "required_change": "更新目标风险判断及其证据，不改写稳定判断。",
        "reviewer_checks": ["核对新旧判断和证据变化，并确认稳定判断未被改写"],
    }
    changed_v1 = changed_v0.model_copy(
        update={
            "text": "修订后的风险判断。",
            "source_ids": ["E-0002"],
        }
    )
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={
            target: (
                "修订后正文。\n\n"
                "[[CLAIM:C-CHANGED]]\n\n[[CLAIM:C-STABLE]]"
            )
        },
        claims_upsert=[changed_v1],
        claim_ids_remove=[],
        source_ids=["E-0002", "E-0003"],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": finding_id,
                "action": "implemented",
                "summary": "已更新目标风险判断和证据边界，并完整保留稳定判断。",
                "changed_target_ids": [target],
            }
        ],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[finding],
                ),
            ),
            ("module-2.1-specialist", "module_revision_submission", patch),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [target]},
                    verdicts=[
                        {
                            "finding_id": finding_id,
                            "verdict": "resolved",
                            "reason": "新旧风险语义和证据变化清楚，稳定判断保持不变。",
                            "evidence_refs": [
                                f"Work/runs/{run_id}/modules/2.1-r1.json"
                            ],
                        }
                    ],
                    new_findings=[],
                ),
            ),
        ],
    )
    knowledge_ref = f"Work/runs/{run_id}/knowledge/module-2.1.md"
    runner.service.store.write_text(
        knowledge_ref,
        (
            f"## {target} 审查知识\n"
            "TARGET-RECHECK-KNOWLEDGE：该机理和适用条件必须在复核时继续可见。"
        ),
    )

    await run_module_review(
        runner,
        "2.1",
        module,
        {
            "run_id": run_id,
            "module_knowledge_refs": {"2.1": knowledge_ref},
        },
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )

    recheck = json.loads(
        (
            tmp_path
            / f"Work/runs/{run_id}/reviews/module/initial/2.1/input-r1.json"
        ).read_text(encoding="utf-8")
    )
    changed_ref = (
        "statement-" + hashlib.sha256(b"C-CHANGED").hexdigest()[:12]
    )
    stable_ref = "statement-" + hashlib.sha256(b"C-STABLE").hexdigest()[:12]
    assert recheck["baseline_subject_ref"].endswith("/modules/2.1-r0.json")
    assert recheck["revision_diff"]["changed_statement_refs"] == [changed_ref]
    assert [item["text"] for item in recheck["claim_statements"]] == [
        "修订后的风险判断。"
    ]
    assert [item["text"] for item in recheck["prior_claim_statements"]] == [
        "修订前的风险判断。"
    ]
    assert set(recheck["unchanged_statement_sha256"]) == {stable_ref}
    assert recheck["subject"]["evidence_ids_by_submodule"] == {
        target: ["E-0002"]
    }
    assert recheck["knowledge_ref"] == knowledge_ref
    assert recheck["knowledge_context"] == ""
    assert recheck["knowledge_ref"]
    assert {item["evidence_id"] for item in recheck["evidence"]} == {
        "E-0001",
        "E-0002",
    }


@pytest.mark.asyncio
async def test_module_review_resume_continues_after_persisted_findings_without_reaudit(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding = {
        "id": "M-2.1-initial-r0-RESUME",
        "target_submodule_id": target,
        "category": "analysis_depth",
        "impact": "advisory",
        "observation": "当前建议没有明确责任接口、执行动作、完成时限以及可由原审查者复核的验收方式。",
        "evidence_refs": ["Work/runs/run-review-resume/modules/2.1-r0.json"],
        "required_change": "在目标小节补充责任接口、具体执行动作、完成时限以及可验证的验收方法。",
        "reviewer_checks": ["核对责任和验收闭环"],
    }
    first = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[finding],
                ),
            ),
        ],
    )
    with pytest.raises(IndexError):
        await run_module_review(
            first,
            "2.1",
            module,
            {"run_id": "run-review-resume"},
            "workflow",
            initial_scope={target},
            lifecycle_id="initial",
        )

    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={target: "### 修订\n\n已补充责任接口和可验证验收方法。"},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": "M-2.1-initial-r0-RESUME",
                "action": "implemented",
                "summary": "已在目标小节补充责任接口、具体执行动作、完成时限以及可复核的验收方法。",
                "changed_target_ids": [target],
            }
        ],
    )
    resumed = _ScriptedRunner(
        tmp_path,
        [
            ("module-2.1-specialist", "module_revision_submission", patch),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [target]},
                    verdicts=[
                        {
                            "finding_id": "M-2.1-initial-r0-RESUME",
                            "verdict": "resolved",
                            "reason": "复核确认修订已明确责任接口、执行动作、完成时限和验收方式，可以关闭。",
                            "evidence_refs": ["Work/runs/run-review-resume/modules/2.1-r1.json"],
                        }
                    ],
                    new_findings=[],
                ),
            ),
        ],
    )
    state = {"run_id": "run-review-resume", "resume": True}
    result = await run_module_review(
        resumed,
        "2.1",
        module,
        state,
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert result.revision == 1
    assert [call[0] for call in resumed.calls] == [
        "module-2.1-specialist",
        "evidence-auditor",
    ]


@pytest.mark.asyncio
async def test_module_review_resume_ignores_legacy_lifecycle_identity(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    store = ReportingStore(tmp_path)
    store.write_json(
        "Work/runs/run-legacy-review/reviews/module/initial/2.1/progress.json",
        {
            "run_id": "run-legacy-review",
            "module_id": "2.1",
            "next_action": "review",
            "current": module.model_dump(mode="json"),
            "pending": [],
            "responses": [],
            "finding_refs": [],
            "verdict_refs": [],
            "resolved_ids": [],
            "review_round": 0,
            "phase": "initial",
            "scope": [target],
        },
    )
    store.write_json(
        "Work/runs/run-legacy-review/agent-identities.json",
        {
            "workflow_id": "workflow",
            "created_by": "main",
            "identities": {
                "module-auditor-2.1-initial": {
                    "agent_id": "evidence-auditor",
                }
            },
        },
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[],
                ),
            )
        ],
    )

    await run_module_review(
        runner,
        "2.1",
        module,
        {"run_id": "run-legacy-review", "resume": True},
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert runner.calls[0][2] == "module-auditor-2.1"
    completion = ReviewCompletionRecord.model_validate_json(
        (
            tmp_path
            / "Work/runs/run-legacy-review/reviews/module/initial/2.1/completion-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert completion.review_protocol_version == 2
    assert completion.reviewer_session_key == "module-auditor-2.1"


@pytest.mark.asyncio
async def test_module_review_lifecycles_use_disjoint_immutable_artifacts(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    state = {"run_id": "run-isolated-reviews"}
    initial = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[],
                ),
            )
        ],
    )
    await run_module_review(
        initial,
        "2.1",
        module,
        state,
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )
    initial_ref = state["module_review_completion_refs"]["2.1"]
    initial_bytes = (tmp_path / initial_ref).read_bytes()

    regression = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[],
                ),
            )
        ],
    )
    await run_module_review(
        regression,
        "2.1",
        module,
        state,
        "workflow",
        initial_scope={target},
        lifecycle_id="cross-r0",
    )
    regression_ref = state["module_review_completion_refs"]["2.1"]

    assert initial_ref != regression_ref
    assert "/module/initial/2.1/" in initial_ref
    assert "/module/cross-r0/2.1/" in regression_ref
    assert (tmp_path / initial_ref).read_bytes() == initial_bytes
    assert (tmp_path / regression_ref).is_file()
    assert initial.calls[0][2] == "module-auditor-2.1"
    assert regression.calls[0][2] == "module-auditor-2.1"


@pytest.mark.asyncio
async def test_author_dispute_enters_main_before_original_reviewer_recheck(
    tmp_path: Path,
) -> None:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding = {
        "id": "M-2.1-initial-r0-DISPUTE",
        "target_submodule_id": target,
        "category": "evidence_boundary",
        "impact": "advisory",
        "observation": "审查者要求补写当前证据并不支持的确定性结论。",
        "evidence_refs": ["Work/runs/run-dispute/modules/2.1-r0.json"],
        "required_change": "逐项说明当前是否存在足够证据；若无法支持新增确定性结论，应明确提出异议并保持证据边界。",
        "reviewer_checks": ["核对作者异议及当前证据边界"],
    }
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": "M-2.1-initial-r0-DISPUTE",
                "action": "disputed",
                "summary": "当前输入没有支持新增确定性结论的证据，因此保留原文并提出异议。",
                "changed_target_ids": [],
            }
        ],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[finding],
                ),
            ),
            ("module-2.1-specialist", "module_revision_submission", patch),
            (
                "main-agent",
                "workflow_decision_submission",
                WorkflowDecisionSubmission(
                    decision="accept_dispute",
                    rationale="作者异议属于证据边界问题，可交回原审查者依据当前正文复核。",
                    finding_ids=["M-2.1-initial-r0-DISPUTE"],
                ),
            ),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [target]},
                    verdicts=[
                        {
                            "finding_id": "M-2.1-initial-r0-DISPUTE",
                            "verdict": "resolved",
                            "reason": "复核确认原文已正确保持证据边界，无需新增结论。",
                            "evidence_refs": ["Work/runs/run-dispute/modules/2.1-r1.json"],
                        }
                    ],
                    new_findings=[],
                ),
            ),
        ],
    )
    state = {"run_id": "run-dispute"}

    await run_module_review(
        runner,
        "2.1",
        module,
        state,
        "workflow",
        initial_scope={target},
        lifecycle_id="initial",
    )

    assert [call[0] for call in runner.calls] == [
        "evidence-auditor",
        "module-2.1-specialist",
        "main-agent",
        "evidence-auditor",
    ]
    exception = json.loads(
        (
            tmp_path
            / (
                "Work/runs/run-dispute/exceptions/"
                "module-author_response-M-2.1-initial-r0-DISPUTE.json"
            )
        ).read_text(encoding="utf-8")
    )
    assert exception["trigger"] == "author_response"
    assert exception["finding_ids"] == ["M-2.1-initial-r0-DISPUTE"]


@pytest.mark.asyncio
async def _legacy_cross_finding_is_closed_by_cross_reviewer_not_module_auditor(
    tmp_path: Path,
) -> None:
    modules = {module_id: _module(module_id) for module_id in REPORT_TAXONOMY}
    for module_id in ("2.1", "2.3"):
        modules[module_id] = ModuleSubmission.model_validate(
            {
                **modules[module_id].model_dump(mode="python"),
                "claims": [
                    {
                        "id": f"C-{module_id}-CROSS",
                        "module_id": module_id,
                        "submodule_id": next(iter(REPORT_TAXONOMY[module_id].submodules)),
                        "text": "该模块已批准一个用于跨模块关系验证的边界判断。",
                        "claim_type": "risk_judgment",
                        "source_ids": [],
                        "footnote_required": False,
                    }
                ],
            }
        )
    target = next(iter(REPORT_TAXONOMY["2.3"].submodules))
    coverage = [
        {
            "module_id": module_id,
            "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS),
        }
        for module_id in REPORT_TAXONOMY
    ]
    cross_finding = {
        "id": "X-005",
        "owner_module_id": "2.3",
        "target_submodule_ids": [target],
        "related_module_ids": ["2.4"],
        "category": "dependencies",
        "impact": "blocking",
        "observation": "模块 2.3 未说明其行动对模块 2.4 前置条件的依赖和联合验收。",
        "evidence_refs": [
            "Work/runs/run-x/modules/2.3-r0.json",
            "Work/runs/run-x/modules/2.4-r0.json",
        ],
        "required_change": "在责任模块目标小节写入依赖对象、作用机制、实施顺序、责任接口和联合验收方式。",
        "reviewer_checks": ["核对责任模块已完整写入跨模块依赖与联合验收"],
    }
    patch = ModuleRevisionSubmission(
        module_id="2.3",
        base_revision=0,
        revision=1,
        submodule_narratives={target: "### 修订\n\n已写入依赖对象、作用机制、实施顺序和联合验收。"},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": "X-005",
                "action": "implemented",
                "summary": "已在责任小节写入跨模块依赖、顺序和联合验收。",
                "changed_target_ids": [target],
            }
        ],
    )
    local_finding_id = "M-2.3-cross-r0-r0-REGRESSION"
    local_finding = {
        "id": local_finding_id,
        "target_submodule_id": target,
        "category": "regression",
        "impact": "blocking",
        "observation": "Cross 回改写入了依赖和联合验收，但未明确验收记录的责任接口。",
        "evidence_refs": ["Work/runs/run-x/modules/2.3-r1.json"],
        "required_change": "补充联合验收记录的责任接口，并保持 Cross 已要求的依赖、顺序和验收内容。",
        "reviewer_checks": ["核对责任接口与原 Cross 回改内容同时保留"],
    }
    local_patch = ModuleRevisionSubmission(
        module_id="2.3",
        base_revision=1,
        revision=2,
        submodule_narratives={
            target: (
                "### 修订\n\n已写入依赖对象、作用机制、实施顺序和联合验收，"
                "并由供配电与保护责任人共同签署验收记录。"
            )
        },
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": local_finding_id,
                "action": "implemented",
                "summary": "已补充联合验收记录的责任接口，并保留 Cross 回改内容。",
                "changed_target_ids": [target],
            }
        ],
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "cross-module-reviewer",
                "cross_review_finding_submission",
                CrossReviewFindingSubmission(
                    coverage=coverage,
                    findings=[cross_finding],
                    synthesis_inputs=_cross_synthesis_inputs(),
                ),
            ),
            ("module-2.3-specialist", "module_revision_submission", patch),
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[local_finding],
                ),
            ),
            ("module-2.3-specialist", "module_revision_submission", local_patch),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [target]},
                    verdicts=[
                        {
                            "finding_id": local_finding_id,
                            "verdict": "resolved",
                            "reason": "责任接口已补充，且 Cross 要求的依赖、顺序与联合验收均保留。",
                            "evidence_refs": ["Work/runs/run-x/modules/2.3-r2.json"],
                        }
                    ],
                    new_findings=[],
                ),
            ),
            (
                "cross-module-reviewer",
                "cross_review_verdict_submission",
                CrossReviewVerdictSubmission(
                    coverage=coverage,
                    verdicts=[
                        {
                            "finding_id": "X-005",
                            "verdict": "resolved",
                            "reason": "责任模块已写入依赖机制、顺序和联合验收，接口问题关闭。",
                            "evidence_refs": ["Work/runs/run-x/modules/2.3-r2.json"],
                        }
                    ],
                    new_findings=[],
                    synthesis_inputs=_cross_synthesis_inputs(),
                ),
            ),
        ],
    )
    knowledge_ref = "Work/runs/run-x/context/module-2.3-knowledge.md"
    other_target = next(
        submodule_id
        for submodule_id in REPORT_TAXONOMY["2.3"].submodules
        if submodule_id != target
    )
    runner.service.store.write_text(
        knowledge_ref,
        (
            "# 模块 2.3 确定性知识上下文\n\n"
            "项目 Knowledge 只提供方法和标准背景。\n\n"
            f"## {target} 目标知识\n\nTARGET-KNOWLEDGE\n\n"
            f"## {other_target} 无关知识\n\nUNRELATED-KNOWLEDGE\n"
        ),
    )
    state = {
        "run_id": "run-x",
        "module_submissions": modules,
        "module_knowledge_refs": {"2.3": knowledge_ref},
    }
    for module_id, module in modules.items():
        runner.service.store.write_json(
            f"Work/runs/run-x/modules/{module_id}-r0.json",
            module.model_dump(mode="json"),
        )
    baseline_ref = "Work/runs/run-x/modules/2.3-r0.json"
    prior_completion_ref = (
        "Work/runs/run-x/reviews/module/initial/2.3/completion-r0.json"
    )
    runner.service.store.write_json(
        prior_completion_ref,
        ReviewCompletionRecord(
            review_protocol_version=2,
            lifecycle="module",
            run_id="run-x",
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.3",
            subject_refs=[baseline_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_hashes(tmp_path, [baseline_ref]),
        ).model_dump(mode="json"),
    )
    state["module_review_completion_refs"] = {"2.3": prior_completion_ref}
    await run_cross_review(runner, state, "workflow")
    agents = [call[0] for call in runner.calls]
    assert agents == [
        *("cross-module-reviewer" for _ in REPORT_TAXONOMY),
        "module-2.3-specialist",
        "evidence-auditor",
        "module-2.3-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
    ]
    cross_sessions = [call[2] for call in runner.calls if call[0] == "cross-module-reviewer"]
    assert cross_sessions == [
        *(f"cross-module-reviewer-{module_id}" for module_id in REPORT_TAXONOMY),
        "cross-module-reviewer-2.3",
    ]
    assert all(
        "<module_skills>" not in envelope.inline_context
        and "<role_skill" not in envelope.inline_context
        and "<template_role_skill" not in envelope.inline_context
        and "<cross_lane_specialization" in envelope.inline_context
        for envelope in runner.envelopes
        if envelope.agent_id == "cross-module-reviewer"
    )
    initial_cross_inputs = [
        json.loads(
            (tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8")
        )
        for envelope in runner.envelopes
        if envelope.agent_id == "cross-module-reviewer" and envelope.revision == 0
    ]
    assert {item["owner_module_id"] for item in initial_cross_inputs} == set(
        REPORT_TAXONOMY
    )
    assert len({tuple(item["review_focus"]) for item in initial_cross_inputs}) == 5
    auditor_sessions = [call[2] for call in runner.calls if call[0] == "evidence-auditor"]
    assert auditor_sessions == ["module-auditor-2.3", "module-auditor-2.3"]
    cross_envelopes = [
        envelope
        for envelope in runner.envelopes
        if envelope.agent_id == "cross-module-reviewer"
    ]
    assert cross_envelopes
    assert all(envelope.allowed_tools == ["submit_result"] for envelope in cross_envelopes)
    assert all(
        envelope.input_refs == [envelope.input_contract_ref]
        for envelope in cross_envelopes
    )
    local_audit_envelopes = [
        envelope
        for envelope in runner.envelopes
        if envelope.agent_id == "evidence-auditor"
    ]
    assert local_audit_envelopes
    assert all(
        envelope.input_refs == [envelope.input_contract_ref]
        and envelope.allowed_tools == ["submit_result"]
        for envelope in local_audit_envelopes
    )
    local_input = json.loads(
        (
            tmp_path
            / "Work/runs/run-x/reviews/module/cross-r0/2.3/input-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert local_input["review_protocol_version"] == 2
    assert local_input["phase"] == "local_regression"
    assert local_input["prior_review_completion_ref"] == prior_completion_ref
    assert local_input["baseline_subject_ref"] == baseline_ref
    assert [item["id"] for item in local_input["trigger_cross_findings"]] == [
        "X-005"
    ]
    assert [
        item["finding_id"] for item in local_input["trigger_revision_responses"]
    ] == ["X-005"]
    assert set(local_input["subject"]["submodule_narratives"]) == {target}
    assert [
        statement["text"] for statement in local_input["claim_statements"]
    ] == ["该模块已批准一个用于跨模块关系验证的边界判断。"]
    assert local_input["claim_statements"][0]["statement_ref"].startswith(
        "statement-"
    )
    assert local_input["knowledge_ref"] == knowledge_ref
    assert local_input["knowledge_context"] == ""
    assert local_input["knowledge_ref"]
    assert local_input["revision_diff"]["changed_submodule_narratives"] == [target]
    recheck_input = json.loads(
        (tmp_path / "Work/runs/run-x/reviews/cross/2.3/input-r1.json").read_text(
            encoding="utf-8"
        )
    )
    assert recheck_input["owner_module_id"] == "2.3"
    assert recheck_input["review_focus"]
    assert recheck_input["changed_module_ids"] == ["2.3"]
    assert set(recheck_input["modules"]) == {"2.3"}
    assert set(recheck_input["unchanged_module_sha256"]) == {
        "2.1",
        "2.2",
        "2.4",
        "2.5",
    }
    assert "machine_validation_reports" not in recheck_input
    assert state["cross_review_completion_ref"].endswith("reviews/cross-completion.json")


@pytest.mark.asyncio
async def test_final_review_uses_chief_response_then_original_auditor_verdict(
    tmp_path: Path,
) -> None:
    module_text = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    current = _edited(module_text)
    finding = {
        "id": "F-001",
        "target_section_ids": ["3.1.2"],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "跨领域章节缺少行动依赖和联合验收，无法支持实施排序。",
        "evidence_refs": ["Work/runs/run-f/edited-revisions/chief-r0.json"],
        "target_changes": [
            {
                "target_section_id": "3.1.2",
                "required_change": "在 3.1.2 补充行动依赖顺序和联合验收。",
                "reviewer_checks": ["核对行动依赖和联合验收是否明确且不改变模块事实"],
            }
        ],
    }
    revised = current.model_copy(
        update={
            "dimension_risk_analysis": (
                current.dimension_risk_analysis + " 明确先完成前置核查，再联合验收并记录剩余风险。"
            ),
            "revision_responses": [
                RevisionResponse(
                    finding_id="F-001",
                    action="implemented",
                    summary="已在 3.1.2 补充前置顺序、联合验收和剩余风险记录。",
                    changed_target_ids=["3.1.2"],
                )
            ],
        }
    )
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "chief-editor-auditor",
                "final_review_finding_submission",
                FinalReviewFindingSubmission(
                    checked_section_ids=[
                        *FINAL_AUDIT_SECTION_IDS,
                    ],
                    findings=[finding],
                    residual_risks=[],
                ),
            ),
            (
                "chief-editor",
                "chief_revision_submission",
                ChiefRevisionSubmission(
                    base_subject_ref=("Work/runs/run-f/edited-revisions/chief-r0.json"),
                    revision=1,
                    section_bodies={
                        "3.1.2": revised.dimension_risk_analysis,
                    },
                    section_part_refs={
                        "3.1.2": (
                            "Work/runs/run-f/drafts/chief-edit-r1/r1/dimension_risk_analysis.md"
                        ),
                    },
                    revision_responses=revised.revision_responses,
                ),
            ),
            (
                "chief-editor-auditor",
                "final_review_verdict_submission",
                FinalReviewVerdictSubmission(
                    checked_section_ids=[
                        *FINAL_AUDIT_SECTION_IDS,
                    ],
                    verdicts=[
                        {
                            "finding_id": "F-001",
                            "verdict": "resolved",
                            "reason": "当前 3.1.2 已明确实施依赖和联合验收，问题关闭。",
                            "evidence_refs": ["Work/runs/run-f/edited-revisions/chief-r1.json"],
                        }
                    ],
                    new_findings=[],
                    residual_risks=[],
                ),
            ),
        ],
    )
    delivery_claims = [
        ClaimRecord(
            id=f"C-{module_id.replace('.', '')}-001",
            module_id=module_id,
            submodule_id=next(iter(REPORT_TAXONOMY[module_id].submodules)),
            text=f"{module_id} 测试结论",
            claim_type="recommendation",
            footnote_required=False,
        )
        for module_id in REPORT_TAXONOMY
    ]
    current = current.model_copy(
        update={
            "protected_claim_ids": [claim.id for claim in delivery_claims],
        }
    )
    state = {
        "run_id": "run-f",
        "edited_report": current,
        "module_submissions": {
            module_id: _module(module_id).model_copy(
                update={
                    "claims": [
                        claim for claim in delivery_claims if claim.module_id == module_id
                    ]
                }
            )
            for module_id in REPORT_TAXONOMY
        },
        "cross_synthesis_inputs": [],
    }
    await run_final_review(
        runner,
        state,
        "workflow",
        chief_envelope=TaskEnvelope(
            task_id="chief-edit",
            run_id="run-f",
            agent_id="chief-editor",
            objective="总编",
            allowed_outputs=["edited_report_submission"],
        ),
        chief_session_key="chief-editor",
        approved_module_text=module_text,
        claims=delivery_claims,
        aggregate_mode=True,
    )
    assert state["final_review_completion_ref"].endswith("reviews/final-completion.json")
    assert state["edited_report"].module_narratives == current.module_narratives
    assert state["edited_report"].photo_ids == current.photo_ids
    chief_revision_envelope = next(
        envelope
        for envelope in runner.envelopes
        if envelope.allowed_outputs == ["chief_revision_submission"]
    )
    assert chief_revision_envelope.context_summary_refs == []
    assert chief_revision_envelope.artifact_delivery_modes == {
        chief_revision_envelope.input_contract_ref: "inline",
        chief_revision_envelope.prior_result_ref: "hash_retained",
    }
    chief_revision_prompt = PromptAssembler.task_message(
        chief_revision_envelope,
        [],
        input_contract_payload=(
            tmp_path / chief_revision_envelope.input_contract_ref
        ).read_text(encoding="utf-8"),
    )
    assert 'delivery_mode="inline"' in chief_revision_prompt
    assert 'delivery_mode="hash_retained"' in chief_revision_prompt
    snapshot_ref = state["final_audit_snapshot_ref"]
    snapshot = json.loads(
        (tmp_path / snapshot_ref).read_text(encoding="utf-8")
    )
    assert snapshot["subject_ref"].endswith("/edited-revisions/chief-r1.json")
    assert snapshot["canonical_markdown_ref"].endswith(
        "/validation/report-chief-candidate-r1.md"
    )
    delivery_validator = object.__new__(ReportWorkflowRunner)
    delivery_validator.service = runner.service
    audited, validated_snapshot_ref = (
        delivery_validator._validated_final_audit_subject(state)
    )
    assert audited == state["edited_report"]
    assert validated_snapshot_ref == snapshot_ref
    tampered_state = {
        **state,
        "edited_report": state["edited_report"].model_copy(
            update={"title": "审计后被静默修改的标题"}
        ),
    }
    with pytest.raises(AgentWorkflowError, match="changed after final audit"):
        delivery_validator._validated_final_audit_subject(tampered_state)
    final_input = json.loads(
        (tmp_path / "Work/runs/run-f/reviews/final-review-input-r0.json").read_text(
            encoding="utf-8"
        )
    )
    assert final_input["required_section_ids"] == [
        "1.1", "1.2", "1.3", "3.1.1", "3.1.2", "3.1.3", "3.2"
    ]
    assert "subject" not in final_input
    assert set(final_input["subject_metadata"]) == {
        "photo_ids",
        "unresolved_editorial_issues",
    }
    assert "\n## 2. 评估内容描述" not in final_input["canonical_markdown"]
    recheck_input = json.loads(
        (tmp_path / "Work/runs/run-f/reviews/final-review-input-r1.json").read_text(
            encoding="utf-8"
        )
    )
    assert recheck_input["canonical_markdown"] is None
    assert recheck_input["subject_metadata"] is None
    assert "cross_synthesis_inputs" not in recheck_input
    assert set(recheck_input["changed_section_bodies"]) == {"3.1.2"}
    assert (
        set(recheck_input["unchanged_section_sha256"])
        == {"1.1", "1.2", "1.3", "3.1.1", "3.1.3", "3.2"}
    )
    assert len(recheck_input["subject_metadata_sha256"]) == 64
    final_inputs = [
        json.loads((tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8"))
        for envelope in runner.envelopes
        if envelope.agent_id == "chief-editor-auditor" and envelope.revision == 0
    ]
    assert len(final_inputs) == 1
    auditor_sessions = [call[2] for call in runner.calls if call[0] == "chief-editor-auditor"]
    assert auditor_sessions == ["chief-editor-auditor", "chief-editor-auditor"]
    final_audit_envelopes = [
        envelope
        for envelope in runner.envelopes
        if envelope.agent_id == "chief-editor-auditor"
    ]
    assert final_audit_envelopes
    assert all(
        envelope.input_refs == [envelope.input_contract_ref]
        and envelope.allowed_tools == ["submit_result"]
        for envelope in final_audit_envelopes
    )
    assert all(
        envelope.artifact_delivery_modes[envelope.prior_result_ref] == "hash_retained"
        for envelope in final_audit_envelopes
        if envelope.prior_result_ref is not None
    )
    chief_revision_envelope = next(
        envelope
        for envelope in runner.envelopes
        if envelope.agent_id == "chief-editor"
    )
    assert chief_revision_envelope.input_refs == [
        chief_revision_envelope.input_contract_ref
    ]
    assert chief_revision_envelope.allowed_tools == [
        "write_result_part",
        "list_result_parts",
        "submit_result",
    ]
    assert not any(
        "必须提交 risk_cluster_matrix" in constraint
        for constraint in chief_revision_envelope.constraints
    )
    assert any(
        "3.1.2 -> dimension_risk_analysis" in constraint
        for constraint in chief_revision_envelope.constraints
    )
    chief_revision_input = json.loads(
        (
            tmp_path
            / "Work/runs/run-f/reviews/chief-revision-input-r1.json"
        ).read_text(encoding="utf-8")
    )
    assert set(chief_revision_input["target_section_bodies"]) == {"3.1.2"}
    assert "3.1.2" not in chief_revision_input["consistency_context"]
    assert set(chief_revision_input["consistency_context"]) == {
        "1.1",
        "1.2",
        "1.3",
        "3.1.1",
        "3.1.3",
        "3.2",
    }


@pytest.mark.asyncio
async def test_final_review_omits_chapter_four_from_contract_when_plan_is_absent(
    tmp_path: Path,
) -> None:
    module_text = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    current = _edited(module_text, include_special_topics=False)
    active_sections = [
        section_id for section_id in FINAL_AUDIT_SECTION_IDS if section_id != "4"
    ]
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "chief-editor-auditor",
                "final_review_finding_submission",
                FinalReviewFindingSubmission(
                    checked_section_ids=active_sections,
                    findings=[],
                    residual_risks=[],
                ),
            ),
        ],
    )
    state = {
        "run_id": "run-no-special",
        "edited_report": current,
        "module_submissions": {
            module_id: _module(module_id) for module_id in REPORT_TAXONOMY
        },
        "cross_synthesis_inputs": [],
    }

    await run_final_review(
        runner,
        state,
        "workflow",
        chief_envelope=TaskEnvelope(
            task_id="chief-edit",
            run_id="run-no-special",
            agent_id="chief-editor",
            objective="总编",
            allowed_outputs=["edited_report_submission"],
        ),
        chief_session_key="chief-editor",
        approved_module_text=module_text,
        claims=[],
        aggregate_mode=True,
    )

    input_data = json.loads(
        (
            tmp_path
            / "Work/runs/run-no-special/reviews/final-review-input-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert input_data["required_section_ids"] == [
        "1.1", "1.2", "1.3", "3.1.1", "3.1.2", "3.1.3", "3.2"
    ]
    assert "\n## 4." not in input_data["canonical_markdown"]
    assert not (
        tmp_path
        / "Work/runs/run-no-special/reviews/final-review-chapter-4-input-r0.json"
    ).exists()
    assert state["final_review_completion_ref"].endswith("reviews/final-completion.json")


@pytest.mark.asyncio
async def test_final_review_rejects_chapter_four_finding_when_plan_is_absent(
    tmp_path: Path,
) -> None:
    module_text = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    current = _edited(module_text, include_special_topics=False)
    active_sections = [
        section_id for section_id in FINAL_AUDIT_SECTION_IDS if section_id != "4"
    ]
    with pytest.raises(Exception, match="invalid section"):
        FinalReviewFindingSubmission(
            checked_section_ids=active_sections,
            findings=[
                {
                    "id": "F-INACTIVE-4",
                    "target_section_ids": ["4"],
                    "target_changes": [
                        {
                            "target_section_id": "4",
                            "required_change": (
                                "不得为不存在的专项计划补写第四章内容，必须保持实际报告范围。"
                            ),
                            "reviewer_checks": ["确认报告保持没有第四章"],
                        }
                    ],
                    "category": "scope",
                    "impact": "blocking",
                    "observation": "审计结果错误地把未启用的第四章当成当前报告范围。",
                    "evidence_refs": [
                        "Work/runs/run-no-special/edited-revisions/chief-r0.json"
                    ],
                }
            ],
            residual_risks=[],
        )


def test_chief_patch_preserves_current_metadata_without_deleted_cross_fields() -> None:
    current = _edited(
        {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    ).model_copy(
        update={
            "photo_ids": ["P-001"],
            "unresolved_editorial_issues": ["保留的透明限制"],
        }
    )
    patch = ChiefRevisionSubmission(
        base_subject_ref="Work/runs/run-f/edited-revisions/chief-r0.json",
        revision=1,
        section_bodies={"3.1.2": "修订后的维度综合分析正文"},
        section_part_refs={
            "3.1.2": ("Work/runs/run-f/drafts/chief-edit-r1/r1/dimension_risk_analysis.md")
        },
        revision_responses=[
            RevisionResponse(
                finding_id="F-001",
                action="implemented",
                summary="已按最终审查要求修订跨模块分析，并完整保留所有未分配章节和元数据。",
                changed_target_ids=["3.1.2"],
            )
        ],
    )

    revised = _apply_chief_patch(
        current,
        patch,
        target_section_ids={"3.1.2"},
        required_finding_ids={"F-001"},
    )

    assert revised.dimension_risk_analysis == "修订后的维度综合分析正文"
    assert revised.risk_panorama == current.risk_panorama
    assert revised.module_narratives == current.module_narratives
    assert revised.photo_ids == ["P-001"]
    assert revised.unresolved_editorial_issues == ["保留的透明限制"]
    assert "synthesis_dispositions" not in revised.model_dump()
    assert "synthesis_tables" not in revised.model_dump()


@pytest.mark.asyncio
async def test_explicit_final_review_restart_preserves_failed_progress(
    tmp_path: Path,
) -> None:
    module_text = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    current = _edited(module_text)
    stale_progress_ref = "Work/runs/run-final-restart/reviews/final-progress.json"
    stale_progress = b'{"legacy_schema":true,"next_action":"revise"}\n'
    stale_path = tmp_path / stale_progress_ref
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(stale_progress)
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "chief-editor-auditor",
                "final_review_finding_submission",
                FinalReviewFindingSubmission(
                    checked_section_ids=list(FINAL_AUDIT_SECTION_IDS),
                    findings=[],
                    residual_risks=[],
                ),
            )
        ],
    )
    state = {
        "run_id": "run-final-restart",
        "resume": True,
        "final_review_restart_round": 1,
        "edited_report": current,
        "module_submissions": {module_id: _module(module_id) for module_id in REPORT_TAXONOMY},
        "cross_synthesis_inputs": [],
    }

    await run_final_review(
        runner,
        state,
        "workflow",
        chief_envelope=TaskEnvelope(
            task_id="chief-edit",
            run_id="run-final-restart",
            agent_id="chief-editor",
            objective="总编",
            allowed_outputs=["edited_report_submission"],
        ),
        chief_session_key="chief-editor",
        approved_module_text=module_text,
        claims=[],
        aggregate_mode=True,
    )

    assert stale_path.read_bytes() == stale_progress
    assert (tmp_path / "Work/runs/run-final-restart/reviews/final-progress-r1.json").is_file()
    assert (
        tmp_path
        / "Work/runs/run-final-restart/reviews/final-review-input-r1.json"
    ).is_file()
    assert state["final_review_completion_ref"].endswith("reviews/final-completion.json")


def test_resume_restores_latest_module_subjects_but_requires_fresh_reviews(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-resume"
    module = _module("2.1", revision=2)
    service.store.write_json(
        f"Work/runs/{run_id}/modules/2.1-r2.json",
        module.model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/reviews/cross-completion.json",
        {
            "review_kind": "cross",
            "subject_ref": f"Work/runs/{run_id}/modules/2.1-r2.json",
            "finding_ref": f"Work/runs/{run_id}/reviews/cross-findings.json",
            "response_refs": [],
            "verdict_ref": f"Work/runs/{run_id}/reviews/cross-verdict.json",
            "resolved_finding_ids": [],
        },
    )
    checkpoint = {
        "run_id": run_id,
        "status": "cancelled",
        "completed_modules": ["2.1"],
        "cross_review_completed": True,
        "final_review_completed": True,
    }
    state = {"run_id": run_id}
    runner._restore_resume_state(state, checkpoint)
    assert state["module_submissions"] == {}
    assert state["specialist_submissions"]["2.1"].revision == 2
    assert "cross_review_completion_ref" not in state
    assert "final_review_completion_ref" not in state


def test_delivery_artifacts_reference_current_final_review_completion() -> None:
    final_review_ref = "Work/runs/run-delivery/reviews/final-completion.json"

    artifacts = ReportWorkflowRunner._delivery_output_artifacts(
        final_review_ref=final_review_ref,
        delivery_manifest_ref=Path(
            "Work/runs/run-delivery/delivery/power-distribution-report-run-delivery/"
            "delivery-manifest.json"
        ),
        final_markdown_ref=Path("Outputs/Reports/配电安全专家咨询报告.md"),
        final_docx_ref=Path("Outputs/Reports/配电安全专家咨询报告.docx"),
        source_index_ref=Path("Outputs/Reports/证据与来源索引.md"),
        source_index_docx_ref=Path("Outputs/Reports/证据与来源索引.docx"),
    )

    review_artifacts = [artifact for artifact in artifacts if artifact.kind == "review"]
    assert [artifact.path.as_posix() for artifact in review_artifacts] == [final_review_ref]
    assert all(
        artifact.path.as_posix() != "Outputs/Reviews/full-review.json" for artifact in artifacts
    )
    assert any(
        artifact.path.as_posix()
        == "Outputs/Reports/证据与来源索引.docx"
        for artifact in artifacts
    )
    assert any(
        artifact.path.is_relative_to(Path("Outputs/Reports"))
        for artifact in artifacts
    )
    module_artifacts = {
        artifact.module_id: artifact.path
        for artifact in artifacts
        if artifact.kind == "module"
    }
    assert set(module_artifacts) == set(REPORT_TAXONOMY)
    assert all(path.is_relative_to(Path("Outputs/Modules")) for path in module_artifacts.values())


def test_restore_delivery_ignores_status_without_a_readable_receipt(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-business-delivery-restore"
    final_review_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
    expected = runner._delivery_output_artifacts(
        final_review_ref=final_review_ref,
        delivery_manifest_ref=Path(
            f"Work/runs/{run_id}/delivery/{run_id}-{run_id}/delivery-manifest.json"
        ),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/delivery-completion.json",
        {
            "run_id": run_id,
            "status": "delivered",
            "delivery_status": "delivered",
            "delivery_receipt_ref": f"Work/runs/{run_id}/missing-receipt.json",
            "report_version_id": "missing-version",
            "output_artifacts": [
                artifact.model_dump(mode="json") for artifact in expected
            ],
        },
    )

    state = {"run_id": run_id, "final_review_completion_ref": final_review_ref}
    service._republish_materialized_delivery = lambda _run_id: (_ for _ in ()).throw(
        ValueError("missing receipt")
    )
    runner._restore_delivery_completion(state)

    assert "delivery_restored" not in state
    assert "output_artifacts" not in state


def test_delivery_root_is_scoped_to_the_owning_run(tmp_path: Path) -> None:
    assert (
        ReportWorkflowRunner._delivery_root(tmp_path, "run-delivery")
        == tmp_path / "Work/runs/run-delivery/delivery"
    )


def test_restore_delivery_accepts_typed_declarations_after_package_validation(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-obsolete-review-output"
    delivery_dir = tmp_path / f"Work/runs/{run_id}/delivery" / f"power-distribution-report-{run_id}"
    modules_dir = delivery_dir / "modules"
    modules_dir.mkdir(parents=True)
    final_docx = delivery_dir / "report.docx"
    report_state = delivery_dir / "report-state.json"
    source_index = delivery_dir / "证据与来源索引.md"
    source_index_docx = delivery_dir / "证据与来源索引.docx"
    manifest = delivery_dir / "delivery-manifest.json"
    final_docx.write_bytes(b"docx")
    report_state.write_text("{}", encoding="utf-8")
    source_index.write_text("## 证据与来源索引", encoding="utf-8")
    source_index_docx.write_bytes(b"docx")
    manifest.write_text("{}", encoding="utf-8")
    module_files = {}
    for module_id in REPORT_TAXONOMY:
        path = modules_dir / f"{module_id}.md"
        path.write_text(f"# {module_id}", encoding="utf-8")
        module_files[module_id] = path
    hashes = {
        "final_docx": runner._sha256(final_docx),
        "report_state": runner._sha256(report_state),
        "source_index": runner._sha256(source_index),
        "source_index_docx": runner._sha256(source_index_docx),
        "manifest": runner._sha256(manifest),
        **{f"module:{module_id}": runner._sha256(path) for module_id, path in module_files.items()},
    }
    receipt_ref = f"Work/runs/{run_id}/delivery-receipt.json"
    service.store.write_json(
        receipt_ref,
        {
            "success": True,
            "delivery_dir": delivery_dir.as_posix(),
            "final_docx": final_docx.as_posix(),
            "module_files": {
                module_id: path.as_posix() for module_id, path in module_files.items()
            },
                "report_state": report_state.as_posix(),
                "source_index": source_index.as_posix(),
                "source_index_docx": source_index_docx.as_posix(),
            "manifest_path": manifest.as_posix(),
            "artifact_sha256": hashes,
        },
    )
    service.store.write_json(
        f"Work/runs/{run_id}/delivery-completion.json",
        {
            "run_id": run_id,
            "status": "completed",
            "delivery_receipt_ref": receipt_ref,
            "report_version_id": run_id,
            "output_artifacts": [
                {
                    "kind": "review",
                    "path": "Outputs/Reviews/full-review.json",
                    "module_id": None,
                }
            ],
        },
    )
    service.store.write_json(
        f"Work/report-versions/{run_id}/version.json",
        ReportVersion(
            version_id=run_id,
            run_id=run_id,
            artifact_refs={},
            skill_provenance=[],
            session_summary_refs=[],
        ).model_dump(mode="json"),
    )
    state = {
        "run_id": run_id,
        "final_review_completion_ref": (f"Work/runs/{run_id}/reviews/final-completion.json"),
    }
    service._republish_materialized_delivery = lambda _run_id: None

    runner._restore_delivery_completion(state)

    assert state["delivery_restored"] is True
    assert state["delivery_completion_ref"].endswith("delivery-completion.json")
    assert state["output_artifacts"][0].path == Path(
        "Outputs/Reviews/full-review.json"
    )


@pytest.mark.asyncio
async def test_module_resume_invalidates_parts_without_context_marker(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-marker-correction"
    module_id = "2.3"
    part_ids = list(REPORT_TAXONOMY[module_id].submodules)
    task_id = f"module-{module_id}"
    draft_root = tmp_path / f"Work/runs/{run_id}/drafts/{task_id}/r0"
    draft_root.mkdir(parents=True)
    for part_id in part_ids:
        (draft_root / f"{part_id}.md").write_text(
            f"### {part_id}\n\n尚未放置结构化 Claim 标记的正文。",
            encoding="utf-8",
        )
    raw_payload = {
        "kind": "module_submission",
        "module_id": module_id,
        "submodule_narratives": {
            part_id: {
                "artifact_refs": [f"Work/runs/{run_id}/drafts/{task_id}/r0/{part_id}.md"]
            }
            for part_id in part_ids
        },
        "claims": [
            {
                "id": f"C-{module_id}-{index:03d}",
                "module_id": module_id,
                "submodule_id": part_id,
                "text": "受当前项目证据支持的陈述。",
                "claim_type": "project_fact",
                "source_ids": ["E-0001"],
                "confidence": 1.0,
                "footnote_required": True,
                "unresolved": False,
            }
            for index, part_id in enumerate(part_ids, start=1)
        ],
        "source_ids": ["E-0001"],
        "unresolved_questions": [],
        "revision": 0,
        "revision_responses": [],
    }
    service.store.write_json(
        f"Work/runs/{run_id}/submissions/{task_id}/attempt-3-raw.json",
        {"raw_payload": raw_payload},
    )
    service.store.write_json(
        f"Work/runs/{run_id}/results/{task_id}.json",
        {"status": "failed"},
    )

    captured: dict[str, object] = {}

    async def stop_after_envelope(
        agent_id,
        envelope,
        artifacts,
        workflow_id,
        *,
        session_key=None,
    ):
        captured["agent_id"] = agent_id
        captured["envelope"] = envelope
        raise RuntimeError("captured correction envelope")

    runner._agent = stop_after_envelope
    planned = TaskEnvelope(
        task_id=f"module-{module_id}",
        run_id=run_id,
        agent_id=f"module-{module_id}-specialist",
        objective="完成模块正文和结构化提交。",
        constraints=[],
        allowed_outputs=["module_submission"],
        target_submodule_ids=part_ids,
    )
    knowledge_ref = f"Work/runs/{run_id}/knowledge/module-{module_id}.md"
    service.store.write_text(knowledge_ref, "# 当前模块知识\n")
    state = {
        "run_id": run_id,
        "resume": True,
        "request": SimpleNamespace(
            execution_requirements=[],
            missing_evidence_policy="draft",
            user_supplements=[],
        ),
        "module_dispatch": SimpleNamespace(module_tasks=[planned]),
        "preparation_refs": {
            "coverage": "Work/coverage.json",
            "evidence": "Work/evidence.jsonl",
            "manifest": "Work/manifest.json",
        },
        "module_knowledge_refs": {module_id: knowledge_ref},
        "template_skill_text": {},
    }

    with pytest.raises(RuntimeError, match="captured correction envelope"):
        await runner._module_pipeline(module_id, state, "workflow")

    envelope = captured["envelope"]
    assert isinstance(envelope, TaskEnvelope)
    assert envelope.revision == 1
    assert envelope.target_submodule_ids == sorted(part_ids)
    assert "search_project_evidence" in envelope.allowed_tools
    assert "search_reference_library" in envelope.allowed_tools
    assert "write_result_part" in envelope.allowed_tools
    assert "submit_result" in envelope.allowed_tools
    assert "query_peer" not in envelope.allowed_tools
    assert not any("/drafts/" in ref for ref in envelope.input_refs)
    assert not any("correction-state" in ref for ref in envelope.input_refs)
    input_contract = json.loads(
        (tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8")
    )
    assert input_contract["revision"] == 1
    assert input_contract["saved_part_ids"] == []
    assert input_contract["rewrite_part_ids"] == []
    assert "existing_part_refs" not in input_contract
    assert "pending_correction_ref" not in input_contract
    marker = json.loads(
        (
            tmp_path
                / f"Work/runs/{run_id}/drafts/{task_id}/r1/_authoring-context.json"
        ).read_text(encoding="utf-8")
    )
    assert marker["authoring_context_sha256"]


@pytest.mark.asyncio
async def test_partial_module_authoring_never_exposes_live_peer_tools(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-partial-no-peers"
    module_id = "2.1"
    planned = TaskEnvelope(
        task_id=f"module-{module_id}",
        run_id=run_id,
        agent_id=f"module-{module_id}-specialist",
        objective="完成部分模块正文。",
        allowed_outputs=["module_submission"],
        target_submodule_ids=list(REPORT_TAXONOMY[module_id].submodules),
    )
    captured: dict[str, TaskEnvelope] = {}

    async def author(
        _agent_id,
        envelope,
        _artifacts,
        _workflow_id,
        *,
        session_key=None,
    ):
        captured["envelope"] = envelope
        return _module(module_id)

    runner._agent = author
    state = {
        "run_id": run_id,
        "request": SimpleNamespace(
            execution_requirements=[],
            missing_evidence_policy="draft",
            user_supplements=[],
        ),
        "module_dispatch": SimpleNamespace(module_tasks=[planned]),
        "preparation_refs": {
            "coverage": "Work/coverage.json",
            "evidence": "Work/evidence.jsonl",
            "manifest": "Work/manifest.json",
        },
        "module_knowledge_refs": {
            module_id: f"Work/runs/{run_id}/knowledge/module-{module_id}.md"
        },
    }

    await runner._module_pipeline(
        module_id,
        state,
        "workflow-partial",
        review=False,
    )

    envelope = captured["envelope"]
    assert envelope.allowed_tools
    assert "query_peer" not in envelope.allowed_tools
    assert "reply_peer" not in envelope.allowed_tools
    assert any(
        "小节是文档 part，不是独立任务或会话" in item
        for item in envelope.constraints
    )


def test_resume_rejects_cross_completion_without_exact_five_owner_barrier(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-current-resume"
    modules = {module_id: _module(module_id) for module_id in REPORT_TAXONOMY}
    module_refs = []
    module_completion_refs = {}
    for module_id, module in modules.items():
        subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r0.json"
        module_refs.append(subject_ref)
        service.store.write_json(subject_ref, module.model_dump(mode="json"))
        finding_ref = f"Work/runs/{run_id}/reviews/module/initial/{module_id}/findings-r0.json"
        finding_payload = ModuleReviewFindingSubmission(
            coverage={
                "submodule_ids": list(REPORT_TAXONOMY[module_id].submodules),
            },
            findings=[],
        ).model_dump(mode="json")
        if module_id == "2.1":
            # Runtime-only locator metadata from the interrupted current run
            # must not invalidate an already closed semantic review.
            finding_payload["coverage"]["claim_ids"] = ["C-2.1-001"]
        service.store.write_json(finding_ref, finding_payload)
        completion_ref = f"Work/runs/{run_id}/reviews/module/initial/{module_id}/completion-r0.json"
        module_completion_refs[module_id] = completion_ref
        service.store.write_json(
            completion_ref,
            ReviewCompletionRecord(
                review_protocol_version=(2 if module_id == "2.1" else 1),
                lifecycle="module",
                run_id=run_id,
                reviewer_agent_id="evidence-auditor",
                    reviewer_session_key=f"module-auditor-{module_id}",
                subject_refs=[subject_ref],
                finding_refs=[finding_ref],
                verdict_refs=[],
                resolved_finding_ids=[],
                artifact_sha256=_artifact_hashes(tmp_path, [subject_ref, finding_ref]),
            ).model_dump(mode="json"),
        )

    cross_finding_ref = f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
    service.store.write_json(
        cross_finding_ref,
        CrossReviewFindingSubmission(
            coverage=[
                {
                    "module_id": module_id,
                    "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS),
                }
                for module_id in REPORT_TAXONOMY
            ],
            findings=[],
            synthesis_inputs=[],
        ).model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/reviews/cross-completion.json",
        ReviewCompletionRecord(
                lifecycle="cross",
                run_id=run_id,
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key="cross-module-reviewer-specialized-cohort",
            subject_refs=module_refs,
            finding_refs=[cross_finding_ref],
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_hashes(tmp_path, [*module_refs, cross_finding_ref]),
        ).model_dump(mode="json"),
    )

    edited_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    edited = _edited({module_id: module.markdown for module_id, module in modules.items()})
    service.store.write_json(edited_ref, edited.model_dump(mode="json"))
    final_finding_ref = f"Work/runs/{run_id}/reviews/final-findings-r0.json"
    service.store.write_json(
        final_finding_ref,
        FinalReviewFindingSubmission(
            checked_section_ids=list(FINAL_AUDIT_SECTION_IDS),
            findings=[],
            residual_risks=[],
        ).model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/reviews/final-completion.json",
        ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="chief-editor-auditor",
            subject_refs=[edited_ref],
            finding_refs=[final_finding_ref],
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_hashes(tmp_path, [edited_ref, final_finding_ref]),
        ).model_dump(mode="json"),
    )

    state = {"run_id": run_id}
    with pytest.raises(AgentWorkflowError, match="exact-five owner barrier"):
        runner._restore_resume_state(
            state,
            {
                "run_id": run_id,
                "status": "cancelled",
                "completed_modules": list(REPORT_TAXONOMY),
                "module_review_completion_refs": module_completion_refs,
            },
        )


def test_revision_resume_uses_checkpoint_module_review_refs(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-revision-resume"
    baseline = {
        module_id: _module(module_id)
        for module_id in REPORT_TAXONOMY
    }
    revised = _module("2.1", revision=1)
    subject_ref = f"Work/runs/{run_id}/modules/2.1-r1.json"
    finding_ref = (
        f"Work/runs/{run_id}/reviews/module/post-delivery/"
        "2.1/findings-r1.json"
    )
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/post-delivery/"
        "2.1/completion-r1.json"
    )
    service.store.write_json(
        subject_ref,
        revised.model_dump(mode="json"),
    )
    service.store.write_json(
        finding_ref,
        ModuleReviewFindingSubmission(
            coverage={
                "submodule_ids": list(
                    REPORT_TAXONOMY["2.1"].submodules
                )
            },
            findings=[],
        ).model_dump(mode="json"),
    )
    service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[subject_ref],
            finding_refs=[finding_ref],
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_hashes(
                tmp_path,
                [subject_ref, finding_ref],
            ),
        ).model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/workflow-state.json",
        {
            "run_id": run_id,
            "status": "waiting_user",
            "completed_revision_modules": ["2.1"],
            "module_review_completion_refs": {
                "2.1": completion_ref,
            },
        },
    )
    state = {
        "run_id": run_id,
        "module_submissions": dict(baseline),
    }

    runner._restore_revision_resume_state(state)

    assert state["completed_revision_modules"] == ["2.1"]
    assert state["module_submissions"]["2.1"].revision == 1
    assert state["module_review_completion_refs"]["2.1"] == completion_ref


def _chief_completion_fixture(
    tmp_path: Path,
    *,
    revision: bool = False,
) -> tuple[
    ReportWorkflowRunner,
    dict,
    str,
]:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    runner._require_template_skill = lambda _state: None
    run_id = "run-revision-chief-completion" if revision else "run-chief-completion"
    modules = {
        module_id: _module(module_id)
        for module_id in REPORT_TAXONOMY
    }
    for module_id, module in modules.items():
        service.store.write_json(
            f"Work/runs/{run_id}/modules/{module_id}-r0.json",
            module.model_dump(mode="json"),
        )
    cross_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
    cross_finding_ref = f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
    service.store.write_json(
        cross_finding_ref,
        CrossReviewFindingSubmission(
            coverage=[
                {
                    "module_id": module_id,
                    "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS),
                }
                for module_id in REPORT_TAXONOMY
            ],
            findings=[],
            synthesis_inputs=[],
        ).model_dump(mode="json"),
    )
    module_refs = [
        f"Work/runs/{run_id}/modules/{module_id}-r0.json"
        for module_id in REPORT_TAXONOMY
    ]
    service.store.write_json(
        cross_ref,
        ReviewCompletionRecord(
            lifecycle="cross",
            run_id=run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key="cross-module-reviewer",
            subject_refs=module_refs,
            finding_refs=[cross_finding_ref],
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_hashes(
                tmp_path,
                [*module_refs, cross_finding_ref],
            ),
        ).model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/ledgers/claims.json",
        {"claims": [], "sources": []},
    )
    request = ReportRequest(
        instruction=(
            "按本次局部修订整合报告。"
            if revision
            else "形成完整报告。"
        ),
        missing_evidence_policy="draft",
    )
    state = {
        "run_id": run_id,
        "request": request,
        "module_submissions": modules,
        "module_review_completion_refs": {},
        "cross_review_completion_ref": cross_ref,
    }
    if revision:
        state["revision_request"] = RevisionRequest(
            baseline_version_id="version-parent",
            feedback="只修订模块 2.1。",
            target_module_ids=["2.1"],
        )
        state["chief_editor_constraints"] = [
            "这是交付后局部修订：未获批准的模块正文必须逐字保持父版本内容",
            "只可更新输入合同授权的目标模块与固定综合章节字段",
        ]
    editor_input = runner._current_chief_editor_input(state)
    editor_input_ref = (
        f"Work/runs/{run_id}/context/chief-editor-input.json"
    )
    envelope = TaskEnvelope(
        task_id="chief-edit",
        run_id=run_id,
        agent_id="chief-editor",
        objective="整合已批准五模块。",
        input_refs=[
            editor_input_ref,
        ],
        constraints=list(
            state.get("chief_editor_constraints", [])
        ),
        allowed_outputs=["edited_report_submission"],
        allowed_tools=[
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
        input_contract_kind="chief_editor_input",
        input_contract_ref=editor_input_ref,
        inline_context="",
    )
    candidate = _edited(
        {
            module_id: module.markdown
            for module_id, module in modules.items()
        },
        include_special_topics=False,
    )
    service.store.write_json(
        editor_input_ref,
        editor_input.model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/context/chief-editor-envelope.json",
        envelope.model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/edited-revisions/chief-r0.json",
        candidate.model_dump(mode="json"),
    )
    completion_ref = runner._write_chief_editor_completion(
        state,
        editor_input=editor_input,
        envelope=envelope,
    )
    return runner, state, completion_ref


@pytest.mark.parametrize("revision", [False, True])
def test_chief_completion_restores_hash_bound_current_context(
    tmp_path: Path,
    revision: bool,
) -> None:
    runner, state, completion_ref = _chief_completion_fixture(
        tmp_path,
        revision=revision,
    )

    candidate, envelope, refs = runner._load_chief_editor_completion(
        state,
        completion_ref,
    )

    assert candidate.title == "示例配电安全专家咨询报告"
    assert envelope.run_id == state["run_id"]
    assert refs["editor_input"].endswith("chief-editor-input.json")


@pytest.mark.parametrize("revision", [False, True])
def test_chief_envelope_is_pack_bound_and_has_no_raw_evidence_or_photo_inputs(
    tmp_path: Path,
    revision: bool,
) -> None:
    runner, state, _completion_ref = _chief_completion_fixture(
        tmp_path,
        revision=revision,
    )
    envelope = TaskEnvelope.model_validate_json(
        (
            runner.service.workspace
            / f"Work/runs/{state['run_id']}/context/chief-editor-envelope.json"
        ).read_text(encoding="utf-8")
    )
    assert envelope.input_refs == [
        f"Work/runs/{state['run_id']}/context/chief-editor-input.json"
    ]
    assert envelope.allowed_tools == [
        "write_result_part",
        "list_result_parts",
        "submit_result",
    ]
    assert all("evidence" not in ref and "photo" not in ref for ref in envelope.input_refs)
    editor_input = ChiefEditorInput.model_validate_json(
        (
            runner.service.workspace
            / envelope.input_contract_ref
        ).read_text(encoding="utf-8")
    )
    assert editor_input.cross_decision_pack_ref == state["cross_decision_pack_ref"]
    assert "cross_decision_pack_sha256" not in editor_input.model_dump()
    assert editor_input.cross_decision is not None


def test_chief_pack_accepts_closed_ordinary_xmr_cross_finding(
    tmp_path: Path,
) -> None:
    """Ordinary Cross ids must never be reinterpreted as removed IF reroutes."""

    runner, state, _completion_ref = _chief_completion_fixture(tmp_path)
    run_id = state["run_id"]
    pack_ref = state.pop("cross_decision_pack_ref")
    state.pop("cross_decision_pack", None)
    state.pop("cross_decision_pack_sha256", None)
    (runner.service.workspace / pack_ref).unlink()

    finding_ref = f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
    verdict_ref = f"Work/runs/{run_id}/reviews/cross-verdicts-r1.json"
    coverage = [
        {
            "module_id": module_id,
            "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS),
        }
        for module_id in REPORT_TAXONOMY
    ]
    finding = {
        "id": "XMR-001",
        "owner_module_id": "2.3",
        "target_submodule_ids": ["2.3.1"],
        "related_module_ids": ["2.5"],
        "category": "dependencies",
        "impact": "blocking",
        "observation": "模块 2.3 尚未说明保护操作对模块 2.5 管理规程的前置依赖，导致联合行动无法排序。",
        "evidence_refs": ["E-0001"],
        "required_change": "在模块 2.3 写明保护操作依赖模块 2.5 的管理规程，并给出实施顺序和联合验收要求。",
        "reviewer_checks": ["依赖、顺序和联合验收均已写入责任模块"],
    }
    runner.service.store.write_json(
        finding_ref,
        CrossReviewFindingSubmission(
            coverage=coverage,
            findings=[finding],
            synthesis_inputs=[],
        ).model_dump(mode="json"),
    )
    runner.service.store.write_json(
        verdict_ref,
        CrossReviewVerdictSubmission(
            coverage=coverage,
            verdicts=[
                {
                    "finding_id": "XMR-001",
                    "verdict": "resolved",
                    "reason": "责任模块已补充依赖、顺序和联合验收要求。",
                    "evidence_refs": [f"Work/runs/{run_id}/modules/2.3-r0.json"],
                }
            ],
            new_findings=[],
            synthesis_inputs=[],
        ).model_dump(mode="json"),
    )
    completion_ref = state["cross_review_completion_ref"]
    module_refs = [
        f"Work/runs/{run_id}/modules/{module_id}-r0.json"
        for module_id in REPORT_TAXONOMY
    ]
    runner.service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="cross",
            run_id=run_id,
            reviewer_agent_id="cross-module-reviewer",
            reviewer_session_key="cross-module-reviewer",
            subject_refs=module_refs,
            finding_refs=[finding_ref],
            verdict_refs=[verdict_ref],
            resolved_finding_ids=["XMR-001"],
            artifact_sha256=_artifact_hashes(
                tmp_path, [*module_refs, finding_ref, verdict_ref]
            ),
        ).model_dump(mode="json"),
    )

    pack = runner._materialize_chief_cross_decision_pack(state)

    assert pack.cross_review_completion_ref == completion_ref
    assert "artifact_sha256" not in pack.model_dump()
    assert "pack_sha256" not in pack.model_dump()
    assert "if_closures" not in pack.model_dump()
    assert "xmr_verdicts" not in pack.model_dump()


def test_revision_checkpoint_refuses_chief_without_cross_owner_barrier(
    tmp_path: Path,
) -> None:
    runner, state, completion_ref = _chief_completion_fixture(
        tmp_path,
        revision=True,
    )
    state["chief_editor_completion_ref"] = completion_ref
    runner._budget = None
    runner._revision_checkpoint(
        state,
        "revision-chief-edit",
        "completed",
    )
    checkpoint = json.loads(
        (
            runner.service.workspace
            / f"Work/runs/{state['run_id']}/workflow-state.json"
        ).read_text(encoding="utf-8")
    )
    assert checkpoint["chief_editor_completion_ref"] == completion_ref
    runner._load_current_review_completion = (
        lambda **_kwargs: (SimpleNamespace(), [])
    )

    with pytest.raises(AgentWorkflowError, match="exact-five owner barrier"):
        runner._restore_revision_resume_state(state)


def test_chief_completion_rejects_resealed_wrong_envelope_identity(
    tmp_path: Path,
) -> None:
    runner, state, completion_ref = _chief_completion_fixture(tmp_path)
    envelope_ref = (
        f"Work/runs/{state['run_id']}/context/"
        "chief-editor-envelope.json"
    )
    envelope = json.loads(
        (tmp_path / envelope_ref).read_text(encoding="utf-8")
    )
    envelope["run_id"] = "another-run"
    runner.service.store.write_json(envelope_ref, envelope)

    with pytest.raises(
        AgentWorkflowError,
        match="envelope identity",
    ):
        runner._load_chief_editor_completion(state, completion_ref)


def test_chief_completion_rejects_changed_input_or_supplement(
    tmp_path: Path,
) -> None:
    runner, state, completion_ref = _chief_completion_fixture(tmp_path)
    state["request"] = state["request"].model_copy(
        update={
            "user_supplements": [
                UserSupplement(
                    id="US-CHIEF-001",
                    content="总编必须按新的管理边界重排优先级。",
                    stages=["chief_edit"],
                )
            ]
        }
    )

    with pytest.raises(
        AgentWorkflowError,
        match="current request|request constraints",
    ):
        runner._load_chief_editor_completion(state, completion_ref)

    runner, state, completion_ref = _chief_completion_fixture(
        tmp_path / "changed-input"
    )
    input_ref = (
        f"Work/runs/{state['run_id']}/context/"
        "chief-editor-input.json"
    )
    changed_input = json.loads(
        (
            runner.service.workspace / input_ref
        ).read_text(encoding="utf-8")
    )
    changed_input["run_id"] = "another-run"
    runner.service.store.write_json(input_ref, changed_input)

    with pytest.raises(
        AgentWorkflowError,
        match="another run|does not match current approved modules",
    ):
        runner._load_chief_editor_completion(state, completion_ref)


def test_aggregate_chief_completion_restores_typed_current_run_candidate(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-aggregate-chief-resume"
    source_modules = {
        module_id: _module(module_id).markdown
        for module_id in REPORT_TAXONOMY
    }
    source_manifest_ref = (
        f"Work/runs/{run_id}/context/aggregate-source-manifest.json"
    )
    editor_input_ref = (
        f"Work/runs/{run_id}/context/aggregate-editor-input.json"
    )
    service.store.write_json(
        source_manifest_ref,
        {"module_refs": sorted(source_modules)},
    )
    service.store.write_json(
        editor_input_ref,
        {"run_id": run_id, "source_format": "markdown"},
    )
    envelope = TaskEnvelope(
        task_id="aggregate-existing",
        run_id=run_id,
        agent_id="chief-editor",
        objective="汇总已有模块",
        input_refs=[editor_input_ref],
        allowed_outputs=["edited_report_submission"],
        input_contract_kind="aggregate_editor_input",
        input_contract_ref=editor_input_ref,
    )
    candidate = _edited(source_modules)
    state = {"run_id": run_id, "resume": True}

    runner._write_aggregate_chief_completion(
        state,
        envelope,
        candidate,
    )

    assert (
        runner._load_aggregate_chief_completion(
            state,
            envelope,
            source_modules,
            [],
        )
        == candidate
    )
    completion_ref = f"Work/runs/{run_id}/aggregate-chief-completion.json"
    completion = json.loads(
        (tmp_path / completion_ref).read_text(encoding="utf-8")
    )
    assert "artifact_sha256" not in completion
    service.store.write_json(
        editor_input_ref,
        {"run_id": run_id, "source_format": "changed"},
    )
    assert (
        runner._load_aggregate_chief_completion(
            state,
            envelope,
            source_modules,
            [],
        )
        is None
    )


def test_cross_decision_pack_resume_ignores_projection_and_archival_hashes(
    tmp_path: Path,
) -> None:
    runner, state, _completion_ref = _chief_completion_fixture(tmp_path)
    pack_ref = state["cross_decision_pack_ref"]
    state.pop("cross_decision_pack", None)
    state["cross_decision_pack_sha256"] = "not-a-hash"
    pack_payload = json.loads(
        (tmp_path / pack_ref).read_text(encoding="utf-8")
    )
    pack_payload["pack_sha256"] = "not-a-hash"
    pack_payload["artifact_sha256"] = {
        state["cross_review_completion_ref"]: "not-a-hash"
    }
    runner.service.store.write_json(pack_ref, pack_payload)
    completion_ref = state["cross_review_completion_ref"]
    completion_payload = json.loads(
        (tmp_path / completion_ref).read_text(encoding="utf-8")
    )
    completion_payload["artifact_sha256"] = {
        state["cross_review_completion_ref"]: "not-a-hash"
    }
    runner.service.store.write_json(completion_ref, completion_payload)

    restored = runner._load_current_cross_decision_pack(state)

    assert restored is not None
    assert restored.run_id == state["run_id"]


@pytest.mark.parametrize(
    ("lifecycle", "reviewer_agent_id", "reviewer_session_key"),
    [
        ("module", "evidence-auditor", "module-auditor-2.4"),
        ("cross", "cross-module-reviewer", "cross-module-reviewer"),
        ("final", "chief-editor-auditor", "chief-editor-auditor"),
    ],
)
def test_review_completion_replays_canonical_regression_findings(
    tmp_path: Path,
    lifecycle: str,
    reviewer_agent_id: str,
    reviewer_session_key: str,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = f"run-{lifecycle}-regression-resume"
    subject_ref = f"Work/runs/{run_id}/subjects/current.json"
    service.store.write_json(subject_ref, {"kind": "subject"})

    initial_id = f"{lifecycle.upper()}-001"
    regression_id = f"{lifecycle.upper()}-002"
    initial_finding = {"id": initial_id, "marker": "initial"}
    regression_finding = {"id": regression_id, "marker": "regression"}
    finding_kind = f"{lifecycle}_review_finding_submission"
    verdict_kind = f"{lifecycle}_review_verdict_submission"
    initial_ref = f"Work/runs/{run_id}/reviews/initial.json"
    regression_ref = f"Work/runs/{run_id}/reviews/regression.json"
    first_verdict_ref = f"Work/runs/{run_id}/reviews/verdict-r1.json"
    final_verdict_ref = f"Work/runs/{run_id}/reviews/verdict-r2.json"
    completion_ref = f"Work/runs/{run_id}/reviews/completion.json"

    service.store.write_json(
        initial_ref,
        {"kind": finding_kind, "findings": [initial_finding]},
    )
    service.store.write_json(
        regression_ref,
        {"kind": finding_kind, "findings": [regression_finding]},
    )
    service.store.write_json(
        first_verdict_ref,
        {
            "kind": verdict_kind,
            "verdicts": [{"finding_id": initial_id}],
            "new_findings": [regression_finding],
        },
    )
    service.store.write_json(
        final_verdict_ref,
        {
            "kind": verdict_kind,
            "verdicts": [{"finding_id": regression_id}],
            "new_findings": [],
        },
    )
    service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle=lifecycle,
            run_id=run_id,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_session_key=reviewer_session_key,
            subject_refs=[subject_ref],
            finding_refs=[initial_ref, regression_ref],
            verdict_refs=[first_verdict_ref, final_verdict_ref],
            resolved_finding_ids=[initial_id, regression_id],
            artifact_sha256=_artifact_hashes(
                tmp_path,
                [
                    subject_ref,
                    initial_ref,
                    regression_ref,
                    first_verdict_ref,
                    final_verdict_ref,
                ],
            ),
        ).model_dump(mode="json"),
    )

    completion, artifacts = runner._load_current_review_completion(
        run_id=run_id,
        completion_ref=completion_ref,
        lifecycle=lifecycle,
        reviewer_agent_id=reviewer_agent_id,
        reviewer_session_key=reviewer_session_key,
        subject_refs=[subject_ref],
    )

    assert completion.resolved_finding_ids == [initial_id, regression_id]
    assert len(artifacts) == 4


def test_review_completion_rejects_regression_copy_that_differs_from_verdict(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-regression-mismatch"
    subject_ref = f"Work/runs/{run_id}/subjects/current.json"
    initial_ref = f"Work/runs/{run_id}/reviews/initial.json"
    regression_ref = f"Work/runs/{run_id}/reviews/regression.json"
    verdict_ref = f"Work/runs/{run_id}/reviews/verdict.json"
    completion_ref = f"Work/runs/{run_id}/reviews/completion.json"
    service.store.write_json(subject_ref, {"kind": "subject"})
    service.store.write_json(
        initial_ref,
        {"kind": "module_review_finding_submission", "findings": []},
    )
    service.store.write_json(
        regression_ref,
        {
            "kind": "module_review_finding_submission",
            "findings": [{"id": "M-001", "marker": "persisted"}],
        },
    )
    service.store.write_json(
        verdict_ref,
        {
            "kind": "module_review_verdict_submission",
            "verdicts": [{"finding_id": "M-001"}],
            "new_findings": [{"id": "M-001", "marker": "different"}],
        },
    )
    service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.4",
            subject_refs=[subject_ref],
            finding_refs=[initial_ref, regression_ref],
            verdict_refs=[verdict_ref],
            resolved_finding_ids=["M-001"],
            artifact_sha256=_artifact_hashes(
                tmp_path,
                [subject_ref, initial_ref, regression_ref, verdict_ref],
            ),
        ).model_dump(mode="json"),
    )

    with pytest.raises(ValueError, match="regression findings differ"):
        runner._load_current_review_completion(
            run_id=run_id,
            completion_ref=completion_ref,
            lifecycle="module",
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.4",
            subject_refs=[subject_ref],
        )


def test_review_completion_ignores_forensic_hash_metadata_after_closure(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-review-hash"
    subject_ref = f"Work/runs/{run_id}/subjects/current.json"
    finding_ref = f"Work/runs/{run_id}/reviews/findings.json"
    completion_ref = f"Work/runs/{run_id}/reviews/completion.json"
    service.store.write_json(subject_ref, {"kind": "subject"})
    service.store.write_json(
        finding_ref,
        {"kind": "module_review_finding_submission", "findings": []},
    )
    service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.4-initial",
            subject_refs=[subject_ref],
            finding_refs=[finding_ref],
            verdict_refs=[],
            resolved_finding_ids=[],
            artifact_sha256=_artifact_hashes(tmp_path, [subject_ref, finding_ref]),
        ).model_dump(mode="json"),
    )
    completion = json.loads(
        (tmp_path / completion_ref).read_text(encoding="utf-8")
    )
    completion["artifact_sha256"] = {subject_ref: "not-a-hash"}
    service.store.write_json(completion_ref, completion)

    record, findings = runner._load_current_review_completion(
        run_id=run_id,
        completion_ref=completion_ref,
        lifecycle="module",
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key="module-auditor-2.4-initial",
        subject_refs=[subject_ref],
    )
    assert record.run_id == run_id
    assert findings == [
        {"kind": "module_review_finding_submission", "findings": []}
    ]


def test_canonical_markdown_uses_only_current_fixed_sections() -> None:
    modules = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    markdown = ReportWorkflowRunner._canonical_markdown(_edited(modules))
    assert "### 1.1 评估背景" in markdown
    assert "#### 2.4.1 配置与选型问题" in markdown
    assert "##### 2.4.1.1 额定/分断能力" in markdown
    assert "#### 3.1.3 跨领域关联风险" not in markdown
    assert "#### 3.1.3 数据缺口分析" in markdown
    assert "### 4.1 动态专项问题" in markdown

    legacy_structure = markdown.replace(
        "#### 3.1.3 数据缺口分析",
        "#### 3.1.3 跨领域关联风险\n\n旧模块正文\n\n#### 3.1.4 数据缺口分析",
    )
    with pytest.raises(ValueError, match="unexpected numbered headings"):
        validate_final_report_markdown(legacy_structure, _special_topic_plan())


def test_canonical_markdown_omits_chapter_four_without_special_topic_plan() -> None:
    modules = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}

    markdown = ReportWorkflowRunner._canonical_markdown(
        _edited(modules, include_special_topics=False)
    )

    assert "## 4. 专项问题分析" not in markdown
    assert "### 4." not in markdown
    validate_final_report_markdown(markdown, None)

    with pytest.raises(ValueError, match="Chapter 4 must be absent"):
        validate_final_report_markdown(
            markdown + "\n## 4. 专项问题分析\n\n### 4.1 擅自增加\n\n正文足够长以触发结构检查。",
            None,
        )


def test_complete_identity_skill_is_injected_without_runtime_slicing() -> None:
    state = {
        "template_skill_text": {
            "author-2.1": "AUTHOR-2.1-COMPLETE",
            "auditor-2.1": "AUDITOR-2.1-COMPLETE",
            "chief-editor-chapter-1": "CHIEF-CHAPTER-1-COMPLETE",
            "chief-editor-chapter-3": "CHIEF-CHAPTER-3-COMPLETE",
            "chief-editor-chapter-4": "CHIEF-CHAPTER-4-COMPLETE",
            "final-auditor": "FINAL-AUDITOR-COMPLETE",
        }
    }

    final_context = ReportWorkflowRunner._template_skill_context(state, "final-auditor")
    assert "FINAL-AUDITOR-COMPLETE" in final_context
    assert "AUTHOR-2.1" not in final_context
    assert 'id="final-auditor"' in final_context

    chapter_context = ReportWorkflowRunner._chief_template_skill_context(
        state, ("3",)
    )
    assert "CHIEF-CHAPTER-3-COMPLETE" in chapter_context
    assert "CHIEF-CHAPTER-1" not in chapter_context
    assert "CHIEF-CHAPTER-4" not in chapter_context
    assert 'id="chief-editor-chapter-3"' in chapter_context


def test_canonical_markdown_places_structured_tables_once(
    tmp_path: Path,
) -> None:
    modules = {module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY}
    edited = _edited(modules).model_copy(
        update={
            "risk_panorama": (
                "风险全景正文保留。\n\n"
                "**表：风险簇矩阵**\n\n"
                "| 风险簇 | 旧列 |\n"
                "| --- | --- |\n"
                "| 内嵌旧风险行 | 不应交付 |"
            ),
            "improvement_action_plan": (
                "行动计划正文保留。\n\n"
                "**表：行动依赖矩阵**\n\n"
                "| 行动 | 旧列 |\n"
                "| --- | --- |\n"
                "| 内嵌旧行动行 | 不应交付 |"
            ),
            "tables": [
                TableSubmission(
                    title="风险簇矩阵",
                    headers=["风险簇", "共同根因", "传播能力", "综合输入"],
                    rows=[["结构化风险行", "共同失效边界", "跨模块传播", "SI-001"]],
                    source_ids=["E-0001"],
                    claim_ids=["C-2.1-001"],
                ),
                TableSubmission(
                    title="行动依赖矩阵",
                    headers=["行动", "前置依赖", "综合输入", "验收标准"],
                    rows=[["结构化行动行", "先完成边界确认", "SI-001", "闭环"]],
                    source_ids=["E-0001"],
                    claim_ids=["C-2.1-001"],
                ),
            ],
        }
    )

    markdown = ReportWorkflowRunner._canonical_markdown(edited)

    assert markdown.count("\n风险簇矩阵（来源：E-0001）\n") == 1
    assert markdown.count("\n行动依赖矩阵（来源：E-0001）\n") == 1
    assert "内嵌旧风险行" not in markdown
    assert "内嵌旧行动行" not in markdown
    assert "| 风险簇 | 共同根因 | 传播能力 | 综合输入 |" in markdown
    assert "| 行动 | 前置依赖 | 综合输入 | 验收标准 |" in markdown
    assert markdown.index("## 4. 专项问题分析") < markdown.index("结构化风险行")
    assert markdown.index("结构化风险行") < markdown.index("结构化行动行")

    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _FakeService(tmp_path)
    state = {
        "run_id": "run-table-gate",
        "edited_report": edited,
    }
    runner._validate_final_report_structure(
        state,
        markdown,
        "chief-candidate-r0",
    )
    validation = ValidationReport.model_validate_json(
        (
            runner.service.workspace
            / "Work/runs/run-table-gate/reviews/"
            "report-integrity-chief-candidate-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert validation.validation_protocol_version == 2
    assert validation.subject_revision == 0
    assert validation.passed


def test_review_protocol_rejects_verdicts_that_guess_missing_finding_ids() -> None:
    from manyselves.core.reporting.review_lifecycle import _validate_verdicts

    with pytest.raises(ReviewLifecycleError, match="cover exactly"):
        _validate_verdicts(
            [
                ResolutionVerdict(
                    finding_id="M-OTHER",
                    verdict="open",
                    reason="这个 verdict 故意没有对应 required finding，用于验证合同拒绝。",
                    evidence_refs=[],
                )
            ],
            {"M-001"},
        )


def test_validation_binding_uses_typed_identity_not_subject_hash(tmp_path: Path) -> None:
    runner = _ScriptedRunner(tmp_path, [])
    subject_ref = "Work/runs/run-stale/modules/2.1-r1.json"
    runner.service.store.write_text(subject_ref, "original")
    report = ValidationReport(
        validation_protocol_version=2,
        run_id="run-stale",
        subject_ref=subject_ref,
        subject_revision=1,
        validator="test/v2",
        check_ids=["content"],
        passed=True,
    )
    runner.service.store.write_text(subject_ref, "changed-after-validation")

    _require_validation_binding(
        runner,
        report,
        subject_ref=subject_ref,
        subject_revision=1,
    )


def test_module_lane_recovery_rejects_completion_for_prior_revision(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _FakeService(tmp_path)
    run_id = "run-module-binding"
    current_ref = f"Work/runs/{run_id}/modules/2.1-r1.json"
    prior_ref = f"Work/runs/{run_id}/modules/2.1-r0.json"
    module = ModuleSubmission(
        module_id="2.1",
        submodule_narratives={
            submodule_id: "module body"
            for submodule_id in REPORT_TAXONOMY["2.1"].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=1,
    )
    runner.service.store.write_json(current_ref, module.model_dump(mode="json"))
    runner.service.store.write_json(
        prior_ref,
        module.model_copy(update={"revision": 0}).model_dump(mode="json"),
    )
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r1.json"
    )
    runner.service.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[prior_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )

    with pytest.raises(AgentWorkflowError, match="does not bind current subject"):
        runner._validate_module_review_completion_binding(
            run_id=run_id,
            module_id="2.1",
            subject_ref=current_ref,
            review_ref=completion_ref,
        )
