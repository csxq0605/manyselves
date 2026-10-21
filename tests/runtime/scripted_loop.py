"""A scripted stand-in for a Provider-backed AgentLoop.

Each user turn is answered by the next step of ``script`` (a list, or a
callable receiving the UserMessage):

- ``("submit", [args, ...])`` calls ``submit_result`` until one is accepted
  (wrapped under the contract name when the tool is keyed);
- ``("tool_then_submit", tool_args, submit_args)`` calls ``lookup`` first;
- ``("say", text)``, ``("max_tokens", text)``, ``("tool_slice", text)`` end the
  turn with that response; a non-empty text is added to the history.
"""

from __future__ import annotations

from typing import Any

from manyselves.interfaces.types import AgentResponse, UserMessage
from manyselves.runtime.loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AGENT_TURN_CONTINUATION_REQUIRED,
)
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMToolCall
from manyselves.runtime.providers.base import Message as LLMMessage


class ScriptedLoop:
    """Answer each user turn with the next scripted step."""

    def __init__(self, bus: MessageBus, script: Any, kwargs: dict[str, Any]) -> None:
        self.bus = bus
        self.script = script
        self.kwargs = kwargs
        self.tools = kwargs["tools"]
        self.history: list[LLMMessage] = []
        self.restored: list[list[Any]] = []
        self.received: list[UserMessage] = []
        self.feedback: list[Any] = []
        self._callback = None
        self.initial_session_restore = None
        self.persist_handoff_summary = False

    def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
        del task_boundaries, handoff_summary
        restored = [
            message
            if isinstance(message, LLMMessage)
            else LLMMessage(role=message["role"], content=message["content"])
            for message in messages
        ]
        self.restored.append(restored)
        self.history = list(restored)

    def conversation_messages(self) -> list[LLMMessage]:
        return list(self.history)

    def begin_typed_task(self, context: dict[str, Any]) -> bool:
        return True

    async def start(self) -> None:
        async def respond(message: UserMessage) -> None:
            if message.agent_type != self.kwargs["agent_type"]:
                return
            self.received.append(message)
            self.history.append(LLMMessage(role="user", content=message.content))
            step = (
                self.script(message) if callable(self.script) else self.script.pop(0)
            )
            kind = step[0]
            if kind == "submit":
                submit = self.tools.get("submit_result")
                for arguments in step[1]:
                    if getattr(submit, "keyed", False) and set(arguments) != {
                        submit.output_contract_id
                    }:
                        # A keyed tool takes the result under its contract name.
                        arguments = {submit.output_contract_id: arguments}
                    self.history.append(
                        LLMMessage(
                            role="assistant",
                            content="",
                            tool_calls=[LLMToolCall("c", "submit_result", arguments)],
                        )
                    )
                    feedback = await self.tools.get("submit_result")(**arguments)
                    self.feedback.append(feedback)
                    if feedback.get("status") != "correction_required":
                        return
                return
            if kind == "tool_then_submit":
                result = await self.tools.get("lookup")(**step[1])
                self.feedback.append(result)
                await self.tools.get("submit_result")(**step[2])
                return
            if kind in {"say", "max_tokens", "tool_slice"}:
                if len(step) > 1 and step[1]:
                    self.history.append(LLMMessage(role="assistant", content=step[1]))
                content = {
                    "say": step[1] if len(step) > 1 else "",
                    "max_tokens": AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
                    "tool_slice": AGENT_TURN_CONTINUATION_REQUIRED,
                }[kind]
                await self.bus.publish(
                    AgentResponse(
                        agent_type=self.kwargs["agent_type"],
                        content=content,
                        message_id=message.message_id,
                        workflow_id=message.workflow_id,
                        run_id=message.run_id,
                        task_id=message.task_id,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )
                return
            raise AssertionError(f"unknown step {kind}")

        self._callback = respond
        self.bus.subscribe(UserMessage, respond)

    async def stop(self) -> None:
        if self._callback is not None:
            self.bus.unsubscribe(UserMessage, self._callback)

    async def wait_until_turn_complete(self) -> None:
        return None
