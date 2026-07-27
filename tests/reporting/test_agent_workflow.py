from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.reporting.agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    EditedReportSubmission,
    FINAL_REPORT_SECTION_IDS,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from manyselves.core.reporting.input_contracts import (
    ReviewCompletionRecord,
    ValidationReport,
)
from manyselves.core.reporting.models import CoverageMatrix, ProjectManifest
from manyselves.core.reporting.review_lifecycle import (
    ReviewLifecycleError,
    run_cross_review,
    run_final_review,
    run_module_review,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.versions import ReportVersion
from manyselves.core.reporting.workflow import FullReportCheckpoint, ReportWorkflowRunner


def _artifact_hashes(root: Path, refs: list[str]) -> dict[str, str]:
    return {
        ref: hashlib.sha256((root / ref).read_bytes()).hexdigest()
        for ref in refs
    }


def _module(module_id: str, revision: int = 0) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: (
                f"### {submodule_id}\n\n"
                "当前模块正文说明现状、判断、风险机理、建议责任和验收方法。"
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
        "target_report_section_ids": ["3.1.1", "3.1.3", "3.2"],
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


def _edited(module_text: dict[str, str], *, responses: list | None = None) -> EditedReportSubmission:
    synthesis = (
        "综合当前证据，明确责任、优先顺序、依赖关系、风险影响、验证指标、验收方式和剩余边界。"
        * 24
    )
    return EditedReportSubmission(
        title="示例配电安全专家咨询报告",
        assessment_background=synthesis,
        findings_overview=synthesis,
        regional_executive_summary=synthesis,
        module_narratives=module_text,
        cross_module_analysis=(
            "2.1 系统架构与 2.2 工况共同制约 2.3 保护和 2.4 设备状态，"
            "并依赖 2.5 运维形成联合整改、复核指标和验收闭环。"
            + "说明跨模块对象、作用机制、风险传播、行动依赖和联合验收。" * 10
        ),
        risk_panorama=synthesis,
        dimension_risk_analysis=(
            synthesis
            + "系统架构、电能质量、保护、设备和运维之间存在共同、叠加、依赖和传播关系。"
        ),
        data_gap_analysis=synthesis + "数据不足会限制判断并影响置信度，应优先补证。",
        improvement_action_plan=(
            synthesis + "责任部门牵头，按依赖优先实施，以指标、复测和验收关闭。"
        ),
        new_factory_planning=synthesis + "新建规划设计应预留条件并完成校核和验收验证。",
        capacity_expansion_plan=synthesis + "增容方案应结合负荷和容量完成校核与验收。",
        daily_power_management=synthesis + "责任台账通过巡检监测、维护复测和闭环指标持续管理。",
        emergency_compliance_management=(
            synthesis + "应急合规需要责任人组织演练、危险能量控制、验证记录和复盘。"
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
        return result

    def _validate_module_structure(self, state, module, phase):
        ref = (
            f"Work/runs/{state['run_id']}/reviews/"
            f"module-quality-{module.module_id}-r{module.revision}-{phase}.json"
        )
        self.service.store.write_json(
            ref,
            ValidationReport(
                run_id=state["run_id"],
                subject_ref=(
                    f"Work/runs/{state['run_id']}/modules/"
                    f"{module.module_id}-r{module.revision}.json"
                ),
                validator="test-module-structure/v1",
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
    def _canonical_markdown(edited):
        return ReportWorkflowRunner._canonical_markdown(edited)

    def _validate_final_report_structure(self, state, markdown, phase):
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/reviews/report-integrity-{phase}.json",
            ValidationReport(
                run_id=state["run_id"],
                subject_ref=(
                    f"Work/runs/{state['run_id']}/validation/report-{phase}.md"
                ),
                validator="test-final-report-structure/v1",
                check_ids=["final_report.fixed_sections_and_markdown"],
                passed=True,
            ).model_dump(mode="json"),
        )

    @staticmethod
    def _final_revision_diff(previous, revised):
        return ReportWorkflowRunner._final_revision_diff(previous, revised)


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
        submodule_narratives={
            target: "### 修订\n\n已补充责任接口、执行动作和可验证验收方法。"
        },
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
                            "evidence_refs": [
                                "Work/runs/run-1/modules/2.1-r1.json"
                            ],
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
        "module-auditor-2.1-initial",
        "module-auditor-2.1-initial",
    ]
    completion = ReviewCompletionRecord.model_validate_json(
        (
            tmp_path
            / state["module_review_completion_refs"]["2.1"]
        ).read_text(encoding="utf-8")
    )
    assert completion.resolved_finding_ids == ["M-2.1-initial-r0-001"]


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
        submodule_narratives={
            target: "### 修订\n\n已补充责任接口和可验证验收方法。"
        },
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
                            "evidence_refs": [
                                "Work/runs/run-review-resume/modules/2.1-r1.json"
                            ],
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
                            "evidence_refs": [
                                "Work/runs/run-dispute/modules/2.1-r1.json"
                            ],
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
                        "submodule_id": next(
                            iter(REPORT_TAXONOMY[module_id].submodules)
                        ),
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
        submodule_narratives={
            target: "### 修订\n\n已写入依赖对象、作用机制、实施顺序和联合验收。"
        },
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
                    findings=[],
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
                            "evidence_refs": [
                                "Work/runs/run-x/modules/2.3-r1.json"
                            ],
                        }
                    ],
                    new_findings=[],
                    synthesis_inputs=_cross_synthesis_inputs(),
                ),
            ),
        ],
    )
    state = {"run_id": "run-x", "module_submissions": modules}
    for module_id, module in modules.items():
        runner.service.store.write_json(
            f"Work/runs/run-x/modules/{module_id}-r0.json",
            module.model_dump(mode="json"),
        )
    await run_cross_review(runner, state, "workflow")
    agents = [call[0] for call in runner.calls]
    assert agents == [
        "cross-module-reviewer",
        "module-2.3-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
    ]
    cross_sessions = [
        call[2] for call in runner.calls if call[0] == "cross-module-reviewer"
    ]
    assert cross_sessions == ["cross-module-reviewer", "cross-module-reviewer"]
    recheck_input = json.loads(
        (
            tmp_path
            / "Work/runs/run-x/reviews/cross-review-input-r1.json"
        ).read_text(encoding="utf-8")
    )
    assert recheck_input["changed_module_ids"] == ["2.3"]
    assert set(recheck_input["modules"]) == {"2.3"}
    assert set(recheck_input["unchanged_module_sha256"]) == {
        "2.1",
        "2.2",
        "2.4",
        "2.5",
    }
    assert state["cross_review_completion_ref"].endswith(
        "reviews/cross-completion.json"
    )


