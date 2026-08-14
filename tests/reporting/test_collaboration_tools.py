from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.agentic_models import (
    SUBMISSION_INPUT_TYPES,
    TEMPLATE_ROLE_SKILL_IDS,
    ClaimRecord,
    CrossOwnerVerdictSubmission,
    CrossReviewFinding,
    EditedReportSubmission,
    ModuleSubmission,
    TemplateSkillSubmission,
)
from manyselves.core.reporting.claim_ledger import ClaimLedger
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.input_contracts import (
    ChiefEditorInput,
    ChiefRevisionInput,
    CrossOwnerInput,
    INPUT_CONTRACT_EXAMPLES,
    ModuleContentView,
    ModuleReviewInput,
    TemplateDistillationInput,
    ValidationReport,
    module_content_view,
)
from manyselves.core.reporting.source_ledger import SourceLedger
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.submission_contracts import (
    render_submission_contract,
    submission_schema,
)
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.tools.reporting_collaboration_tools import (
    ListResultPartsTool,
    SubmissionValidationError,
    SubmitResultTool,
    WriteResultPartTool,
    WriteResultPartsTool,
)


def _special_topic_plan() -> dict:
    return {
        "source_ref": "Inputs/专项问题分析.md",
        "source_sha256": "0" * 64,
        "sections": [
            {
                "section_id": "4.1",
                "title": "动态专项问题",
                "requirement": "分析项目边界、方案条件和验证方法。",
            }
        ],
    }


def _tool(
    workspace: Path,
    *,
    task_id: str = "task",
    allowed_outputs: list[str] | None = None,
    revision: int = 0,
    input_contract_kind: str | None = None,
    input_contract_ref: str | None = None,
    submission_schemas: dict[str, dict] | None = None,
) -> SubmitResultTool:
    return SubmitResultTool(
        "agent",
        "session",
        "run-1",
        task_id,
        ReportingStore(workspace),
        MessageBus(),
        "workflow",
        allowed_outputs=allowed_outputs or ["module_submission"],
        revision=revision,
        input_contract_kind=input_contract_kind,
        input_contract_ref=input_contract_ref,
        submission_schemas=submission_schemas,
    )


def _module_payload() -> dict:
    return {
        "kind": "module_submission",
        "module_id": "2.1",
        "unresolved_questions": [],
        "revision": 0,
        "revision_responses": [],
    }


def _module_subject_payload() -> dict:
    return {
        "kind": "module_submission",
        "module_id": "2.1",
        "submodule_narratives": {
            submodule_id: f"### {submodule_id}\n\n示例正文"
            for submodule_id in REPORT_TAXONOMY["2.1"].submodules
        },
        "claims": [],
        "source_ids": [],
        "unresolved_questions": [],
        "revision": 0,
        "revision_responses": [],
    }


async def _write_bound_module_parts(
    workspace: Path,
    *,
    task_id: str = "task",
    revision: int = 0,
    part_ids: list[str] | None = None,
) -> None:
    evidence_id = "E-0001"
    SourceLedger(workspace, "run-1").register_project(
        evidence_id,
        "测试证据",
        "Inputs/test.txt",
        "测试事实",
    )
    ids = part_ids or list(REPORT_TAXONOMY["2.1"].submodules)
    writer = WriteResultPartTool(
        "run-1",
        task_id,
        revision,
        ReportingStore(workspace),
        ids,
        evidence_binding_required=True,
    )
    for part_id in ids:
        await writer(
            part_id=part_id,
            content=f"### {part_id}\n\n由项目证据支持的完整正文。",
            evidence_ids=[evidence_id],
        )


def _write_revision_contract(
    workspace: Path,
    *,
    subject: dict,
    target_submodule_ids: list[str],
) -> str:
    contract_ref = "Work/runs/run-1/reviews/module-revision-input-2.1-r1.json"
    subject_model = ModuleSubmission.model_validate(subject)
    ReportingStore(workspace).write_json(
        "Work/runs/run-1/modules/2.1-r0.json",
        subject_model.model_dump(mode="json"),
    )
    ReportingStore(workspace).write_json(
        contract_ref,
        {
            "kind": "module_revision_input",
            "run_id": "run-1",
            "module_id": "2.1",
            "subject_ref": "Work/runs/run-1/modules/2.1-r0.json",
            "subject": module_content_view(
                subject_model, set(target_submodule_ids)
            ).model_dump(mode="json"),
            "target_submodule_ids": target_submodule_ids,
            "module_findings": [
                {
                    "id": "M-001",
                    "target_submodule_id": target_submodule_ids[0],
                    "category": "factual_accuracy",
                    "impact": "blocking",
                    "observation": "当前目标小节存在需要修正且必须重新核验的具体事实表述问题。",
                    "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
                    "required_change": "修正目标表述并保留未变更 Claim 的完整引用标记。",
                    "reviewer_checks": ["目标表述已修正且既有 Claim 标记仍可追溯"],
                }
            ],
            "cross_findings": [],
            "requested_changes": [],
        },
    )
    return contract_ref


def _cross_synthesis_input() -> dict:
    return {
        "id": "SI-001",
        "related_module_ids": ["2.1", "2.2"],
        "cluster_type": "risk_cluster",
        "root_causes": ["2.1 供电边界与 2.2 环境压力具有共同约束"],
        "propagation_steps": [
            "2.1 供电边界使局部异常更容易扩大",
            "2.2 环境压力进一步削弱设备运行裕度",
        ],
        "causal_chain": "2.1 供电边界与 2.2 环境压力叠加后，会共同扩大故障影响范围。",
        "decision_implication": "管理层需要按共同根因安排联合整改，而不能分别关闭表面问题。",
        "action_dependencies": ["先确认 2.1 供电边界，再处理 2.2 环境压力"],
        "joint_actions": ["由 2.1 与 2.2 责任方共同完成边界确认和整改复测"],
        "verification_method": "联合核对供电边界、环境复测结果和异常事件记录。",
        "acceptance_criteria": ["2.1 与 2.2 的复测记录均达到约定关闭条件"],
        "module_statement_refs": ["2.1.1.1", "2.2.1.1"],
        "confidence_and_boundary": "当前关系由现场证据支持，具体阈值仍需连续数据进一步确认。",
        "target_report_section_ids": ["3.1.1", "3.1.2", "3.2"],
        "evidence_refs": ["E-0001"],
    }


def _write_chief_contract_and_claim_ledger(workspace: Path) -> str:
    ledger = SourceLedger(workspace, "run-1")
    source = ledger.register_project(
        "E-0001",
        "系统边界证据",
        "Inputs/system.txt",
        "供电边界与环境压力存在关联。",
    )
    claim = ClaimRecord(
        id="C-2.1-001",
        module_id="2.1",
        submodule_id=next(iter(REPORT_TAXONOMY["2.1"].submodules)),
        text="供电边界可能扩大异常影响范围",
        claim_type="risk_judgment",
        source_ids=["E-0001"],
    )
    ReportingStore(workspace).write_json(
        "Work/runs/run-1/ledgers/claims.json",
        ClaimLedger(claims=[claim], sources=[source]).model_dump(mode="json"),
    )
    modules = {}
    for module_id, definition in REPORT_TAXONOMY.items():
        submodule_id = next(iter(definition.submodules))
        modules[module_id] = ModuleContentView(
            module_id=module_id,
            revision=0,
            submodule_narratives={submodule_id: f"{module_id} 已批准正文"},
            evidence_ids_by_submodule={submodule_id: ["E-0001"] if module_id == "2.1" else []},
        )
    contract = ChiefEditorInput(
        run_id="run-1",
        approved_module_markers={
            module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_TAXONOMY
        },
        modules=modules,
        cross_review_completion_ref="Work/runs/run-1/reviews/cross-completion.json",
        special_topic_plan=_special_topic_plan(),
    )
    contract_ref = "Work/runs/run-1/context/chief-editor-input.json"
    ReportingStore(workspace).write_json(contract_ref, contract.model_dump(mode="json"))
    return contract_ref


