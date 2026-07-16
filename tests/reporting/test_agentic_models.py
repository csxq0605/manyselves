import pytest
from pydantic import ValidationError

from autoreport.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    AuditSubmission,
    ClaimRecord,
    ModuleSubmission,
    SourceKind,
    SourceRecord,
    TaskEnvelope,
)
from autoreport.core.reporting.models import ReviewIssue
from autoreport.core.reporting.taxonomy import REPORT_TAXONOMY


def _narratives(module_id: str) -> dict[str, str]:
    return {
        submodule_id: f"{submodule_id} 暂无充分客户证据。"
        for submodule_id in REPORT_TAXONOMY[module_id].submodules
    }


def test_task_envelope_keeps_dynamic_context_out_of_identity():
    envelope = TaskEnvelope(
        task_id="task-24",
        run_id="run-1",
        agent_id="module-2.4-specialist",
        objective="完成 2.4 配电设备与元件风险分析",
        input_refs=["evidence.jsonl"],
        allowed_outputs=["module_submission"],
    )
    assert envelope.agent_id == "module-2.4-specialist"
    assert envelope.revision == 0


def test_project_fact_claim_cannot_use_only_reference_source():
    with pytest.raises(ValidationError, match="project_fact.*E-"):
        ClaimRecord(
            id="C-001",
            module_id="2.4",
            submodule_id="2.4.2.3",
            text="现场断路器存在过热",
            claim_type="project_fact",
            source_ids=["R-001"],
        )


def test_source_prefix_must_match_kind():
    with pytest.raises(ValidationError, match="source id"):
        SourceRecord(
            id="W-001",
            kind=SourceKind.LOCAL_REFERENCE,
            title="参考条款",
            locator="Knowledge/参考/a.md",
        )


def test_agent_result_requires_typed_payload_when_completed():
    with pytest.raises(ValidationError, match="payload"):
        AgentResult(
            task_id="task-24",
            run_id="run-1",
            agent_id="module-2.4-specialist",
            session_id="session-24",
            status=AgentRunStatus.COMPLETED,
        )


def test_module_submission_preserves_free_form_markdown():
    submission = ModuleSubmission(
        module_id="2.4",
        markdown="## 2.4 配电设备与元件\n\n温升与连接状态应结合分析。",
        submodule_narratives=_narratives("2.4"),
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    assert "事实：" not in submission.markdown


def test_module_submission_requires_exact_fixed_submodules():
    with pytest.raises(ValidationError, match="exact fixed submodules"):
        ModuleSubmission(
            module_id="2.4",
            markdown="设备分析",
            submodule_narratives={"2.4.1": "错误的非固定子模块"},
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )


def test_claim_submodule_must_belong_to_claim_module():
    with pytest.raises(ValidationError, match="does not belong to module 2.4"):
        ClaimRecord(
            id="C-2.4-001",
            module_id="2.4",
            submodule_id="2.1.1",
            text="错误归类",
            claim_type="technical_interpretation",
        )


def test_blocking_audit_issue_can_be_module_scoped():
    submission = AuditSubmission(
        module_id="2.4",
        approved=False,
        issues=[
            ReviewIssue(
                module_id="2.4",
                kind="unsupported",
                message="需要模块级复核",
                severity="blocking",
            )
        ],
        checked_claim_ids=[],
    )

    assert submission.issues[0].submodule_id is None


def test_audit_submission_rejects_untyped_review_issue():
    with pytest.raises(ValidationError, match="issues"):
        AuditSubmission(
            module_id="2.4",
            approved=False,
            issues=[{"arbitrary": "value"}],
            checked_claim_ids=[],
        )