@pytest.mark.asyncio
async def test_final_review_uses_chief_response_then_original_auditor_verdict(
    tmp_path: Path,
) -> None:
    module_text = {
        module_id: _module(module_id).markdown for module_id in REPORT_TAXONOMY
    }
    current = _edited(module_text)
    finding = {
        "id": "F-001",
        "target_section_ids": ["3.1.3"],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "跨领域章节缺少行动依赖和联合验收，无法支持实施排序。",
        "evidence_refs": [
            "Work/runs/run-f/edited-revisions/chief-r0.json"
        ],
        "required_change": "在 3.1.3 补充行动依赖顺序和联合验收。",
        "reviewer_checks": ["核对行动依赖和联合验收是否明确且不改变模块事实"],
    }
    revised = current.model_copy(
        update={
            "cross_module_analysis": (
                current.cross_module_analysis
                + " 明确先完成前置核查，再联合验收并记录剩余风险。"
            ),
            "revision_responses": [
                RevisionResponse(
                    finding_id="F-001",
                    action="implemented",
                    summary="已在 3.1.3 补充前置顺序、联合验收和剩余风险记录。",
                    changed_target_ids=["3.1.3"],
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
                        "1.1",
                        "1.2",
                        "1.3",
                        "2.1",
                        "2.2",
                        "2.3",
                        "2.4",
                        "2.5",
                        "3.1.1",
                        "3.1.2",
                        "3.1.3",
                        "3.1.4",
                        "3.2",
                        "4.1",
                        "4.2",
                        "4.3",
                        "4.4",
                    ],
                    findings=[finding],
                    residual_risks=[],
                ),
            ),
            ("chief-editor", "edited_report_submission", revised),
            (
                "chief-editor-auditor",
                "final_review_verdict_submission",
                FinalReviewVerdictSubmission(
                    checked_section_ids=[
                        "1.1",
                        "1.2",
                        "1.3",
                        "2.1",
                        "2.2",
                        "2.3",
                        "2.4",
                        "2.5",
                        "3.1.1",
                        "3.1.2",
                        "3.1.3",
                        "3.1.4",
                        "3.2",
                        "4.1",
                        "4.2",
                        "4.3",
                        "4.4",
                    ],
                    verdicts=[
                        {
                            "finding_id": "F-001",
                            "verdict": "resolved",
                            "reason": "当前 3.1.3 已明确实施依赖和联合验收，问题关闭。",
                            "evidence_refs": [
                                "Work/runs/run-f/edited-revisions/chief-r1.json"
                            ],
                        }
                    ],
                    new_findings=[],
                    residual_risks=[],
                ),
            ),
        ],
    )
    state = {
        "run_id": "run-f",
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
            run_id="run-f",
            agent_id="chief-editor",
            objective="总编",
            allowed_outputs=["edited_report_submission"],
        ),
        chief_session_key="chief-editor",
        approved_module_text=module_text,
        claims=[],
        aggregate_mode=True,
    )
    assert state["final_review_completion_ref"].endswith(
        "reviews/final-completion.json"
    )
    auditor_sessions = [
        call[2] for call in runner.calls if call[0] == "chief-editor-auditor"
    ]
    assert auditor_sessions == [
        "chief-editor-auditor",
        "chief-editor-auditor",
    ]


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
    assert [artifact.path.as_posix() for artifact in review_artifacts] == [
        final_review_ref
    ]
    assert all(
        artifact.path.as_posix() != "Outputs/Reviews/full-review.json"
        for artifact in artifacts
    )


