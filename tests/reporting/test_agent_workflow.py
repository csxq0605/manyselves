from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    FINAL_AUDIT_SECTION_IDS,
    FINAL_REPORT_SECTION_IDS,
    AgentResult,
    ChiefRevisionSubmission,
    ClaimRecord,
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
from manyselves.core.reporting.input_contracts import (
    ReviewCompletionRecord,
    ValidationReport,
)
from manyselves.core.reporting.assets import validate_final_report_markdown
from manyselves.core.reporting.models import (
    CoverageMatrix,
    ProjectManifest,
    ReportRequest,
    RevisionRequest,
    SpecialTopicPlan,
    UserSupplement,
)
from manyselves.core.reporting.prompts import PromptAssembler
from manyselves.core.reporting.review_lifecycle import (
    ReviewLifecycleError,
    _apply_chief_patch,
    _require_validation_binding,
    run_cross_review,
    run_final_review,
    run_module_review,
)
from manyselves.core.reporting.source_ledger import SourceLedger
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.versions import ReportVersion
from manyselves.core.reporting.workflow import (
    AgentWorkflowError,
    FullReportCheckpoint,
    ReportWorkflowRunner,
)
from manyselves.core.tools.reporting_collaboration_tools import SubmitResultTool


def _artifact_hashes(root: Path, refs: list[str]) -> dict[str, str]:
    return {ref: hashlib.sha256((root / ref).read_bytes()).hexdigest() for ref in refs}


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

    async def _agent(
        self,
        agent_id,
        envelope,
        artifacts,
        workflow_id,
        *,
        session_key=None,
    ):
        expected_agent, expected_output, result = self.scripted.pop(0)
        assert agent_id == expected_agent
        assert envelope.allowed_outputs == [expected_output]
        self.calls.append((agent_id, expected_output, session_key))
        self.envelopes.append(envelope)
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
                content_sha256=hashlib.sha256(
                    (self.service.workspace / subject_ref).read_bytes()
                ).hexdigest(),
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
    def _role_skill_context(state, role, **kwargs):
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
                content_sha256=hashlib.sha256(
                    (self.service.workspace / subject_ref).read_bytes()
                ).hexdigest(),
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
        outcome = await tool(payload=payload)
        assert outcome["status"] == "completed", outcome
        result = AgentResult.model_validate_json(
            (self.service.workspace / outcome["result_path"]).read_text(encoding="utf-8")
        )
        assert result.payload is not None
        return result.payload


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
    runner._restore_preparation_snapshot(restored)
    assert restored["project_manifest"].files == []

    (tmp_path / state["preparation_refs"]["coverage"]).write_text(
        '{"entries": {"tampered": {}}}', encoding="utf-8"
    )
    with pytest.raises(Exception, match="hash mismatch"):
        runner._restore_preparation_snapshot(restored)


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
    assert any(
        "revision_responses 必须为空" in constraint
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
    assert "TARGET-RECHECK-KNOWLEDGE" in recheck["knowledge_context"]
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
async def test_module_review_resume_reuses_legacy_lifecycle_identity(
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

    assert runner.calls[0][2] == "module-auditor-2.1-initial"
    completion = ReviewCompletionRecord.model_validate_json(
        (
            tmp_path
            / "Work/runs/run-legacy-review/reviews/module/initial/2.1/completion-r0.json"
        ).read_text(encoding="utf-8")
    )
    assert completion.review_protocol_version == 2
    assert completion.reviewer_session_key == "module-auditor-2.1-initial"


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
async def test_cross_finding_is_closed_by_cross_reviewer_not_module_auditor(
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
        "machine_checks": [],
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
        "cross-module-reviewer",
        "module-2.3-specialist",
        "evidence-auditor",
        "module-2.3-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
    ]
    cross_sessions = [call[2] for call in runner.calls if call[0] == "cross-module-reviewer"]
    assert cross_sessions == ["cross-module-reviewer", "cross-module-reviewer"]
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
    assert "TARGET-KNOWLEDGE" in local_input["knowledge_context"]
    assert "UNRELATED-KNOWLEDGE" not in local_input["knowledge_context"]
    assert local_input["revision_diff"]["changed_submodule_narratives"] == [target]
    recheck_input = json.loads(
        (tmp_path / "Work/runs/run-x/reviews/cross-review-input-r1.json").read_text(
            encoding="utf-8"
        )
    )
    assert recheck_input["changed_module_ids"] == ["2.3"]
    assert set(recheck_input["modules"]) == {"2.3"}
    assert set(recheck_input["unchanged_module_sha256"]) == {
        "2.1",
        "2.2",
        "2.4",
        "2.5",
    }
    [machine_report] = recheck_input["machine_validation_reports"]
    assert machine_report["validation_protocol_version"] == 2
    assert machine_report["subject_ref"].endswith("/modules/2.3-r2.json")
    assert machine_report["subject_revision"] == 2
    assert machine_report["content_sha256"] == hashlib.sha256(
        (tmp_path / machine_report["subject_ref"]).read_bytes()
    ).hexdigest()
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
    assert final_input["required_section_ids"] == list(FINAL_AUDIT_SECTION_IDS)
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
        == set(FINAL_AUDIT_SECTION_IDS) - {"3.1.2"}
    )
    assert len(recheck_input["subject_metadata_sha256"]) == 64
    auditor_sessions = [call[2] for call in runner.calls if call[0] == "chief-editor-auditor"]
    assert auditor_sessions == [
        "chief-editor-auditor",
        "chief-editor-auditor",
    ]
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
    assert final_audit_envelopes[1].artifact_delivery_modes[
        final_audit_envelopes[1].prior_result_ref
    ] == "hash_retained"
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
    assert input_data["required_section_ids"] == active_sections
    assert "\n## 4." not in input_data["canonical_markdown"]
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
    runner = _ScriptedRunner(
        tmp_path,
        [
            (
                "chief-editor-auditor",
                "final_review_finding_submission",
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

    with pytest.raises(ReviewLifecycleError, match="inactive report section"):
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
    assert (tmp_path / "Work/runs/run-final-restart/reviews/final-review-input-r1.json").is_file()
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
    )

    review_artifacts = [artifact for artifact in artifacts if artifact.kind == "review"]
    assert [artifact.path.as_posix() for artifact in review_artifacts] == [final_review_ref]
    assert all(
        artifact.path.as_posix() != "Outputs/Reviews/full-review.json" for artifact in artifacts
    )
    assert any(
        artifact.path.as_posix() == "Outputs/Reports/证据与来源索引.docx"
        for artifact in artifacts
    )


def test_delivery_root_is_scoped_to_the_owning_run(tmp_path: Path) -> None:
    assert (
        ReportWorkflowRunner._delivery_root(tmp_path, "run-delivery")
        == tmp_path / "Work/runs/run-delivery/delivery"
    )


def test_restore_delivery_rejects_obsolete_review_output_declaration(
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

    runner._restore_delivery_completion(state)

    assert "delivery_completion_ref" not in state
    assert "output_artifacts" not in state


@pytest.mark.asyncio
async def test_module_resume_invalidates_legacy_parts_without_context_fingerprint(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-marker-correction"
    module_id = "2.3"
    part_ids = list(REPORT_TAXONOMY[module_id].submodules)
    draft_root = tmp_path / f"Work/runs/{run_id}/drafts/module-{module_id}/r0"
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
                "artifact_refs": [f"Work/runs/{run_id}/drafts/module-{module_id}/r0/{part_id}.md"]
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
        f"Work/runs/{run_id}/submissions/module-{module_id}/attempt-3-raw.json",
        {"raw_payload": raw_payload},
    )
    service.store.write_json(
        f"Work/runs/{run_id}/results/module-{module_id}.json",
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
            / f"Work/runs/{run_id}/drafts/module-{module_id}/r1/_authoring-context.json"
        ).read_text(encoding="utf-8")
    )
    assert marker["authoring_context_sha256"]


def test_resume_restores_exact_current_protocol_review_completions(
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
                reviewer_session_key=(
                    f"module-auditor-{module_id}"
                    if module_id == "2.1"
                    else f"module-auditor-{module_id}-initial"
                ),
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
            reviewer_session_key="cross-module-reviewer",
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
            checked_section_ids=list(FINAL_REPORT_SECTION_IDS),
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
    runner._restore_resume_state(
        state,
        {
            "run_id": run_id,
            "status": "cancelled",
            "completed_modules": list(REPORT_TAXONOMY),
            "module_review_completion_refs": module_completion_refs,
        },
    )

    assert set(state["module_submissions"]) == set(REPORT_TAXONOMY)
    assert state["cross_review_completion_ref"].endswith("cross-completion.json")
    assert state["final_review_completion_ref"].endswith("final-completion.json")
    assert state["edited_report"] == edited


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
            reviewer_session_key="module-auditor-2.1-post-delivery",
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
    evidence_ref = f"Work/runs/{run_id}/preparation/evidence.jsonl"
    photo_ref = f"Work/runs/{run_id}/preparation/photo-manifest.json"
    cross_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
    service.store.write_text(evidence_ref, "")
    service.store.write_json(photo_ref, {"assets": []})
    service.store.write_json(cross_ref, {"kind": "test-cross-completion"})
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
        "preparation_refs": {
            "evidence": evidence_ref,
            "photo_manifest": photo_ref,
        },
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
            evidence_ref,
            photo_ref,
        ],
        constraints=list(
            state.get("chief_editor_constraints", [])
        ),
        allowed_outputs=["edited_report_submission"],
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


def test_revision_checkpoint_restores_only_hash_bound_chief_completion(
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

    runner._restore_revision_resume_state(state)

    assert state["chief_editor_completion_ref"] == completion_ref
    assert state["chief_editor_envelope"].run_id == state["run_id"]
    assert state["chief_candidate_ref"].endswith("chief-r0.json")


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
    completion = json.loads(
        (tmp_path / completion_ref).read_text(encoding="utf-8")
    )
    completion["artifact_sha256"][envelope_ref] = hashlib.sha256(
        (tmp_path / envelope_ref).read_bytes()
    ).hexdigest()
    runner.service.store.write_json(completion_ref, completion)

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
        match="artifact hash mismatch",
    ):
        runner._load_chief_editor_completion(state, completion_ref)


def test_aggregate_chief_completion_restores_only_hash_bound_candidate(
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


def test_review_completion_rejects_artifact_mutation_after_closure(
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
    service.store.write_json(
        finding_ref,
        {
            "kind": "module_review_finding_submission",
            "findings": [{"id": "M-mutated"}],
        },
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        runner._load_current_review_completion(
            run_id=run_id,
            completion_ref=completion_ref,
            lifecycle="module",
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.4-initial",
            subject_refs=[subject_ref],
        )


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


def test_role_skill_context_does_not_send_authoring_guidance_to_final_auditor() -> None:
    state = {
        "template_skill_text": {
            "core": "CORE-AUTHOR",
            "analysis": "ANALYSIS-AUTHOR",
            "synthesis": "SYNTHESIS-AUTHOR",
            "visual": "VISUAL-AUTHOR",
            "rubric": "AUDIT-RUBRIC",
        }
    }

    final_context = ReportWorkflowRunner._role_skill_context(
        state, "final-auditor"
    )
    cross_context = ReportWorkflowRunner._role_skill_context(
        state, "cross-reviewer"
    )

    assert "AUDIT-RUBRIC" in final_context
    assert "AUTHOR" not in final_context
    assert "SYNTHESIS-AUTHOR" in cross_context
    assert "CORE-AUTHOR" not in cross_context
    assert 'role="final-auditor"' in final_context


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


def test_validation_binding_rejects_stale_subject_bytes(tmp_path: Path) -> None:
    runner = _ScriptedRunner(tmp_path, [])
    subject_ref = "Work/runs/run-stale/modules/2.1-r1.json"
    runner.service.store.write_text(subject_ref, "original")
    report = ValidationReport(
        validation_protocol_version=2,
        run_id="run-stale",
        subject_ref=subject_ref,
        subject_revision=1,
        content_sha256=hashlib.sha256(b"original").hexdigest(),
        validator="test/v2",
        check_ids=["content"],
        passed=True,
    )
    runner.service.store.write_text(subject_ref, "changed-after-validation")

    with pytest.raises(ReviewLifecycleError, match="exact final subject"):
        _require_validation_binding(
            runner,
            report,
            subject_ref=subject_ref,
            subject_revision=1,
        )
