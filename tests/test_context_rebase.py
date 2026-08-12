from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from manyselves.core.loops.context_rebase import AtomicToolUnit
from manyselves.core.providers.base import LLMToolCall, Message as LLMMessage
from manyselves.core.reporting.context_rebase import ReportingContextRebuilder
from manyselves.core.reporting.context_state import EvidenceSlice


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_atomic_tool_unit_requires_all_results() -> None:
    assistant = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[
            LLMToolCall(id="a", name="read", arguments={}),
            LLMToolCall(id="b", name="inspect", arguments={}),
        ],
    )
    with pytest.raises(ValueError, match="incomplete"):
        AtomicToolUnit(
            assistant,
            (
                LLMMessage(
                    role="tool", content="ok", tool_call_id="a", is_tool_result=True
                ),
            ),
        )


def test_rebuilder_orders_typed_state_and_keeps_unconsumed_unit_atomic() -> None:
    rebaser = ReportingContextRebuilder()
    rebaser.begin_task(
        "run-1",
        "task-1",
        objective="完成任务",
        stable_prefix=[LLMMessage(role="system", content="stable")],
    )
    rebaser.record_tool_call(
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(id="call-1", name="read", arguments={}),
                LLMToolCall(id="call-2", name="inspect", arguments={}),
            ],
        )
    )
    rebaser.record_tool_result("call-1", "one")
    rebaser.record_tool_result("call-2", "two")
    context = rebaser.rebuild(
        [],
        [{"name": "read"}, {"name": "inspect"}, {"name": "write"}],
    )
    assert [message.role for message in context.messages[:2]] == ["system", "user"]
    assistant = next(message for message in context.messages if message.tool_calls)
    assert {call.id for call in assistant.tool_calls} == {"call-1", "call-2"}
    assert {message.tool_call_id for message in context.messages if message.is_tool_result} == {
        "call-1",
        "call-2",
    }
    assert {item["name"] for item in context.tool_definitions} == {"read", "inspect"}


def test_consumed_tool_result_is_reference_hash_only() -> None:
    rebaser = ReportingContextRebuilder()
    rebaser.begin_task("run-1", "task-1")
    memo = rebaser.record_tool_result(
        "call-1",
        "secret prose",
        tool_name="read",
        ref="Work/tool-result.json",
        sha256=_sha("secret prose"),
        consumed=True,
    )
    assert memo.ref == "Work/tool-result.json"
    context = rebaser.rebuild([], [{"name": "read"}])
    assert "secret prose" not in "\n".join(message.content for message in context.messages)
    assert context.manifest.tool_results[0].sha256 == _sha("secret prose")


def test_successful_provider_delivery_rebases_context_to_summary_ref_and_new_units() -> None:
    rebaser = ReportingContextRebuilder()
    full_evidence = "现场开关温升达到 85 摄氏度，建议复核接点电阻。" * 40
    rebaser.begin_task(
        "run-1",
        "task-1",
        evidence=[
            EvidenceSlice(
                ref="Work/runs/run-1/preparation/evidence.jsonl",
                sha256=_sha(full_evidence),
                content=full_evidence,
            )
        ],
    )
    first = rebaser.rebuild([])
    assert full_evidence in "\n".join(message.content for message in first.messages)

    rebaser.mark_provider_context_delivered(first.messages)
    second = rebaser.rebuild([])
    second_text = "\n".join(message.content for message in second.messages)
    assert full_evidence not in second_text
    assert "现场开关温升达到 85 摄氏度" in second_text
    assert "ref=Work/runs/run-1/preparation/evidence.jsonl" in second_text
    assert second.manifest.evidence[0].consumed is True
    assert second.manifest.evidence[0].content is None

    rebaser.record_tool_call(LLMToolCall(id="call-new", name="read", arguments={}))
    rebaser.record_tool_result("call-new", "本轮新增工具结果")
    third = rebaser.rebuild([], [{"name": "read"}])
    assert "本轮新增工具结果" in "\n".join(message.content for message in third.messages)
    rebaser.mark_provider_context_delivered(third.messages)
    fourth = rebaser.rebuild([], [{"name": "read"}])
    assert "本轮新增工具结果" not in "\n".join(
        message.content for message in fourth.messages
    )
    assert fourth.manifest.tool_results[0].summary == "本轮新增工具结果"


def test_first_tool_request_defers_base_context_consumption_until_result_round() -> None:
    rebaser = ReportingContextRebuilder()
    content = "需要工具后续仍可引用的完整证据"
    rebaser.begin_task(
        "run-1",
        "task-1",
        evidence=[
            EvidenceSlice(
                ref="Work/evidence.json",
                sha256=_sha(content),
                content=content,
            )
        ],
    )
    first = rebaser.rebuild([])
    rebaser.mark_provider_context_delivered(
        first.messages,
        response=SimpleNamespace(tool_calls=[object()]),
    )
    assert rebaser.manifest.evidence[0].consumed is False
    assert rebaser.manifest.evidence[0].content == content

    rebaser.record_tool_call(LLMToolCall(id="call-1", name="read", arguments={}))
    rebaser.record_tool_result("call-1", "新增结果")
    followup = rebaser.rebuild([], [{"name": "read"}])
    rebaser.mark_provider_context_delivered(
        followup.messages,
        response=SimpleNamespace(tool_calls=[]),
    )
    assert rebaser.manifest.evidence[0].consumed is True
    assert rebaser.manifest.evidence[0].content is None