def test_delivery_root_is_scoped_to_the_owning_run(tmp_path: Path) -> None:
    assert ReportWorkflowRunner._delivery_root(
        tmp_path, "run-delivery"
    ) == tmp_path / "Work/runs/run-delivery/delivery"


def test_restore_delivery_rejects_obsolete_review_output_declaration(
    tmp_path: Path,
) -> None:
    service = _FakeService(tmp_path)
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-obsolete-review-output"
    delivery_dir = (
        tmp_path
        / f"Work/runs/{run_id}/delivery"
        / f"power-distribution-report-{run_id}"
    )
    modules_dir = delivery_dir / "modules"
    modules_dir.mkdir(parents=True)
    final_docx = delivery_dir / "report.docx"
    report_state = delivery_dir / "report-state.json"
    manifest = delivery_dir / "delivery-manifest.json"
    final_docx.write_bytes(b"docx")
    report_state.write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    module_files = {}
    for module_id in REPORT_TAXONOMY:
        path = modules_dir / f"{module_id}.md"
        path.write_text(f"# {module_id}", encoding="utf-8")
        module_files[module_id] = path
    hashes = {
        "final_docx": runner._sha256(final_docx),
        "report_state": runner._sha256(report_state),
        "manifest": runner._sha256(manifest),
        **{
            f"module:{module_id}": runner._sha256(path)
            for module_id, path in module_files.items()
        },
    }
    receipt_ref = f"Work/runs/{run_id}/delivery-receipt.json"
    service.store.write_json(
        receipt_ref,
        {
            "success": True,
            "delivery_dir": delivery_dir.as_posix(),
            "final_docx": final_docx.as_posix(),
            "module_files": {
                module_id: path.as_posix()
                for module_id, path in module_files.items()
            },
            "report_state": report_state.as_posix(),
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
        "final_review_completion_ref": (
            f"Work/runs/{run_id}/reviews/final-completion.json"
        ),
    }

    runner._restore_delivery_completion(state)

    assert "delivery_completion_ref" not in state
    assert "output_artifacts" not in state


@pytest.mark.asyncio
async def test_module_resume_rebinds_legacy_parts_without_replaying_old_refs(
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
                "artifact_refs": [
                    f"Work/runs/{run_id}/drafts/module-{module_id}/r0/{part_id}.md"
                ]
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
        "module_knowledge_refs": {
            module_id: f"Work/runs/{run_id}/knowledge/module-{module_id}.md"
        },
    }

    with pytest.raises(RuntimeError, match="captured correction envelope"):
        await runner._module_pipeline(module_id, state, "workflow")

    envelope = captured["envelope"]
    assert isinstance(envelope, TaskEnvelope)
    assert envelope.target_submodule_ids == sorted(part_ids)
    assert envelope.allowed_tools == [
        "search_project_evidence",
        "open_project_source",
        "list_result_parts",
        "write_result_part",
        "submit_result",
    ]
    assert not any("/drafts/" in ref for ref in envelope.input_refs)
    assert not any("correction-state" in ref for ref in envelope.input_refs)
    input_contract = json.loads(
        (tmp_path / envelope.input_contract_ref).read_text(encoding="utf-8")
    )
    assert input_contract["saved_part_ids"] == sorted(part_ids)
    assert input_contract["rewrite_part_ids"] == sorted(part_ids)
    assert "existing_part_refs" not in input_contract
    assert "pending_correction_ref" not in input_contract


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
        finding_ref = (
            f"Work/runs/{run_id}/reviews/module/initial/{module_id}/"
            "findings-r0.json"
        )
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
        completion_ref = (
            f"Work/runs/{run_id}/reviews/module/initial/{module_id}/"
            "completion-r0.json"
        )
        module_completion_refs[module_id] = completion_ref
        service.store.write_json(
            completion_ref,
            ReviewCompletionRecord(
                lifecycle="module",
                run_id=run_id,
                reviewer_agent_id="evidence-auditor",
                reviewer_session_key=f"module-auditor-{module_id}-initial",
                subject_refs=[subject_ref],
                finding_refs=[finding_ref],
                verdict_refs=[],
                resolved_finding_ids=[],
                artifact_sha256=_artifact_hashes(
                    tmp_path, [subject_ref, finding_ref]
                ),
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
            artifact_sha256=_artifact_hashes(
                tmp_path, [*module_refs, cross_finding_ref]
            ),
        ).model_dump(mode="json"),
    )

    edited_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    edited = _edited(
        {module_id: module.markdown for module_id, module in modules.items()}
    )
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
            artifact_sha256=_artifact_hashes(
                tmp_path, [edited_ref, final_finding_ref]
            ),
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
            artifact_sha256=_artifact_hashes(
                tmp_path, [subject_ref, finding_ref]
            ),
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
    assert "#### 3.1.3 跨领域关联风险" in markdown
    assert "### 4.4 应急管理及合规性管理建议" in markdown


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