@pytest.mark.asyncio
async def test_submit_result_preserves_raw_candidate_before_validation(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    bad = {"kind": "cross_review_submission", "approved": True}
    outcome = await tool(**bad)
    assert outcome["status"] == "correction_required"
    assert outcome["accepted"] is False
    assert outcome["submission_kind"] == "module_submission"
    assert outcome["next_action"] == (
        "resubmit_result_once_after_applying_validation_errors"
    )
    assert "Never repeat" in outcome["instruction"]
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "kind"
    assert issue["received_type"] == "string"
    assert issue["received"] == "cross_review_submission"
    assert issue["expected"] == "one of ['module_submission']"
    assert issue["example"] == "module_submission"
    assert "top-level kind" in issue["repair_instruction"]
    assert outcome["remaining_attempts"] == 7
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["raw_payload"] == bad


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(SUBMISSION_INPUT_TYPES))
async def test_every_submission_identity_gets_common_structured_correction(
    tmp_path: Path,
    kind: str,
) -> None:
    tool = _tool(
        tmp_path,
        task_id=f"correction-{kind}",
        allowed_outputs=[kind],
    )

    outcome = await tool(kind=kind)

    assert outcome["status"] == "correction_required"
    assert outcome["accepted"] is False
    assert outcome["submission_kind"] == kind
    assert outcome["next_action"] == (
        "resubmit_result_once_after_applying_validation_errors"
    )
    assert isinstance(outcome["validation_errors"], list)
    assert outcome["validation_errors"]
    assert "rewrite_part_ids" in outcome
    assert "Never repeat" in outcome["instruction"]


@pytest.mark.asyncio
async def test_review_submit_runtime_assigns_coverage_and_finding_id(
    tmp_path: Path,
) -> None:
    contract = ModuleReviewInput(
        phase="initial",
        run_id="run-1",
        module_id="2.1",
        lifecycle_id="initial",
        review_round=0,
        subject_ref="Work/runs/run-1/modules/2.1-r0.json",
        subject_revision=0,
        subject=ModuleContentView(
            module_id="2.1",
            revision=0,
            submodule_narratives={"2.1.1": "完整审查正文"},
            evidence_ids_by_submodule={"2.1.1": []},
        ),
        evidence=[],
        required_submodule_ids=["2.1.1"],
        validation_report_ref="Work/runs/run-1/validations/2.1.json",
        validation_report=ValidationReport(
            validation_protocol_version=2,
            run_id="run-1",
            subject_ref="Work/runs/run-1/modules/2.1-r0.json",
            subject_revision=0,
            content_sha256="0" * 64,
            validator="test/v2",
            check_ids=["structure"],
            passed=True,
        ),
    )
    contract_ref = "Work/runs/run-1/reviews/module-review-input.json"
    ReportingStore(tmp_path).write_json(contract_ref, contract.model_dump(mode="json"))
    tool = _tool(
        tmp_path,
        task_id="module-2.1-review-r0",
        allowed_outputs=["module_review_finding_submission"],
        input_contract_kind="module_review_input",
        input_contract_ref=contract_ref,
    )

    outcome = await tool(
        **{
            "kind": "module_review_finding_submission",
            "findings": [
                {
                    "target_submodule_id": "2.1.1",
                    "category": "evidence_boundary",
                    "impact": "blocking",
                    "observation": "当前正文把尚未核实的条件性信息写成了确定项目事实。",
                    "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
                    "required_change": "将该表述改为明确待核实，并说明证据缺口对结论的影响。",
                    "reviewer_checks": ["条件性表述和证据缺口均已清晰呈现"],
                }
            ],
        }
    )

    assert outcome["status"] == "completed"
    assert outcome["accepted"] is True
    assert outcome["submission_kind"] == "module_review_finding_submission"
    assert outcome["next_action"] == "finish_task"
    assert "Do not submit or rewrite" in outcome["instruction"]
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/module-2.1-review-r0.json").read_text(encoding="utf-8")
    )
    assert result["payload"]["coverage"] == {"submodule_ids": ["2.1.1"]}
    assert result["payload"]["findings"][0]["id"] == ("M-2.1-initial-r0-001")


def _cross_owner_contract(
    *,
    phase: str = "initial",
    required_findings: list[CrossReviewFinding] | None = None,
) -> CrossOwnerInput:
    payload = dict(INPUT_CONTRACT_EXAMPLES["cross_owner_input"])
    payload["phase"] = phase
    payload["review_round"] = 0 if phase == "initial" else 1
    payload["required_findings"] = [
        finding.model_dump(mode="json")
        for finding in (required_findings or [])
    ]
    if phase == "recheck":
        payload["revision_responses"] = [
            {
                "finding_id": finding.id,
                "action": "implemented",
                "summary": "已完成 owner 小节修订并保留相关模块只读边界。",
                "changed_target_ids": [finding.target_submodule_ids[0]],
            }
            for finding in (required_findings or [])
        ]
        payload["local_regression_review_ref"] = (
            "Work/runs/run-1/reviews/module/cross-r1/2.1/completion.json"
        )
        payload["machine_validation_ref"] = (
            "Work/runs/run-1/validations/cross-2.1-r0.json"
        )
        payload["machine_validation_report"] = {
            "kind": "validation_report",
            "validation_protocol_version": 2,
            "run_id": "run-1",
            "subject_ref": payload["owner_subject_ref"],
            "subject_revision": payload["owner_subject_revision"],
            "content_sha256": "0" * 64,
            "validator": "test-cross-owner/v2",
            "check_ids": ["cross.owner"],
            "failures": [],
            "observations": [],
            "passed": True,
        }
    return CrossOwnerInput.model_validate(payload)


def _cross_owner_finding() -> CrossReviewFinding:
    return CrossReviewFinding(
        id="XMR-2.1-001",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块正文尚未明确跨模块责任接口、实施顺序和联合验收边界。",
        evidence_refs=["Work/runs/run-1/modules/2.1-r0.json"],
        required_change="请在 2.1.1 补充责任接口、实施顺序和联合验收记录要求。",
        reviewer_checks=["责任接口、顺序和联合验收均已写入目标小节。"],
        machine_checks=[],
    )


@pytest.mark.asyncio
async def test_cross_owner_submit_runtime_assigns_owner_scope_and_finding_id(
    tmp_path: Path,
) -> None:
    contract = _cross_owner_contract()
    contract_ref = "Work/runs/run-1/reviews/cross-owner-input-2.1.json"
    ReportingStore(tmp_path).write_json(contract_ref, contract.model_dump(mode="json"))
    tool = _tool(
        tmp_path,
        task_id="cross-owner-2.1-r0",
        allowed_outputs=["cross_owner_finding_submission"],
        input_contract_kind="cross_owner_input",
        input_contract_ref=contract_ref,
    )
    outcome = await tool(
        kind="cross_owner_finding_submission",
        findings=[
                {
                    "owner_module_id": "2.1",
                    "target_submodule_ids": ["2.1.1"],
                    "related_module_ids": ["2.2"],
                    "category": "dependencies",
                    "impact": "blocking",
                    "observation": "当前模块正文尚未明确跨模块责任接口、实施顺序和联合验收边界。",
                    "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
                    "required_change": "请在 2.1.1 补充责任接口、实施顺序和联合验收记录要求。",
                    "reviewer_checks": ["责任接口、顺序和联合验收均已写入目标小节。"],
                    "machine_checks": [],
                }
            ],
    )
    assert outcome["status"] == "completed", outcome
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/cross-owner-2.1-r0.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["payload"]["owner_module_id"] == "2.1"
    assert result["payload"]["coverage"]["module_id"] == "2.1"
    assert result["payload"]["findings"][0]["id"] == "XMR-2.1-001"


