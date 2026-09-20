from manyselves.runtime.semantic_trace import (
    SemanticEventKind,
    SemanticTraceEvent,
    SemanticTraceRecorder,
)


def test_semantic_trace_omits_unset_and_nondeterministic_fields() -> None:
    recorder = SemanticTraceRecorder()
    recorder.record(
        SemanticEventKind.AGENT_INVOKED,
        workflow_id="neutral-workflow",
        action_id="neutral-action",
        agent_id="neutral-agent",
        conversation_key="conversation-a",
        input_contract="input-v1",
        output_contract="output-v1",
    )

    assert recorder.snapshot() == [
        {
            "event": "agent.invoked",
            "workflow_id": "neutral-workflow",
            "action_id": "neutral-action",
            "agent_id": "neutral-agent",
            "conversation_key": "conversation-a",
            "input_contract": "input-v1",
            "output_contract": "output-v1",
        }
    ]
    assert set(SemanticTraceEvent.__dataclass_fields__) == {
        "event",
        "workflow_id",
        "action_id",
        "agent_id",
        "conversation_key",
        "input_contract",
        "output_contract",
        "tool_id",
        "branch",
        "status",
        "output_id",
    }
    assert [event.value for event in SemanticEventKind] == [
        "workflow.started",
        "action.started",
        "conversation.created",
        "agent.invoked",
        "tool.invoked",
        "contract.validated",
        "branch.selected",
        "action.completed",
        "action.failed",
        "workflow.completed",
        "output.published",
    ]
