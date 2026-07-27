from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.agentic_models import (
    ClaimRecord,
    EditedReportSubmission,
    ModuleSubmission,
)
from manyselves.core.reporting.claim_ledger import ClaimLedger
from manyselves.core.reporting.input_contracts import (
    ChiefEditorInput,
    ChiefRevisionInput,
    ModuleContentView,
    ModuleReviewInput,
    ValidationReport,
    final_audit_content_view,
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
    SubmitResultTool,
    WriteResultPartTool,
)


def _tool(
    workspace: Path,
    *,
    task_id: str = "task",
    allowed_outputs: list[str] | None = None,
    revision: int = 0,
    input_contract_kind: str | None = None,
    input_contract_ref: str | None = None,
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
            "subject": module_content_view(subject_model).model_dump(mode="json"),
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
        "target_report_section_ids": ["3.1.1", "3.1.3", "3.2"],
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
        cross_synthesis_inputs=[_cross_synthesis_input()],
        cross_review_completion_ref="Work/runs/run-1/reviews/cross-completion.json",
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
    outcome = await tool(payload=bad)
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "kind"
    assert issue["received_type"] == "string"
    assert issue["received"] == "cross_review_submission"
    assert issue["expected"] == "one of ['module_submission']"
    assert issue["example"] == "module_submission"
    assert "native JSON object" in issue["repair_instruction"]
    assert outcome["remaining_attempts"] == 7
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["raw_payload"] == bad


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
            run_id="run-1",
            subject_ref="Work/runs/run-1/modules/2.1-r0.json",
            validator="test/v1",
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
        payload={
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
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/module-2.1-review-r0.json").read_text(encoding="utf-8")
    )
    assert result["payload"]["coverage"] == {"submodule_ids": ["2.1.1"]}
    assert result["payload"]["findings"][0]["id"] == ("M-2.1-initial-r0-001")


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

    outcome = await _tool(tmp_path)(payload=_module_payload())

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
async def test_submit_result_does_not_decode_or_repair_string_payload(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    candidate = json.dumps(_module_payload())
    outcome = await tool(payload=candidate)
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "payload"
    assert issue["received_type"] == "string"
    assert issue["received"] == candidate
    assert "JSON object" in issue["problem"]
    assert issue["expected"] == "a native JSON object, not a JSON-encoded string"
    assert issue["example"]["kind"] == "module_submission"
    assert "Do not quote or JSON-stringify" in issue["repair_instruction"]
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["raw_payload"] == candidate


@pytest.mark.asyncio
async def test_missing_kind_uses_the_only_allowed_contract_for_correction(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    payload = _module_payload()
    payload.pop("kind")

    outcome = await tool(payload=payload)

    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "kind"
    assert issue["received_type"] == "null"
    assert issue["expected"] == "one of ['module_submission']"
    assert issue["example"] == "module_submission"
    assert "payload.kind" in issue["repair_instruction"]


@pytest.mark.asyncio
async def test_string_payload_example_uses_current_module_authoring_contract(
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

    outcome = await tool(payload='{"kind":"module_submission"}')

    issue = outcome["validation_errors"][0]
    example = issue["example"]
    assert example["module_id"] == "2.2"
    assert example["revision"] == 0
    assert "submodule_narratives" not in example
    assert "claims" not in example
    assert "source_ids" not in example


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
    outcome = await tool(payload=payload)
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
    first = await tool(payload=bad)
    assert first["status"] == "correction_required"
    outcome = await tool(payload=bad)
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
    outcome = await tool(payload=payload)
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert result["payload"]["kind"] == "module_revision_submission"
    assert set(result["payload"]["submodule_narratives"]) == {"2.1.1"}
    assert result["payload"]["claims_upsert"][0]["id"] == "C-2.1-2-1-1"
    assert result["payload"]["source_ids"] == ["E-0001"]
    assert "markdown" not in result["payload"]


@pytest.mark.asyncio
async def test_module_commit_materializes_bound_parts_without_model_refs(
    tmp_path: Path,
) -> None:
    task_id = "module-2.1"
    await _write_bound_module_parts(tmp_path, task_id=task_id)
    tool = _tool(tmp_path, task_id=task_id)
    outcome = await tool(payload=_module_payload())
    result = json.loads((tmp_path / outcome["result_path"]).read_text(encoding="utf-8"))
    assert "由项目证据支持的完整正文。" in result["payload"]["submodule_narratives"]["2.1.1"]
    assert "[[CLAIM:C-2.1-2-1-1]]" in result["payload"]["submodule_narratives"]["2.1.1"]
    assert result["payload"]["claims"][0]["source_ids"] == ["E-0001"]


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

    outcome = await tool(payload=payload)

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
    )(payload=payload)

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

    outcome = await tool(payload=payload)

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
        required_synthesis_table_types=[
            "risk_cluster_matrix",
            "action_dependency_matrix",
        ],
    )()
    assert listing["missing_part_ids"] == ["part-b"]
    assert listing["required_synthesis_input_ids"] == ["SI-001", "SI-002"]
    assert listing["required_synthesis_table_types"] == [
        "risk_cluster_matrix",
        "action_dependency_matrix",
    ]
    assert listing["complete"] is False


@pytest.mark.asyncio
async def test_module_commit_rejects_model_authored_claims(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    payload = _module_payload()
    payload["claims"] = {"item": []}
    outcome = await tool(payload=payload)
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert issue["field"] == "claims"
    assert issue["received_type"] == "object"
    assert "claims" not in issue["example"]
    assert "Correct claims" in issue["repair_instruction"]


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

    outcome = await _tool(tmp_path, task_id=task_id)(payload=payload)

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

    first = await tool(payload={"kind": "wrong-kind"})
    second = await tool(payload="not-an-object")
    third_payload = _module_payload()
    third_payload["claims"] = {"item": []}
    third = await tool(payload=third_payload)
    fourth_payload = _module_payload()
    fourth_payload["module_id"] = "9.9"
    fourth = await tool(payload=fourth_payload)

    assert [first["status"], second["status"], third["status"], fourth["status"]] == [
        "correction_required",
        "correction_required",
        "correction_required",
        "correction_required",
    ]
    assert fourth["remaining_attempts"] == 4

    repeated = await tool(payload=fourth_payload)
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
    outcome = await tool(payload=_module_payload())
    assert outcome["status"] == "correction_required"
    issue = outcome["validation_errors"][0]
    assert "identity differs" in issue["problem"]
    assert issue["field"] == "$identity"
    assert issue["received"] == {"module_id": "2.1", "revision": 0}
    assert issue["example"] == {"module_id": "2.1", "revision": 1}
    assert "Copy module_id and revision exactly" in issue["repair_instruction"]


@pytest.mark.asyncio
async def test_chief_submission_accounts_for_cross_inputs_and_derives_table_traceability(
    tmp_path: Path,
) -> None:
    contract_ref = _write_chief_contract_and_claim_ledger(tmp_path)
    task_id = "chief-edit"
    part_ids = [
        "risk_panorama",
        "dimension_risk_analysis",
        "cross_module_analysis",
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
            content=f"{part_id} 对 SI-001 的系统级整合正文。",
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
        "cross_module_analysis": refs["cross_module_analysis"],
        "data_gap_analysis": "缺口",
        "improvement_action_plan": refs["improvement_action_plan"],
        "new_factory_planning": "新建",
        "capacity_expansion_plan": "增容",
        "daily_power_management": "日常",
        "emergency_compliance_management": "应急",
        "tables": [],
        "synthesis_dispositions": [
            {
                "synthesis_input_id": "SI-001",
                "status": "integrated",
                "target_section_ids": ["3.1.1", "3.1.3", "3.2"],
                "result_part_refs": [
                    refs["risk_panorama"],
                    refs["cross_module_analysis"],
                    refs["improvement_action_plan"],
                ],
                "merged_into_ids": [],
                "integration_summary": "该系统风险簇已进入风险全景、跨模块分析和行动计划。",
            }
        ],
        "synthesis_tables": [
            {
                "table_type": "risk_cluster_matrix",
                "title": "系统风险簇矩阵",
                "headers": ["风险簇", "影响"],
                "rows": [["供电边界与环境压力", "扩大异常影响范围"]],
                "synthesis_input_ids": ["SI-001"],
                "row_synthesis_input_ids": [["SI-001"]],
            },
            {
                "table_type": "action_dependency_matrix",
                "title": "联合行动依赖矩阵",
                "headers": ["前置动作", "后续动作"],
                "rows": [["确认供电边界", "完成环境整改与复测"]],
                "synthesis_input_ids": ["SI-001"],
                "row_synthesis_input_ids": [["SI-001"]],
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

    incomplete = await tool(payload={**payload, "synthesis_dispositions": []})
    outcome = await tool(payload=payload)

    assert incomplete["status"] == "correction_required"
    assert incomplete["validation_errors"][0]["field"] == (
        "synthesis_dispositions.synthesis_input_id"
    )
    assert outcome["status"] == "completed", outcome
    result = json.loads(
        (tmp_path / "Work/runs/run-1/results/chief-edit.json").read_text(encoding="utf-8")
    )["payload"]
    assert result["synthesis_dispositions"][0]["synthesis_input_id"] == "SI-001"
    assert result["synthesis_tables"][0]["source_ids"] == ["E-0001"]
    assert result["synthesis_tables"][0]["claim_ids"] == ["C-2.1-001"]
    assert result["synthesis_tables"][0]["row_synthesis_input_ids"] == [["SI-001"]]


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
        cross_module_analysis="跨模块分析原文",
        data_gap_analysis="数据缺口原文",
        improvement_action_plan="行动计划原文",
        new_factory_planning="新建规划原文",
        capacity_expansion_plan="增容规划原文",
        daily_power_management="日常管理原文",
        emergency_compliance_management="应急合规原文",
        photo_ids=["P-001"],
        unresolved_editorial_issues=["保留的透明限制"],
    )
    subject_ref = "Work/runs/run-1/edited-revisions/chief-r0.json"
    contract_ref = "Work/runs/run-1/reviews/chief-revision-input-r1.json"
    finding = {
        "id": "F-001",
        "target_section_ids": ["3.1.3"],
        "target_changes": [
            {
                "target_section_id": "3.1.3",
                "required_change": "在该小节补充明确的行动依赖顺序和联合验收方法。",
                "reviewer_checks": ["行动依赖和联合验收都已形成可核对闭环"],
            }
        ],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "当前跨模块分析缺少行动依赖顺序和联合验收闭环。",
        "evidence_refs": [subject_ref],
    }
    store = ReportingStore(tmp_path)
    store.write_json(subject_ref, baseline.model_dump(mode="json"))
    contract = ChiefRevisionInput(
        run_id="run-1",
        subject_ref=subject_ref,
        subject=final_audit_content_view(baseline),
        revision=1,
        target_section_ids=["3.1.3"],
        findings=[finding],
        cross_synthesis_inputs=[],
    )
    store.write_json(contract_ref, contract.model_dump(mode="json"))
    writer = WriteResultPartTool(
        "run-1",
        "chief-edit-r1",
        1,
        store,
        ["cross_module_analysis"],
    )
    await writer(
        part_id="cross_module_analysis",
        content="修订后的跨模块分析，明确前置动作、责任接口和联合验收。",
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
        payload={
            "kind": "chief_revision_submission",
            "base_subject_ref": subject_ref,
            "revision": 1,
            "revision_responses": [
                {
                    "finding_id": "F-001",
                    "action": "implemented",
                    "summary": "已在指定小节补充行动依赖、责任接口和联合验收。",
                    "changed_target_ids": ["3.1.3"],
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
        "3.1.3": "修订后的跨模块分析，明确前置动作、责任接口和联合验收。"
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
        "cross_module_analysis": "关联",
        "risk_panorama": "风险",
        "dimension_risk_analysis": "维度",
        "data_gap_analysis": "缺口",
        "improvement_action_plan": "行动",
        "new_factory_planning": "新建",
        "capacity_expansion_plan": "增容",
        "daily_power_management": "日常",
        "emergency_compliance_management": "应急",
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
    outcome = await tool(payload=payload)
    assert outcome["status"] == "correction_required"
    assert outcome["validation_errors"][0]["field"] == "protected_claim_ids"
    assert "Extra inputs are not permitted" in outcome["validation_errors"][0]["problem"]
    raw = json.loads(
        (tmp_path / "Work/runs/run-1/submissions/task/attempt-1-raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["raw_payload"]["protected_claim_ids"] == ["C-invented"]