def test_cross_owner_verdict_reads_empty_removed_protocol_fields() -> None:
    payload = {
        "kind": "cross_owner_verdict_submission",
        "owner_module_id": "2.1",
        "coverage": {
            "module_id": "2.1",
            "checked_dimensions": [
                "terminology",
                "facts",
                "risk_levels",
                "dependencies",
                "propagation",
                "joint_verification",
            ],
        },
        "verdicts": [],
        "new_findings": [],
        "synthesis_inputs": [],
        "interface_closures": [],
    }

    restored = CrossOwnerVerdictSubmission.model_validate(payload)
    dumped = restored.model_dump(mode="json")
    assert dumped["new_findings"] == []
    assert "synthesis_inputs" not in dumped
    assert "interface_closures" not in dumped

    payload["interface_closures"] = [{"request_id": "IF-obsolete"}]
    with pytest.raises(ValueError, match="interface_closures"):
        CrossOwnerVerdictSubmission.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_echo", [False, True])
async def test_cross_owner_recheck_injects_exact_ids_and_accepts_new_regressions(
    tmp_path: Path,
    legacy_echo: bool,
) -> None:
    finding = _cross_owner_finding()
    contract = _cross_owner_contract(phase="recheck", required_findings=[finding])
    contract_ref = "Work/runs/run-1/reviews/cross-owner-input-2.1-r1.json"
    ReportingStore(tmp_path).write_json(contract_ref, contract.model_dump(mode="json"))
    tool = _tool(
        tmp_path,
        task_id="cross-owner-2.1-r1",
        allowed_outputs=["cross_owner_verdict_submission"],
        revision=1,
        input_contract_kind="cross_owner_input",
        input_contract_ref=contract_ref,
    )
    payload = {
            "kind": "cross_owner_verdict_submission",
            "verdicts": [
                {
                    "verdict": "resolved",
                    "reason": "责任模块已完成修订并通过同一 owner 的语义复核。",
                    "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
                }
            ],
        }
    if legacy_echo:
        # Legacy model echoes are ignored: initial material is immutable and
        # the removed IF protocol must not break a valid five-module verdict.
        payload["synthesis_inputs"] = []
        payload["interface_closures"] = []
    outcome = await tool(**payload)
    assert outcome["status"] == "completed", outcome
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/cross-owner-2.1-r1.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["finding_id"] for item in result["payload"]["verdicts"]] == [
        "XMR-2.1-001"
    ]
    assert result["payload"]["new_findings"] == []
    assert "synthesis_inputs" not in result["payload"]
    assert "interface_closures" not in result["payload"]

    regression_tool = _tool(
        tmp_path,
        task_id="cross-owner-2.1-r1-regression",
        allowed_outputs=["cross_owner_verdict_submission"],
        revision=1,
        input_contract_kind="cross_owner_input",
        input_contract_ref=contract_ref,
    )
    regression = await regression_tool(
        **{
            "kind": "cross_owner_verdict_submission",
            "verdicts": [
                {
                    "verdict": "resolved",
                    "reason": "责任模块已完成修订并通过同一 owner 的语义复核。",
                    "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
                }
            ],
            "new_findings": [
                {
                    "owner_module_id": "2.1",
                    "target_submodule_ids": ["2.1.1"],
                    "related_module_ids": ["2.2"],
                    "category": "dependencies",
                    "impact": "advisory",
                    "observation": (
                        "本轮 owner 修订虽然关闭了原问题，但同时改变了与模块2.2之间的"
                        "实施先后关系，形成了新的跨模块依赖回归。"
                    ),
                    "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
                    "required_change": (
                        "下一轮应由责任模块补充实施先后关系、接口责任人以及联合验收条件，"
                        "再交由同一 Cross owner 复核关闭。"
                    ),
                    "reviewer_checks": ["下一轮修订关闭该新增回归。"],
                    "machine_checks": [],
                }
            ],
        }
    )
    assert regression["status"] == "completed", regression
    regression_result = json.loads(
        (
            tmp_path
            / "Work/runs/run-1/results/cross-owner-2.1-r1-regression.json"
        ).read_text(encoding="utf-8")
    )
    assert regression_result["payload"]["new_findings"][0]["id"].startswith(
        "XMR-2.1-r1-"
    )


