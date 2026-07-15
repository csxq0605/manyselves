import pytest
from pydantic import ValidationError

from autoreport.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    ClaimRecord,
    ModuleSubmission,
    SourceKind,
    SourceRecord,
    TaskEnvelope,
)


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
            locator="Knowledge/01_页面导入知识库/a.md",
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
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    assert "事实：" not in submission.markdown
