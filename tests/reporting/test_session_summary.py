from pathlib import Path

import pytest

from manyselves.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.core.reporting.session_summary import SessionSummaryBuilder, SessionSummaryStore


def _result() -> AgentResult:
    return AgentResult(
        task_id="module-2.1",
        run_id="run-summary",
        agent_id="module-2.1-specialist",
        session_id="session-summary",
        status=AgentRunStatus.COMPLETED,
        payload=ModuleSubmission(
            module_id="2.1",
            submodule_narratives={
                "2.1.1": "负荷容量。",
                "2.1.2": "关键负荷。",
                "2.1.3": "自动切换。",
                "2.1.4": "并联闭锁。",
                "2.1.5": "无功补偿。",
            },
            claims=[],
            source_ids=[],
            unresolved_questions=["待补一次图"],
            revision=0,
        ),
    )


def test_session_summary_uses_artifact_skeleton_and_agent_rationale_as_context_only(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "Work/evidence.jsonl"
    result_ref = tmp_path / "Work/runs/run-summary/results/module-2.1.json"
    evidence.parent.mkdir(parents=True)
    result_ref.parent.mkdir(parents=True)
    evidence.write_text("{}\n", encoding="utf-8")
    result_ref.write_text(_result().model_dump_json(), encoding="utf-8")
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id="run-summary",
        agent_id="module-2.1-specialist",
        objective="完成 2.1 分析",
        input_refs=["Work/evidence.jsonl"],
    )
    builder = SessionSummaryBuilder(tmp_path)
    skeleton = builder.build_skeleton(
        envelope,
        ["Work/evidence.jsonl"],
        session_id="session-summary",
        message_log=[
            {"role": "user", "content": "任务"},
            {"role": "assistant", "tool_calls": [{"name": "submit_result"}]},
        ],
    )
    summary = builder.finalize(
        skeleton,
        _result(),
        output_refs=["Work/runs/run-summary/results/module-2.1.json"],
        agent_rationale="现有证据只支持保留边界。",
    )
    store = SessionSummaryStore(tmp_path)
    saved_ref = store.save(summary)

    loaded = store.load(saved_ref)
    assert loaded.context_only is True
    assert loaded.agent_rationale == "现有证据只支持保留边界。"
    assert loaded.message_count == 2
    assert loaded.tool_names == ["submit_result"]
    assert store.relevant(run_id="run-summary", agent_id="module-2.1-specialist") == [saved_ref]


def test_session_summary_rejects_missing_artifact_reference(tmp_path: Path) -> None:
    builder = SessionSummaryBuilder(tmp_path)
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id="run-summary",
        agent_id="module-2.1-specialist",
        objective="完成 2.1 分析",
        input_refs=["Work/missing.json"],
    )

    with pytest.raises(FileNotFoundError, match="missing.json"):
        builder.build_skeleton(envelope, [], session_id="session-summary", message_log=[])