@pytest.mark.asyncio
async def test_submit_result_appends_attempt_after_same_run_resume(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    refs = []
    for attempt in range(1, 4):
        ref = f"Work/runs/run-1/submissions/task/attempt-{attempt}-raw.json"
        store.write_json(ref, {"raw_payload": {"original_attempt": attempt}})
        refs.append(ref)
    before = {ref: (tmp_path / ref).read_bytes() for ref in refs}
    await _write_bound_module_parts(tmp_path)

    outcome = await _tool(tmp_path)(**_module_payload())

    assert outcome["status"] == "completed", outcome
    for ref, original_bytes in before.items():
        assert (tmp_path / ref).read_bytes() == original_bytes
    appended = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-4-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert appended["raw_payload"] == _module_payload()


@pytest.mark.asyncio
async def test_submit_result_rejects_legacy_payload_wrapper_without_unwrapping(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    candidate = json.dumps(_module_payload())
    outcome = await tool(payload=candidate)
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "payload"
    assert issue["received"] == candidate
    assert "does not accept a payload wrapper" in issue["problem"]
    assert issue["expected"].startswith("no payload property")
    assert "Remove the outer payload property" in issue["repair_instruction"]
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["raw_payload"] == {"payload": candidate}


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", ["not-json", "[]", '"double encoded"'])
async def test_submit_result_rejects_non_object_string_without_guessing(
    tmp_path: Path,
    candidate: str,
) -> None:
    outcome = await _tool(tmp_path)(payload=candidate)
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "payload"
    assert issue["received_type"] == "string"
    assert issue["received"] == candidate
    assert issue["expected"].startswith("no payload property")


@pytest.mark.asyncio
async def test_missing_kind_uses_the_only_allowed_contract_for_correction(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    payload = _module_payload()
    payload.pop("kind")

    outcome = await tool(**payload)

    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "kind"
    assert issue["received_type"] == "null"
    assert issue["expected"] == "one of ['module_submission']"
    assert issue["example"] == "module_submission"
    assert "top-level kind" in issue["repair_instruction"]


@pytest.mark.asyncio
async def test_flat_incomplete_correction_uses_current_module_authoring_contract(
    tmp_path: Path,
) -> None:
    contract_ref = "Work/runs/run-1/context/module-2.2-authoring.json"
    required = list(REPORT_TAXONOMY["2.2"].submodules)
    ReportingStore(tmp_path).write_json(
        contract_ref,
        {
            "kind": "module_authoring_input",
            "run_id": "run-1",
            "module_id": "2.2",
            "revision": 0,
            "required_submodule_ids": required,
            "coverage_ref": "Work/coverage.json",
            "evidence_ref": "Work/evidence.jsonl",
            "manifest_ref": "Work/manifest.json",
            "knowledge_ref": "Work/runs/run-1/knowledge/module-2.2.md",
            "saved_part_ids": [],
            "rewrite_part_ids": [],
        },
    )
    tool = _tool(
        tmp_path,
        task_id="module-2.2",
        input_contract_kind="module_authoring_input",
        input_contract_ref=contract_ref,
    )

    outcome = await tool(kind="module_submission")

    issues = {issue["field"]: issue for issue in outcome["validation_errors"]}
    assert issues["module_id"]["example"] == "2.2"
    assert issues["revision"]["example"] == 0
    assert "submodule_narratives" not in issues
    assert "claims" not in issues
    assert "source_ids" not in issues


@pytest.mark.asyncio
async def test_submit_result_does_not_autofill_finding_contract(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path, allowed_outputs=["module_review_finding_submission"])
    payload = {
        "kind": "module_review_finding_submission",
        "coverage": {"submodule_ids": ["2.1.1"]},
        "findings": [
            {
                "id": "M-001",
                "target_submodule_id": "2.1.1",
                "category": "analysis_depth",
                "impact": "blocking",
                "observation": "当前正文没有形成足够具体且可以复核的行动闭环说明。",
                "evidence_refs": [],
                "required_change": "补充行动责任和验收方法。",
                "reviewer_checks": [],
            }
        ],
    }
    outcome = await tool(**payload)
    assert outcome["status"] == "correction_required"
    assert {
        "field",
        "problem",
        "received_type",
        "received",
        "expected",
        "example",
        "repair_instruction",
    }.issubset(outcome["validation_errors"][0])
    raw = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["raw_payload"] == payload


@pytest.mark.asyncio
async def test_repeated_same_contract_error_stops_with_failed_result(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path, allowed_outputs=["module_review_finding_submission"])
    bad = {
        "kind": "module_review_finding_submission",
        "coverage": {"submodule_ids": []},
        "findings": [],
    }
    first = await tool(**bad)
    assert first["status"] == "correction_required"
    outcome = await tool(**bad)
    assert outcome["status"] == "failed"
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert "stopped instead of guessing" in result["reason"]


@pytest.mark.asyncio
async def test_module_revision_persists_explicit_patch_without_merging_baseline(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1-revision-r1"
    contract_ref = _write_revision_contract(
        tmp_path,
        subject=_module_subject_payload(),
        target_submodule_ids=["2.1.1"],
    )
    await _write_bound_module_parts(
        tmp_path,
        task_id=task_id,
        revision=1,
        part_ids=["2.1.1"],
    )
    tool = _tool(
        tmp_path,
        task_id=task_id,
        allowed_outputs=["module_revision_submission"],
        revision=1,
        input_contract_kind="module_revision_input",
        input_contract_ref=contract_ref,
    )
    payload = {
        "kind": "module_revision_submission",
        "module_id": "2.1",
        "base_revision": 0,
        "revision": 1,
        "unresolved_questions": [],
        "revision_responses": [
            {
                "finding_id": "M-001",
                "action": "implemented",
                "summary": "已在目标小节实施定向修改并保持其他小节不在补丁中。",
                "changed_target_ids": ["2.1.1"],
            }
        ],
    }
    outcome = await tool(**payload)
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert result["payload"]["kind"] == "module_revision_submission"
    assert set(result["payload"]["submodule_narratives"]) == {"2.1.1"}
    assert result["payload"]["claims_upsert"][0]["id"] == "C-2.1-2-1-1"
    assert result["payload"]["source_ids"] == ["E-0001"]
    assert "markdown" not in result["payload"]


@pytest.mark.asyncio
async def test_module_revision_materializes_only_targets_declared_implemented(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1-revision-r1"
    contract_ref = _write_revision_contract(
        tmp_path,
        subject=_module_subject_payload(),
        target_submodule_ids=["2.1.1", "2.1.2"],
    )
    contract_path = tmp_path / contract_ref
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["module_findings"].append(
        {
            "id": "M-002",
            "target_submodule_id": "2.1.2",
            "category": "analysis_depth",
            "impact": "advisory",
            "observation": "当前目标小节还缺少需要外部确认后才能补写的分析前提与边界说明。",
            "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
            "required_change": "在获得缺失输入后补写分析前提，并明确结论适用范围和验证方式。",
            "reviewer_checks": ["缺失输入已取得，且结论边界和验证方式均已写明"],
        }
    )
    ReportingStore(tmp_path).write_json(contract_ref, contract)
    await _write_bound_module_parts(
        tmp_path,
        task_id=task_id,
        revision=1,
        part_ids=["2.1.1"],
    )
    tool = _tool(
        tmp_path,
        task_id=task_id,
        allowed_outputs=["module_revision_submission"],
        revision=1,
        input_contract_kind="module_revision_input",
        input_contract_ref=contract_ref,
    )

    outcome = await tool(
        **{
            "kind": "module_revision_submission",
            "module_id": "2.1",
            "base_revision": 0,
            "revision": 1,
            "unresolved_questions": ["2.1.2 仍需外部输入。"],
            "revision_responses": [
                {
                    "finding_id": "M-001",
                    "action": "implemented",
                    "summary": "已在目标小节实施定向修改，并保持其他小节继续继承基线内容。",
                    "changed_target_ids": ["2.1.1"],
                },
                {
                    "finding_id": "M-002",
                    "action": "needs_input",
                    "summary": "仍缺少形成该小节结论所需的外部事实，当前不能编造补充内容。",
                    "changed_target_ids": [],
                },
            ],
        }
    )

    assert outcome["status"] == "completed", outcome
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    patch = result["payload"]
    assert set(patch["submodule_narratives"]) == {"2.1.1"}
    assert {claim["submodule_id"] for claim in patch["claims_upsert"]} == {"2.1.1"}
    assert {response["action"] for response in patch["revision_responses"]} == {
        "implemented",
        "needs_input",
    }


@pytest.mark.asyncio
async def test_module_revision_correction_example_uses_each_finding_target(
    tmp_path: Path,
) -> None:
    contract_ref = _write_revision_contract(
        tmp_path,
        subject=_module_subject_payload(),
        target_submodule_ids=["2.1.1", "2.1.2"],
    )
    contract_path = tmp_path / contract_ref
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["module_findings"].append(
        {
            "id": "M-002",
            "target_submodule_id": "2.1.2",
            "category": "analysis_depth",
            "impact": "advisory",
            "observation": "当前目标小节缺少对关键约束、作用机制和验证路径的完整分析说明。",
            "evidence_refs": ["Work/runs/run-1/modules/2.1-r0.json"],
            "required_change": "补写关键约束、作用机制及可复核的验证路径，且不得改写其他小节。",
            "reviewer_checks": ["关键约束、作用机制和验证路径均已在目标小节写明"],
        }
    )
    ReportingStore(tmp_path).write_json(contract_ref, contract)
    tool = _tool(
        tmp_path,
        task_id="module-2.1-revision-r1",
        allowed_outputs=["module_revision_submission"],
        revision=1,
        input_contract_kind="module_revision_input",
        input_contract_ref=contract_ref,
    )

    outcome = await tool(
        kind="module_revision_submission",
        revision_responses="invalid",
    )

    issues = {issue["field"]: issue for issue in outcome["validation_errors"]}
    responses = issues["revision_responses"]["example"]
    assert {
        response["finding_id"]: response["changed_target_ids"]
        for response in responses
    } == {
        "M-001": ["2.1.1"],
        "M-002": ["2.1.2"],
    }


@pytest.mark.asyncio
async def test_module_commit_materializes_bound_parts_without_model_refs(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1"
    await _write_bound_module_parts(tmp_path, task_id=task_id)
    tool = _tool(tmp_path, task_id=task_id)
    outcome = await tool(**_module_payload())
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert "由项目证据支持的完整正文。" in result["payload"]["submodule_narratives"]["2.1.1"]
    assert "[[CLAIM:C-2.1-2-1-1]]" in result["payload"]["submodule_narratives"]["2.1.1"]
    assert result["payload"]["claims"][0]["source_ids"] == ["E-0001"]


@pytest.mark.asyncio
async def test_result_part_rejects_compaction_marker_without_overwriting_prose(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    writer = WriteResultPartTool("run-1", "task", 0, store, ["2.1.1"])
    saved = await writer(part_id="2.1.1", content="已经持久化的完整正文。")

    assert saved["persisted"] is True
    assert saved["next_action"] == "list_result_parts"
    assert "Do not rewrite" in saved["rewrite_policy"]

    rejected = await writer(
        part_id="2.1.1",
        content=(
            "<persisted_result_part "
            "sha256=389878319f157175ac47ee882a14cf729420d8766012b5c0d5e82ce4d5ae4fe4 "
            "characters=1445>"
        ),
    )
    assert rejected["status"] == "correction_required"
    assert rejected["accepted"] is False
    assert rejected["persisted"] is False
    assert rejected["rewrite_part_ids"] == ["2.1.1"]
    assert "retired internal history token" in (
        rejected["validation_errors"][0]["problem"]
    )
    assert "persisted_result_part" not in json.dumps(
        rejected,
        ensure_ascii=False,
    )

    saved_content = (
        tmp_path / "Work/runs/run-1/drafts/task/r0/2.1.1.md"
    ).read_text(encoding="utf-8")
    assert saved_content == "已经持久化的完整正文。"

    listing = await ListResultPartsTool(
        "run-1",
        "task",
        0,
        store,
        ["2.1.1"],
    )()
    assert listing["complete"] is True
    assert listing["ready_part_ids"] == ["2.1.1"]
    assert listing["do_not_rewrite_part_ids"] == ["2.1.1"]
    assert listing["next_action"] == "submit_result"
    assert "do not rewrite ready parts" in listing["instruction"]


@pytest.mark.asyncio
async def test_template_role_skill_frontmatter_correction_targets_only_that_identity(
    tmp_path: Path,
) -> None:
    description = (
        "从模板中提炼可迁移的报告写作与推理方法，仅约束表达组织，"
        "不迁移当前客户事实、专业阈值或项目结论。"
    )
    skills = {
        skill_id: (
            f"---\nname: report-template-{skill_id}\ndescription: {description}\n---\n"
            f"# {skill_id}\n\n" + "所有方法仅约束表达与推理，不提供当前项目事实。" * 12
        )
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    }
    skills["author-2.1"] = skills["author-2.1"].replace(
        "name: report-template-author-2.1", "name: report-template-wrong"
    )
    payload = {
        "kind": "template_skill_submission",
        "name": "report-template-role-skills",
        "skills": skills,
    }

    contract_ref = "Work/runs/run-1/context/template-distillation-input.json"
    ReportingStore(tmp_path).write_json(
        contract_ref,
        {
            "kind": "template_distillation_input",
            "run_id": "run-1",
            "template_ref": "template.docx",
            "inspect_max_chars": 100000,
            "required_part_ids": list(TEMPLATE_ROLE_SKILL_IDS),
            "boundary_policy_version": 1,
            "allowed_transfer_categories": [
                "analysis_method",
                "synthesis_method",
                "visual_method",
                "quality_check",
            ],
            "required_exclusion_categories": [
                "domain_knowledge",
                "domain_standard_or_threshold",
                "project_fact_or_number",
                "customer_identity",
                "project_finding_or_risk",
                "project_conclusion_or_recommendation",
                "evidence_or_claim_identifier",
            ],
        },
    )

    outcome = await _tool(
        tmp_path,
        task_id="template-skill-distillation",
        allowed_outputs=["template_skill_submission"],
        input_contract_kind="template_distillation_input",
        input_contract_ref=contract_ref,
    )(**payload)

    assert outcome["status"] == "correction_required"
    assert outcome["affected_part_ids"] == ["author-2.1"]
    assert outcome["rewrite_part_ids"] == ["author-2.1"]
    problem = outcome["validation_errors"][0]["problem"]
    assert "frontmatter name" in problem


def test_template_skill_boundary_reports_the_exact_contaminated_identity() -> None:
    description = (
        "从模板中提炼可迁移的报告写作与推理方法，仅约束表达组织，"
        "不迁移当前客户事实、专业阈值或项目结论。"
    )
    skills = {
        skill_id: (
            f"---\nname: report-template-{skill_id}\ndescription: {description}\n---\n"
            f"# {skill_id}\n\n" + "先限定证据，再形成判断并设置可观察复核动作。" * 15
        )
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    }
    skills["author-2.2"] += "\n建议按设备额定电流的 20% 设置容量。"
    transferred = [
        "analysis_method",
        "synthesis_method",
        "visual_method",
        "quality_check",
    ]
    excluded = [
        "domain_knowledge",
        "domain_standard_or_threshold",
        "project_fact_or_number",
        "customer_identity",
        "project_finding_or_risk",
        "project_conclusion_or_recommendation",
        "evidence_or_claim_identifier",
    ]
    payload = TemplateSkillSubmission(
        skills=skills,
        boundary_manifest={
            "policy_version": 1,
            "transferred_categories": transferred,
            "excluded_categories": excluded,
            "boundary_statement": (
                "本 Skill 只保留可跨项目复用的分析、综合、图证组织和质量检查方法；"
                "专业机理、标准阈值、客户事实、项目判断、项目建议及证据标识均未迁移，"
                "必须分别由模块 Skill、Knowledge 或当前运行 Evidence 提供。"
            ),
        },
    )
    contract = TemplateDistillationInput(
        run_id="run-1",
        template_ref="template.docx",
        inspect_max_chars=100000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
        allowed_transfer_categories=transferred,
        required_exclusion_categories=excluded,
    )

    with pytest.raises(SubmissionValidationError) as caught:
        SubmitResultTool._validate_template_skill_boundary(
            payload,
            contract,
            "与当前 Skill 无重复的模板源文本。",
        )

    assert caught.value.field == "skills.author-2.2"
    assert "concrete domain number or threshold" in str(caught.value)


@pytest.mark.asyncio
async def test_module_result_part_rejects_nested_numbered_heading_before_write(
    tmp_path: Path,
) -> None:
    writer = WriteResultPartTool(
        "run-1",
        "module-2.1",
        0,
        ReportingStore(tmp_path),
        list(REPORT_TAXONOMY["2.1"].submodules),
        evidence_binding_required=True,
    )

    outcome = await writer(
        part_id="2.1.1",
        content=(
            "## 2.1.1 配电系统负荷分配与过载风险\n\n"
            "### 2.1.1.1 现状描述\n\n完整正文。"
        ),
        evidence_ids=[],
    )

    assert outcome["status"] == "correction_required"
    assert outcome["persisted"] is False
    assert outcome["rewrite_part_ids"] == ["2.1.1"]
    assert "outside the fixed taxonomy" in outcome["validation_errors"][0]["problem"]

    assert not (
        tmp_path / "Work/runs/run-1/drafts/module-2.1/r0/2.1.1.md"
    ).exists()


@pytest.mark.asyncio
async def test_list_result_parts_marks_nested_numbered_heading_for_rewrite(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    root = "Work/runs/run-1/drafts/module-2.1/r0"
    store.write_text(
        f"{root}/2.1.1.md",
        "## 2.1.1 固定小节\n\n### 2.1.1.1 现状描述\n\n完整正文。",
    )
    store.write_json(
        f"{root}/_evidence/2.1.1.json",
        {"evidence_ids": []},
    )
    listing = await ListResultPartsTool(
        "run-1",
        "module-2.1",
        0,
        store,
        ["2.1.1"],
        evidence_binding_required=True,
    )()

    assert listing["parts"][0]["ready"] is False
    assert listing["parts"][0]["structural_errors"] == [
        "### 2.1.1.1 现状描述"
    ]
    assert listing["rewrite_part_ids"] == ["2.1.1"]
    assert listing["complete"] is False


@pytest.mark.asyncio
async def test_module_commit_rejects_preexisting_compaction_marker(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1"
    await _write_bound_module_parts(tmp_path, task_id=task_id)
    ReportingStore(tmp_path).write_text(
        f"Work/runs/run-1/drafts/{task_id}/r0/2.1.1.md",
        "<persisted_result_part sha256=" + "0" * 64 + " characters=1445>",
    )

    outcome = await _tool(tmp_path, task_id=task_id)(**_module_payload())

    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "result_parts.2.1.1.content"
    assert "provider-history compaction marker" in issue["problem"]
    assert "complete intended prose" in issue["repair_instruction"]


@pytest.mark.asyncio
async def test_module_commit_targets_nested_heading_correction_to_exact_part(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1"
    await _write_bound_module_parts(tmp_path, task_id=task_id)
    ReportingStore(tmp_path).write_text(
        f"Work/runs/run-1/drafts/{task_id}/r0/2.1.1.md",
        "## 2.1.1 固定小节\n\n### 2.1.1.1 现状描述\n\n完整正文。",
    )

    outcome = await _tool(tmp_path, task_id=task_id)(**_module_payload())

    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "submodule_narratives.2.1.1"
    correction = json.loads(
        (
            tmp_path
            / "Work/runs/run-1/submissions/module-2.1/correction-state.json"
        ).read_text(encoding="utf-8")
    )
    assert correction["affected_part_ids"] == ["2.1.1"]


@pytest.mark.asyncio
async def test_runtime_claim_boundary_uses_project_evidence_metadata(
    tmp_path: Path,
) -> None:
    evidence = {
        "id": "E-0001",
        "subject": "谐波评估",
        "fact": "既有结论需要补充连续监测数据确认。",
        "source": {
            "file_id": "F-0001",
            "path": "Inputs/test.xlsx",
            "sheet": "评估表",
            "cell": "A1",
        },
        "confidence": 0.65,
        "needs_confirmation": True,
        "module_id": "2.1",
        "submodule_id": "2.1.1",
        "photo_refs": [],
    }
    SourceLedger(tmp_path, "run-1").register_project(
        "E-0001",
        "谐波评估",
        "Inputs/test.xlsx#评估表!A1",
        json.dumps(evidence, ensure_ascii=False),
    )
    task_id = "module-2.1"
    part_ids = list(REPORT_TAXONOMY["2.1"].submodules)
    writer = WriteResultPartTool(
        "run-1",
        task_id,
        0,
        ReportingStore(tmp_path),
        part_ids,
        evidence_binding_required=True,
    )
    for part_id in part_ids:
        await writer(
            part_id=part_id,
            content=f"### {part_id}\n\n由项目证据支持的完整正文。",
            evidence_ids=["E-0001"] if part_id == "2.1.1" else [],
        )

    outcome = await _tool(tmp_path, task_id=task_id)(**_module_payload())

    assert outcome["status"] == "completed", outcome
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    claims = {claim["submodule_id"]: claim for claim in result["payload"]["claims"]}
    assert claims["2.1.1"]["confidence"] == 0.65
    assert claims["2.1.1"]["unresolved"] is True
    assert claims["2.1.1"]["footnote_required"] is True
    assert claims["2.1.2"]["confidence"] == 0.0
    assert claims["2.1.2"]["unresolved"] is True
    assert claims["2.1.2"]["footnote_required"] is False


@pytest.mark.asyncio
async def test_module_revision_commit_uses_bound_target_part(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1-revision-r1"
    subject = _module_subject_payload()
    contract_ref = _write_revision_contract(
        tmp_path,
        subject=subject,
        target_submodule_ids=["2.1.1"],
    )
    await _write_bound_module_parts(
        tmp_path,
        task_id=task_id,
        revision=1,
        part_ids=["2.1.1"],
    )
    payload = {
        "kind": "module_revision_submission",
        "module_id": "2.1",
        "base_revision": 0,
        "revision": 1,
        "unresolved_questions": [],
        "revision_responses": [
            {
                "finding_id": "M-001",
                "action": "implemented",
                "summary": "已完成目标小节的定向修订，并保留所有未变更内容与证据边界。",
                "changed_target_ids": ["2.1.1"],
            }
        ],
    }
    tool = _tool(
        tmp_path,
        task_id=task_id,
        allowed_outputs=["module_revision_submission"],
        revision=1,
        input_contract_kind="module_revision_input",
        input_contract_ref=contract_ref,
    )

    outcome = await tool(**payload)

    assert outcome["status"] == "completed"
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert set(result["payload"]["submodule_narratives"]) == {"2.1.1"}
    assert result["payload"]["claims_upsert"][0]["id"] == "C-2.1-2-1-1"


@pytest.mark.asyncio
async def test_module_commit_rejects_legacy_artifact_ref_fields(
    tmp_path: Path,
) -> None:
    foreign_writer = WriteResultPartTool(
        "run-1", "foreign-task", 1, ReportingStore(tmp_path), ["2.1.1"]
    )
    artifact = await foreign_writer(part_id="2.1.1", content="不应读取的正文")
    payload = {
        "kind": "module_revision_submission",
        "module_id": "2.1",
        "base_revision": 0,
        "revision": 1,
        "submodule_narratives": {"2.1.1": artifact["artifact_ref"]},
        "claims_upsert": [],
        "claim_ids_remove": [],
        "source_ids": [],
        "unresolved_questions": [],
        "revision_responses": [
            {
                "finding_id": "M-001",
                "action": "implemented",
                "summary": "已尝试提交目标小节的定向修订，并保持其他模块内容不发生变化。",
                "changed_target_ids": ["2.1.1"],
            }
        ],
    }

    outcome = await _tool(
        tmp_path,
        task_id="module-2.1-revision-r1",
        allowed_outputs=["module_revision_submission"],
        revision=1,
    )(**payload)

    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"].startswith("submodule_narratives")
    assert "Extra inputs are not permitted" in issue["problem"]


@pytest.mark.asyncio
async def test_module_revision_runtime_replaces_target_claim_bindings(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1-revision-r1"
    subject = _module_subject_payload()
    subject["submodule_narratives"]["2.1.1"] = (
        "### 2.1.1\n\n事实一 [[CLAIM:C-2.1-001]]；事实二 [[CLAIM:C-2.1-002]]。"
    )
    subject["claims"] = [
        {
            "id": f"C-2.1-{index:03d}",
            "module_id": "2.1",
            "submodule_id": "2.1.1",
            "text": f"事实 {index}",
            "claim_type": "project_fact",
            "source_ids": ["E-0001"],
            "confidence": 1.0,
            "footnote_required": True,
            "unresolved": False,
        }
        for index in (1, 2)
    ]
    subject["source_ids"] = ["E-0001"]
    contract_ref = _write_revision_contract(
        tmp_path,
        subject=subject,
        target_submodule_ids=["2.1.1"],
    )
    await _write_bound_module_parts(
        tmp_path,
        task_id=task_id,
        revision=1,
        part_ids=["2.1.1"],
    )
    payload = {
        "kind": "module_revision_submission",
        "module_id": "2.1",
        "base_revision": 0,
        "revision": 1,
        "unresolved_questions": [],
        "revision_responses": [
            {
                "finding_id": "M-001",
                "action": "implemented",
                "summary": "已按审计要求重写目标小节，并声明保留其他内容与证据边界。",
                "changed_target_ids": ["2.1.1"],
            }
        ],
    }
    tool = _tool(
        tmp_path,
        task_id=task_id,
        allowed_outputs=["module_revision_submission"],
        revision=1,
        input_contract_kind="module_revision_input",
        input_contract_ref=contract_ref,
    )

    outcome = await tool(**payload)

    assert outcome["status"] == "completed", outcome
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert result["payload"]["claim_ids_remove"] == [
        "C-2.1-001",
        "C-2.1-002",
    ]
    assert result["payload"]["claims_upsert"][0]["id"] == "C-2.1-2-1-1"


@pytest.mark.asyncio
async def test_result_parts_report_missing_declared_ids(tmp_path: Path) -> None:
    store = ReportingStore(tmp_path)
    writer = WriteResultPartTool("run-1", "task", 0, store, ["part-a", "part-b"])
    await writer(part_id="part-a", content="正文")
    listing = await ListResultPartsTool(
        "run-1",
        "task",
        0,
        store,
        ["part-a", "part-b"],
        required_synthesis_input_ids=["SI-001", "SI-002"],
    )()
    assert listing["missing_part_ids"] == ["part-b"]
    assert listing["required_synthesis_input_ids"] == ["SI-001", "SI-002"]
    assert listing["complete"] is False


@pytest.mark.asyncio
async def test_batch_result_parts_persist_module_prose_and_evidence_bindings(
    tmp_path: Path,
) -> None:
    SourceLedger(tmp_path, "run-1").register_project(
        "E-0001",
        "测试证据",
        "Inputs/test.txt",
        "测试事实",
    )
    store = ReportingStore(tmp_path)
    writer = WriteResultPartsTool(
        "run-1",
        "module-2.1",
        0,
        store,
        ["2.1.1", "2.1.2"],
        evidence_binding_required=True,
        max_batch_size=4,
    )

    result = await writer(
        parts=[
            {
                "part_id": "2.1.1",
                "content": "### 2.1.1\n\n第一段完整正文。",
                "evidence_ids": ["E-0001"],
            },
            {
                "part_id": "2.1.2",
                "content": "### 2.1.2\n\n第二段明确记录证据缺口。",
                "evidence_ids": [],
            },
        ]
    )

    assert result["status"] == "completed"
    assert result["count"] == 2
    assert [part["part_id"] for part in result["parts"]] == ["2.1.1", "2.1.2"]
    root = tmp_path / "Work/runs/run-1/drafts/module-2.1/r0"
    assert (root / "2.1.1.md").read_text(encoding="utf-8").endswith("第一段完整正文。")
    assert json.loads(
        (root / "_evidence/2.1.1.json").read_text(encoding="utf-8")
    )["evidence_ids"] == ["E-0001"]
    assert json.loads(
        (root / "_evidence/2.1.2.json").read_text(encoding="utf-8")
    )["evidence_ids"] == []


@pytest.mark.asyncio
async def test_batch_result_parts_validate_every_item_before_writing(
    tmp_path: Path,
) -> None:
    SourceLedger(tmp_path, "run-1").register_project(
        "E-0001",
        "测试证据",
        "Inputs/test.txt",
        "测试事实",
    )
    writer = WriteResultPartsTool(
        "run-1",
        "module-2.1",
        0,
        ReportingStore(tmp_path),
        ["2.1.1", "2.1.2"],
        evidence_binding_required=True,
        max_batch_size=4,
    )

    with pytest.raises(ValueError, match="requires evidence_ids"):
        await writer(
            parts=[
                {
                    "part_id": "2.1.1",
                    "content": "本项本身有效，但整批失败时不得落盘。",
                    "evidence_ids": ["E-0001"],
                },
                {
                    "part_id": "2.1.2",
                    "content": "缺少模块证据绑定。",
                },
            ]
        )

    root = tmp_path / "Work/runs/run-1/drafts/module-2.1/r0"
    assert not (root / "2.1.1.md").exists()
    assert not (root / "2.1.2.md").exists()


@pytest.mark.asyncio
async def test_batch_result_parts_reject_duplicate_ids_without_writing(
    tmp_path: Path,
) -> None:
    writer = WriteResultPartsTool(
        "run-1",
        "chief-edit",
        0,
        ReportingStore(tmp_path),
        ["assessment_background"],
        max_batch_size=8,
    )

    with pytest.raises(ValueError, match="duplicate part_id"):
        await writer(
            parts=[
                {"part_id": "assessment_background", "content": "第一版正文。"},
                {"part_id": "assessment_background", "content": "重复正文。"},
            ]
        )

    assert not (
        tmp_path
        / "Work/runs/run-1/drafts/chief-edit/r0/assessment_background.md"
    ).exists()


@pytest.mark.asyncio
async def test_module_commit_rejects_model_authored_claims(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    payload = _module_payload()
    payload["claims"] = {"item": []}
    outcome = await tool(**payload)
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "claims"
    assert issue["received_type"] == "object"
    assert "claims" not in issue["example"]
    assert "Remove undeclared field claims completely" in issue["repair_instruction"]
    assert "null, an empty array" in issue["repair_instruction"]


def test_module_contract_hides_claims_markers_and_artifact_refs() -> None:
    schema = submission_schema("module_submission")
    example = schema["examples"][0]

    assert "kind" in schema["required"]
    assert set(example) == {
        "kind",
        "module_id",
        "unresolved_questions",
        "revision",
        "revision_responses",
    }

    rendered = render_submission_contract("module_submission")
    assert "kind (required)" in rendered
    assert "final commit contains only the fields declared by this schema" in rendered
    assert "[[CLAIM:<Claim.id>]]" not in rendered


def test_all_model_facing_reporting_contracts_hide_runtime_claim_protocol() -> None:
    forbidden = (
        "protected_claim_ids",
        "affected_claim_ids",
        "claim_ids",
        "claims_upsert",
        "claim_ids_remove",
        "[[CLAIM:",
        "C-*",
    )
    for kind in (
        "module_submission",
        "module_revision_submission",
        "module_review_finding_submission",
        "module_review_verdict_submission",
        "cross_review_finding_submission",
        "cross_review_verdict_submission",
        "edited_report_submission",
        "chief_revision_submission",
    ):
        rendered = json.dumps(submission_schema(kind), ensure_ascii=False)
        for item in forbidden:
            assert item not in rendered


@pytest.mark.asyncio
async def test_module_commit_reports_unbound_part_without_claim_feedback(
    tmp_path: Path,
) -> None:
    task_id = "module-2.3"
    store = ReportingStore(tmp_path)
    writer = WriteResultPartTool(
        "run-1",
        task_id,
        0,
        store,
        list(REPORT_TAXONOMY["2.3"].submodules),
    )
    part_ids = list(REPORT_TAXONOMY["2.3"].submodules)
    for part_id in part_ids:
        await writer(part_id=part_id, content=f"### {part_id}\n\n旧协议正文。")
    payload = {
        "kind": "module_submission",
        "module_id": "2.3",
        "unresolved_questions": [],
        "revision": 0,
        "revision_responses": [],
    }

    outcome = await _tool(tmp_path, task_id=task_id)(**payload)

    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == f"result_parts.{part_ids[0]}"
    assert issue["received"] == {
        "prose_saved": True,
        "evidence_binding_saved": False,
    }
    assert "evidence_ids" in issue["repair_instruction"]
    assert "Claim id" in issue["repair_instruction"]


@pytest.mark.asyncio
async def test_distinct_submission_errors_do_not_exhaust_three_attempts(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)

    first = await tool(kind="wrong-kind")
    second = await tool()
    third_payload = _module_payload()
    third_payload["claims"] = {"item": []}
    third = await tool(**third_payload)
    fourth_payload = _module_payload()
    fourth_payload["module_id"] = "9.9"
    fourth = await tool(**fourth_payload)

    assert [first["status"], second["status"], third["status"], fourth["status"]] == [
        "correction_required",
        "correction_required",
        "correction_required",
        "correction_required",
    ]
    assert fourth["remaining_attempts"] == 4

    repeated = await tool(**fourth_payload)
    assert repeated["status"] == "failed"
    assert "same validation defect was repeated" in repeated["error"]


@pytest.mark.asyncio
async def test_module_authoring_output_is_checked_against_visible_input_contract(
    tmp_path: Path,
) -> None:
    contract_ref = "Work/runs/run-1/context/module-authoring.json"
    ReportingStore(tmp_path).write_json(
        contract_ref,
        {
            "kind": "module_authoring_input",
            "run_id": "run-1",
            "module_id": "2.1",
            "revision": 1,
            "required_submodule_ids": list(REPORT_TAXONOMY["2.1"].submodules),
            "coverage_ref": "Work/coverage.json",
            "evidence_ref": "Work/evidence.jsonl",
            "manifest_ref": "Work/manifest.json",
            "knowledge_ref": "Work/runs/run-1/knowledge/module-2.1.md",
            "saved_part_ids": [],
            "rewrite_part_ids": [],
        },
    )
    tool = _tool(
        tmp_path,
        input_contract_kind="module_authoring_input",
        input_contract_ref=contract_ref,
    )
    outcome = await tool(**_module_payload())
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert "identity differs" in issue["problem"]
    assert issue["field"] == "$identity"
    assert issue["received"] == {"module_id": "2.1", "revision": 0}
    assert issue["example"] == {"module_id": "2.1", "revision": 1}
    assert "Copy module_id and revision exactly" in issue["repair_instruction"]


@pytest.mark.asyncio
async def test_chief_submission_rejects_deleted_cross_fields_and_traces_regular_tables(
    tmp_path: Path,
) -> None:
    contract_ref = _write_chief_contract_and_claim_ledger(tmp_path)
    task_id = "chief-edit"
    part_ids = [
        "risk_panorama",
        "dimension_risk_analysis",
        "improvement_action_plan",
    ]
    writer = WriteResultPartTool(
        "run-1",
        task_id,
        0,
        ReportingStore(tmp_path),
        part_ids,
    )
    refs = {}
    for part_id in part_ids:
        result = await writer(
            part_id=part_id,
            content=f"{part_id} 当前实际章节正文。",
        )
        refs[part_id] = result["artifact_ref"]

    payload = {
        "kind": "edited_report_submission",
        "title": "报告",
        "assessment_background": "背景",
        "findings_overview": "发现",
        "regional_executive_summary": "摘要",
        "module_narratives": {
            module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_TAXONOMY
        },
        "risk_panorama": refs["risk_panorama"],
        "dimension_risk_analysis": refs["dimension_risk_analysis"],
        "data_gap_analysis": "缺口",
        "improvement_action_plan": refs["improvement_action_plan"],
        "special_topic_analysis": (
            "### 4.1 动态专项问题\n\n"
            "说明项目事实边界、方案条件和验证方法，通用知识不作为客户事实。"
        ),
        "tables": [
            {
                "title": "证据表",
                "headers": ["对象", "判断"],
                "rows": [["供电边界", "需要复核"]],
                "evidence_ids": ["E-0001"],
            },
        ],
        "photo_ids": [],
        "unresolved_editorial_issues": [],
        "revision_responses": [],
    }
    tool = _tool(
        tmp_path,
        task_id=task_id,
        allowed_outputs=["edited_report_submission"],
        input_contract_kind="chief_editor_input",
        input_contract_ref=contract_ref,
    )

    legacy = await tool(**{**payload, "synthesis_dispositions": []})
    outcome = await tool(**payload)

    assert legacy["status"] == "correction_required"
    assert legacy["validation_errors"][0]["field"] == "synthesis_dispositions"
    assert outcome["status"] == "completed", outcome
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/chief-edit.json").read_text(encoding="utf-8")
    )["payload"]
    assert "synthesis_dispositions" not in result
    assert "synthesis_tables" not in result
    assert result["tables"][0]["source_ids"] == ["E-0001"]
    assert result["tables"][0]["claim_ids"] == ["C-2.1-001"]


@pytest.mark.asyncio
async def test_chief_revision_commit_is_compact_and_reads_only_assigned_parts(
    tmp_path: Path,
) -> None:
    baseline = EditedReportSubmission(
        title="报告",
        assessment_background="背景原文",
        findings_overview="发现原文",
        regional_executive_summary="摘要原文",
        module_narratives={
            module_id: f"{module_id} 已批准且不可编辑正文" for module_id in REPORT_TAXONOMY
        },
        risk_panorama="风险全景原文",
        dimension_risk_analysis="维度分析原文",
        data_gap_analysis="数据缺口原文",
        improvement_action_plan="行动计划原文",
        special_topic_plan=_special_topic_plan(),
        special_topic_analysis=(
            "### 4.1 动态专项问题\n\n"
            "原专项分析已说明项目边界、方案条件和验证方法。"
        ),
        photo_ids=["P-001"],
        unresolved_editorial_issues=["保留的透明限制"],
    )
    subject_ref = "Work/runs/run-1/edited-revisions/chief-r0.json"
    contract_ref = "Work/runs/run-1/reviews/chief-revision-input-r1.json"
    finding = {
        "id": "F-001",
        "target_section_ids": ["3.1.2"],
        "target_changes": [
            {
                "target_section_id": "3.1.2",
                "required_change": "在该小节补充明确的行动依赖顺序和联合验收方法。",
                "reviewer_checks": ["行动依赖和联合验收都已形成可核对闭环"],
            }
        ],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "当前维度综合分析缺少行动依赖顺序和联合验收闭环。",
        "evidence_refs": [subject_ref],
    }
    store = ReportingStore(tmp_path)
    store.write_json(subject_ref, baseline.model_dump(mode="json"))
    contract = ChiefRevisionInput(
        run_id="run-1",
        subject_ref=subject_ref,
        target_section_bodies={"3.1.2": baseline.dimension_risk_analysis},
        consistency_context={
            "1.1": baseline.assessment_background,
            "1.2": baseline.findings_overview,
            "1.3": baseline.regional_executive_summary,
            "3.1.1": baseline.risk_panorama,
            "3.2": baseline.improvement_action_plan,
        },
        revision=1,
        target_section_ids=["3.1.2"],
        findings=[finding],
    )
    store.write_json(contract_ref, contract.model_dump(mode="json"))
    writer = WriteResultPartTool(
        "run-1",
        "chief-edit-r1",
        1,
        store,
        ["dimension_risk_analysis"],
    )
    await writer(
        part_id="dimension_risk_analysis",
        content="修订后的维度综合分析，明确前置动作、责任接口和联合验收。",
    )
    tool = _tool(
        tmp_path,
        task_id="chief-edit-r1",
        allowed_outputs=["chief_revision_submission"],
        revision=1,
        input_contract_kind="chief_revision_input",
        input_contract_ref=contract_ref,
    )
    outcome = await tool(
        **{
            "kind": "chief_revision_submission",
            "base_subject_ref": subject_ref,
            "revision": 1,
            "revision_responses": [
                {
                    "finding_id": "F-001",
                    "action": "implemented",
                    "summary": "已在指定小节补充行动依赖、责任接口和联合验收。",
                    "changed_target_ids": ["3.1.2"],
                }
            ],
        }
    )

    assert outcome["status"] == "completed", outcome
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/chief-edit-r1.json").read_text(encoding="utf-8")
    )["payload"]
    assert set(result) == {
        "kind",
        "base_subject_ref",
        "revision",
        "section_bodies",
        "section_part_refs",
        "revision_responses",
    }
    assert result["section_bodies"] == {
        "3.1.2": "修订后的维度综合分析，明确前置动作、责任接口和联合验收。"
    }
    assert "module_narratives" not in result
    assert "synthesis_dispositions" not in result


@pytest.mark.asyncio
async def test_markdown_aggregate_contract_rejects_unverified_bindings_without_clearing(
    tmp_path: Path,
) -> None:
    contract_ref = "Work/runs/run-1/context/aggregate-editor-input.json"
    markers = {module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_TAXONOMY}
    ReportingStore(tmp_path).write_json(
        contract_ref,
        {
            "kind": "aggregate_editor_input",
            "run_id": "run-1",
            "source_format": "markdown",
            "approved_module_markers": markers,
            "special_topic_plan": _special_topic_plan(),
            "structured_modules": {},
            "markdown_modules": {
                module_id: f"模块 {module_id} 正文" for module_id in REPORT_TAXONOMY
            },
        },
    )
    payload = {
        "kind": "edited_report_submission",
        "title": "报告",
        "assessment_background": "背景",
        "findings_overview": "发现",
        "regional_executive_summary": "摘要",
        "module_narratives": markers,
        "risk_panorama": "风险",
        "dimension_risk_analysis": "维度",
        "data_gap_analysis": "缺口",
        "improvement_action_plan": "行动",
        "special_topic_analysis": (
            "### 4.1 动态专项问题\n\n"
            "说明项目边界、方案条件和验证方法。"
        ),
        "protected_claim_ids": ["C-invented"],
        "tables": [],
        "photo_ids": [],
        "unresolved_editorial_issues": [],
        "revision_responses": [],
    }
    tool = _tool(
        tmp_path,
        allowed_outputs=["edited_report_submission"],
        input_contract_kind="aggregate_editor_input",
        input_contract_ref=contract_ref,
    )
    outcome = await tool(**payload)
    assert outcome["status"] == "correction_required"
    assert outcome["validation_errors"][0]["field"] == "protected_claim_ids"
    assert "Extra inputs are not permitted" in outcome["validation_errors"][0]["problem"]
    raw = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["raw_payload"]["protected_claim_ids"] == ["C-invented"]
