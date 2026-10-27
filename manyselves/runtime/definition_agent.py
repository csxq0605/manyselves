"""Generic Agent invoker driven only by file definitions.

One invoker serves every Agent of one Run.  It turns an ``invoke_agent``
action into one typed turn on a Provider-backed AgentLoop:

- the Agent definition supplies the system prompt; the Task supplies the
  objective, constraints, model-visible Tools and output contract;
- ``submit_result`` validates the result against the output contract and asks
  for a correction in the same conversation when it does not match;
- declared recovery covers natural language without a submission, max-token
  and tool-slice continuations, and no-progress stops;
- a completed result for the same conversation, task and input is reused on a
  Same-run resume, and Capability Tool results are reused per their definition;
- each conversation keeps one live session, its full history is saved after
  every invocation, and a forked conversation starts from its source's history.

Nothing here knows a Capability, a role, or a domain field.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from manyselves.kernel.contracts import ContractAdapter
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    TaskDefinition,
    ToolDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.recovery import RecoveryEventKind

from .agent_execution import (
    AgentExecutionService,
    AgentSessionLoop,
    AgentSessionRestore,
    AgentTurnRequest,
)
from .agent_memo import FileAgentTurnMemo, file_digest, turn_key
from .agent_recovery import AgentRecoveryDriver
from .agent_results import FileAgentResultStore
from .context_budget import estimate_request_tokens, provider_context_window
from .conversation_history import (
    FileConversationHistoryStore,
    drop_rejected_attempts,
    serialize_message,
)
from .provider_agent_session import ProviderAgentSessionFactory
from .tools.definition_tool import DefinitionTool
from .tools.registry import ToolRegistry
from .tools.submit_result import SubmitResultTool
from .typed_agent_turn import TypedAgentTurn
from .typed_recovery import build_tool_recovery_callback, execute_typed_recovery

LoopBuilder = Callable[..., AgentSessionLoop]
ToolAdapterFactory = Callable[[ToolDefinition], Any]

_CORRECTION_TAG = "submission_correction"
_MAX_TOKENS_TAG = "max_tokens_continuation"
_TOOL_SLICE_TAG = "tool_slice_continuation"
_INTERNAL_TAGS = (_CORRECTION_TAG, _MAX_TOKENS_TAG, _TOOL_SLICE_TAG)


def _default_loop_builder(**kwargs: Any) -> AgentSessionLoop:
    from .loops.agent_loop import AgentLoop

    return AgentLoop(**kwargs)


class DefinitionAgentInvoker:
    """Invoke any file-defined Agent task through one Provider-backed session."""

    def __init__(
        self,
        services: Any,
        *,
        workflow_id: str,
        run_id: str,
        definitions: DefinitionRegistry,
        contracts: Mapping[str, ContractAdapter],
        tool_factory: ToolAdapterFactory | None = None,
        execution: AgentExecutionService | None = None,
        loop_builder: LoopBuilder = _default_loop_builder,
    ) -> None:
        if services.workspace is None:
            raise ValueError("the Agent invoker requires a workspace")
        self.services = services
        self.workspace = Path(services.workspace).resolve()
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.definitions = definitions
        self.contracts = contracts
        self.tool_factory = tool_factory
        self.execution = execution or AgentExecutionService(services.bus)
        self.loop_builder = loop_builder
        self.results = FileAgentResultStore(self.workspace, run_id)
        self.histories = FileConversationHistoryStore(self.workspace, run_id)
        self.memo = FileAgentTurnMemo(self.workspace, run_id)

    def _note_budget(
        self,
        task: Any,
        conversation: ConversationRecord,
        action: str,
        estimate: int,
        sent: int,
    ) -> None:
        """Record a turn that went over its task's budget and what was done instead."""

        path = self.workspace / f"Work/runs/{self.run_id}/budget-notes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "task_id": task.id,
                "conversation_id": conversation.conversation_id,
                "action": action,
                "budget": task.attention_budget_tokens,
                "estimate": estimate,
                "sent": sent,
            }, ensure_ascii=False) + "\n")

    async def close(self) -> None:
        await self.execution.close_workflow(self.workflow_id)

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent, task, value, conversation, task_id=task_id, recovery_policy=None
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        input_value = _json_value(value)
        output_contract = self.contracts[task.output_contract]
        recovery = (
            AgentRecoveryDriver(recovery_policy) if recovery_policy is not None else None
        )
        conversation_id = conversation.conversation_id
        store_key = _store_key(task_id, task)
        completed = self.results.completed_for(
            conversation_id=conversation_id,
            task_id=store_key,
            input_value=input_value,
        )
        if completed is not None:
            return await self._reuse(
                completed, output_contract, task, recovery, conversation
            )

        runtime_id = f"{self.workflow_id}:{agent.id}:{conversation_id}"
        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation_id}"
        )
        attempt_id = f"{task_id}@{conversation_id}"
        tools = self._tools(
            agent=agent,
            task=task,
            task_id=task_id,
            attempt_id=attempt_id,
            runtime_id=runtime_id,
            session_id=session_id,
            conversation_id=conversation_id,
            input_value=input_value,
            output_contract=output_contract,
            recovery=recovery,
        )
        factory = ProviderAgentSessionFactory(
            loop_builder=self.loop_builder,
            loop_kwargs={
                "agent_type": runtime_id,
                "workspace": self.workspace,
                "tools": tools,
                "bus": self.services.bus,
                "config": self.services.agent_defaults,
                "llm_provider": self.services.active_provider,
                "system_prompt": agent.instructions,
                "usage_run_id": self.run_id,
                "usage_task_id": f"{task.id}:{conversation.key.value}",
            },
            persist_handoff_summary=False,
        )
        existing = self.execution.session(self.workflow_id, conversation_id)
        restore = None if existing is not None else self._restore(conversation)
        try:
            images = self._images(task, input_value)
        except ValueError as exc:
            return AgentInvocationOutcome(
                status="failed", session_id=conversation.external_session_id, error=str(exc)
            )
        prompt = self._prompt(task, input_value, tools)
        before = _history_of(existing, restore)
        if task.attention_budget_tokens is not None:
            # The budget is where the work changes course, never where it
            # fails or is cut: over it, the turn starts afresh from its own
            # input (every task that continues a conversation carries what it
            # needs, or says it reads its history); still over, it is sent
            # whole with a note.  Only a request the model's window cannot
            # hold fails.
            def estimate_for(history: list[dict[str, Any]]) -> int:
                return estimate_request_tokens(
                    system_prompt=agent.instructions,
                    history=history,
                    prompt=prompt,
                    tool_schemas=tools.get_definitions(),
                    images=len(images),
                )

            estimate = estimate_for(before)
            if estimate > task.attention_budget_tokens and before and not task.needs_history:
                fresh = estimate_for([])
                if fresh < estimate:
                    self._note_budget(
                        task, conversation, "fresh_conversation", estimate, fresh
                    )
                    if existing is not None:
                        replace = getattr(existing.loop, "replace_conversation", None)
                        if callable(replace):
                            replace([])
                    restore = None
                    before = []
                    estimate = fresh
            if estimate > task.attention_budget_tokens:
                window = provider_context_window(
                    self.services.agent_defaults, self.services.active_provider
                )
                if estimate > window:
                    return AgentInvocationOutcome(
                        status="failed",
                        session_id=conversation.external_session_id,
                        error=(
                            f"{ATTENTION_BUDGET_EXCEEDED}: task {task.id} would send about "
                            f"{estimate} tokens, above the model's window of {window}"
                        ),
                    )
                self._note_budget(task, conversation, "sent_over_budget", estimate, estimate)
        memo_key = turn_key(
            system_prompt=agent.instructions,
            prompt=prompt,
            tool_schemas=tools.get_definitions(),
            output_schema=output_contract.json_schema(),
            image_digests=[file_digest(image["path"]) for image in images],
            history=before,
        )
        replayed = await self._replay(
            memo_key,
            before=before,
            output_contract=output_contract,
            conversation=conversation,
            agent=agent,
            task=task,
            store_key=store_key,
            input_value=input_value,
            live=existing is not None,
        )
        if replayed is not None:
            return replayed
        if existing is not None:
            factory.reconfigure(existing.loop)
        turn = TypedAgentTurn(
            execution=self.execution,
            workflow_id=self.workflow_id,
            conversation_key=conversation_id,
            runtime_id=runtime_id,
            session_id=session_id,
            session_factory=factory,
        )
        try:
            session = await turn.start_or_restore(restore=restore)
        except Exception as exc:
            return AgentInvocationOutcome(
                status="failed",
                session_id=conversation.external_session_id,
                error=str(exc),
            )
        conversation.external_session_id = session.session_id
        begin_typed_task = getattr(session.loop, "begin_typed_task", None)
        if callable(begin_typed_task):
            begin_typed_task(
                {
                    "task_id": f"{task_id}:{task.id}",
                    "run_id": self.run_id,
                    "input_contract": task.input_contract,
                    "output_contract": task.output_contract,
                }
            )
        request = AgentTurnRequest(
            content=prompt,
            images=images,
            message_id=f"{attempt_id}:initial",
            workflow_id=self.workflow_id,
            run_id=self.run_id,
            task_id=task_id,
            task_attempt_id=attempt_id,
            turn_kind="task_initial",
        )
        terminal = turn.result_terminal(
            run_id=self.run_id,
            task_id=task_id,
            task_attempt_id=attempt_id,
            session_id=session.session_id,
        )

        def decode(result_ref: str) -> Any:
            return self._decode(result_ref, output_contract)

        try:
            if recovery_policy is None:
                outcome = await turn.dispatch(session, request, terminals=(terminal,))
                mapped = TypedAgentTurn.map_outcome(
                    outcome, session_id=session.session_id, decode_result=decode
                )
            else:
                recovered = await execute_typed_recovery(
                    self.execution,
                    session,
                    request,
                    recovery_policy=recovery_policy,
                    terminals=(terminal,),
                    prompt_builder=lambda event: self._recovery_prompt(
                        event, agent, task
                    ),
                    result_decoder=decode,
                    recovery=recovery,
                    progress_observer=_ProgressObserver(session.loop),
                )
                mapped = (
                    recovered
                    if isinstance(recovered, AgentInvocationOutcome)
                    else AgentInvocationOutcome(
                        status="ok", result=recovered, session_id=session.session_id
                    )
                )
            if mapped.status == "ok":
                self._settle(session.loop, len(before))
        finally:
            self._save_history(conversation_id, session.loop)
        if mapped.status == "ok" and not task.tools:
            self._remember(
                memo_key,
                result=mapped.result,
                before=before,
                loop=session.loop,
                detail={
                    "agent_id": agent.id,
                    "task_id": task.id,
                    "store_key": store_key,
                    "conversation_id": conversation_id,
                    "model": self._model_identity(),
                },
            )
        if mapped.status != "ok":
            reason = self._failure_reason(conversation_id, store_key)
            if reason:
                mapped = mapped.model_copy(update={"error": reason})
        return mapped

    def _model_identity(self) -> str:
        """Which provider configuration and model answer this Run's turns now.

        Two answers count as the same model only when the Provider settings
        entry, service address, model and generation parameters all match.
        The readable part names the class and model; the hash covers the rest
        so the service address is not written into Run records in plain text.
        """

        provider = self.services.active_provider
        defaults = self.services.agent_defaults
        model = getattr(provider, "model", "") or ""
        settings = {
            "config_id": getattr(provider, "config_id", None),
            "provider_type": getattr(provider, "provider_type", None),
            "api_base": getattr(provider, "api_base", None),
            "model": model,
            "temperature": getattr(defaults, "temperature", None),
            "max_tokens": getattr(defaults, "max_tokens", None),
            "max_output_tokens": getattr(provider, "max_output_tokens", None),
        }
        digest = hashlib.sha256(
            json.dumps(settings, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return f"{type(provider).__name__}:{model}@{digest}"

    async def _replay(
        self,
        memo_key: str,
        *,
        before: list[dict[str, Any]],
        output_contract: ContractAdapter,
        conversation: ConversationRecord,
        agent: AgentDefinition,
        task: TaskDefinition,
        store_key: str,
        input_value: Any,
        live: bool,
    ) -> AgentInvocationOutcome | None:
        """Replay a turn the model already completed for exactly this context."""

        if task.tools:
            # Turns that call Capability Tools have side effects; replaying
            # the reply alone would skip them.
            return None
        record = self.memo.load(memo_key)
        if record is None:
            return None
        if self.memo.current_model_only() and record.get("model") != self._model_identity():
            return None
        try:
            result = _json_value(output_contract.validate(record.get("result")))
        except Exception:  # noqa: BLE001 - a stale record is simply not reused
            return None
        conversation_id = conversation.conversation_id
        self.histories.save(conversation_id, self.memo.history_after(record, before))
        if live:
            # The next turn restores from the saved history, so the live
            # session must not keep the pre-replay conversation.
            await self.execution.close_session(self.workflow_id, conversation_id)
        self.results.save(
            conversation_id=conversation_id,
            task_id=store_key,
            agent_id=agent.id,
            input_value=input_value,
            status="completed",
            result=result,
        )
        self.memo.log_hit(
            {
                "key": memo_key,
                "conversation_id": conversation_id,
                "task_id": task.id,
                "store_key": store_key,
            }
        )
        return AgentInvocationOutcome(
            status="ok", result=result, session_id=conversation.external_session_id
        )

    def _remember(
        self,
        memo_key: str,
        *,
        result: Any,
        before: list[dict[str, Any]],
        loop: AgentSessionLoop,
        detail: Mapping[str, Any],
    ) -> None:
        messages = getattr(loop, "conversation_messages", None)
        if not callable(messages):
            return
        self.memo.save(
            memo_key,
            result=_json_value(result),
            before=before,
            after=messages(),
            detail=detail,
        )

    async def _reuse(
        self,
        completed: Mapping[str, Any],
        output_contract: ContractAdapter,
        task: TaskDefinition,
        recovery: AgentRecoveryDriver | None,
        conversation: ConversationRecord,
    ) -> AgentInvocationOutcome:
        result = _json_value(output_contract.validate(completed["result"]))

        async def reuse(_directive: Any) -> AgentInvocationOutcome:
            return AgentInvocationOutcome(
                status="ok",
                result=result,
                session_id=conversation.external_session_id,
            )

        async def stop(directive: Any) -> AgentInvocationOutcome:
            return AgentInvocationOutcome(
                status="incomplete",
                session_id=conversation.external_session_id,
                error=getattr(directive, "reason", None)
                or "completed result recovery stopped",
            )

        if recovery is None:
            return await reuse(None)
        return await self.execution.recover_completed_result(
            recovery=recovery,
            detail={"task_id": task.id, "source": "persisted_result"},
            reuse_result=reuse,
            stop=stop,
        )

    def _tools(
        self,
        *,
        agent: AgentDefinition,
        task: TaskDefinition,
        task_id: str,
        attempt_id: str,
        runtime_id: str,
        session_id: str,
        conversation_id: str,
        input_value: Any,
        output_contract: ContractAdapter,
        recovery: AgentRecoveryDriver | None,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            SubmitResultTool(
                sender=runtime_id,
                workflow_id=self.workflow_id,
                run_id=self.run_id,
                task_id=task_id,
                task_attempt_id=attempt_id,
                session_id=session_id,
                conversation_id=conversation_id,
                agent_id=agent.id,
                input_value=input_value,
                output_contract_id=task.output_contract,
                output_contract=output_contract,
                generic=_generic(agent, task),
                store_task_id=_store_key(task_id, task),
                store=self.results,
                bus=self.services.bus,
                recovery_callback=(
                    build_tool_recovery_callback(recovery) if recovery is not None else None
                ),
            )
        )
        for tool_id in task.tools:
            definition = self.definitions.require(DefinitionKind.TOOL, tool_id)
            if not isinstance(definition, ToolDefinition):
                raise TypeError(f"definition is not a tool: {tool_id}")
            if self.tool_factory is None:
                raise ValueError(f"no tool implementations are bound for {tool_id}")
            registry.register(
                DefinitionTool(
                    definition,
                    self.tool_factory(definition),
                    task_id=f"{task_id}@{conversation_id}",
                )
            )
        return registry

    def _restore(self, conversation: ConversationRecord) -> AgentSessionRestore | None:
        own = self.histories.load(conversation.conversation_id)
        if own is not None:
            return AgentSessionRestore(messages=own)
        source_id = conversation.forked_from
        if source_id is None:
            return None
        source = self.execution.session(self.workflow_id, source_id)
        messages = getattr(source.loop, "conversation_messages", None) if source else None
        if callable(messages):
            return AgentSessionRestore(messages=messages())
        persisted = self.histories.load(source_id)
        if persisted is not None:
            return AgentSessionRestore(messages=persisted)
        return None

    @staticmethod
    def _settle(loop: AgentSessionLoop, start: int) -> None:
        """Keep only the accepted answer of the turn that just finished."""

        messages = getattr(loop, "conversation_messages", None)
        replace = getattr(loop, "replace_conversation", None)
        if not callable(messages) or not callable(replace):
            return
        current = messages()
        settled = drop_rejected_attempts(current, start)
        if len(settled) != len(current):
            replace(settled)

    def _save_history(self, conversation_id: str, loop: AgentSessionLoop) -> None:
        messages = getattr(loop, "conversation_messages", None)
        if callable(messages):
            self.histories.save(conversation_id, messages())

    def _decode(self, result_ref: str, output_contract: ContractAdapter) -> Any:
        payload = self.results.load(result_ref)
        if payload.get("status") != "completed":
            raise ValueError(payload.get("reason") or "Agent result is not completed")
        return _json_value(output_contract.validate(payload.get("result")))

    def _failure_reason(self, conversation_id: str, task_id: str) -> str | None:
        path = self.results.path(conversation_id, task_id)
        if not path.is_file():
            return None
        try:
            payload = self.results.load(str(path))
        except (OSError, ValueError):
            return None
        if payload.get("status") == "completed":
            return None
        return payload.get("reason")

    def _images(self, task: TaskDefinition, input_value: Any) -> tuple[dict[str, str], ...]:
        if task.image is None:
            return ()
        if not isinstance(input_value, Mapping):
            raise ValueError(f"task {task.id} image input requires an object input")
        spec = task.image
        if spec.list_field is None:
            entries: list[Any] = [input_value]
        else:
            listed = input_value.get(spec.list_field) or []
            if not isinstance(listed, list):
                raise ValueError(f"task {task.id} input field {spec.list_field} is not a list")
            entries = listed
        images: list[dict[str, str]] = []
        for entry in entries:
            relative = entry.get(spec.path_field) if isinstance(entry, Mapping) else None
            media_type = entry.get(spec.media_type_field) if isinstance(entry, Mapping) else None
            if not isinstance(relative, str) or not isinstance(media_type, str):
                raise ValueError(f"task {task.id} input has no image path and media type")
            path = (self.workspace / relative).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_file():
                raise ValueError(f"task {task.id} image is not a workspace file: {relative}")
            images.append({"path": str(path), "media_type": media_type})
        return tuple(images)

    @staticmethod
    def _prompt(task: TaskDefinition, input_value: Any, tools: ToolRegistry) -> str:
        remaining = input_value
        blocks: dict[str, str] = {}
        if task.context_fields and isinstance(input_value, Mapping):
            remaining = dict(input_value)
            for field in task.context_fields:
                if field not in remaining:
                    continue
                value = remaining.pop(field)
                text = value if isinstance(value, str) else _compact_json(value)
                blocks[field] = f"<context name=\"{field}\">\n{text.strip()}\n</context>"
        static: list[str] = [f"<task id=\"{task.id}\">\n{task.objective.strip()}\n</task>"]
        if task.constraints:
            static.append(
                "<constraints>\n"
                + "\n".join(f"- {item}" for item in task.constraints)
                + "\n</constraints>"
            )
        other_tools = [name for name in tools.get_all() if name != "submit_result"]
        if other_tools:
            static.append("可用工具：" + "、".join(other_tools) + "。")
        submit = tools.get("submit_result")
        generic = bool(getattr(submit, "generic", False))
        schema: list[str] = []
        if generic:
            # The generic tool declares only "an object"; the task's own schema
            # travels in the message.
            schema.append(
                f"<output_schema contract=\"{task.output_contract}\">\n"
                + json.dumps(
                    submit.output_contract.json_schema(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n</output_schema>"
            )
        body = f"<input contract=\"{task.input_contract}\">\n" + _compact_json(remaining) + "\n</input>"
        hint = _submit_hint(task, generic) + "资料不足时如实写明，不要编造。"
        if task.static_first:
            # What every request of the batch shares comes first, what varies
            # last, so a request reuses the prefix an earlier one sent.
            shared = [blocks.pop(f) for f in task.shared_fields if f in blocks]
            parts = [*shared, *static, *schema, *blocks.values(), body, hint]
        else:
            parts = [*blocks.values(), *static, body, *schema, hint]
        return "\n\n".join(parts)

    @staticmethod
    def _recovery_prompt(
        event: RecoveryEventKind,
        agent: AgentDefinition,
        task: TaskDefinition,
    ) -> str:
        if event is RecoveryEventKind.MAX_TOKENS:
            tag = _MAX_TOKENS_TAG
            body = (
                "上一轮输出达到单次输出上限，任务尚未完成。继续使用当前会话，"
                "不要从头重复已经完成的内容。"
            )
        elif event is RecoveryEventKind.TOOL_SLICE_BOUNDARY:
            tag = _TOOL_SLICE_TAG
            body = (
                "上一轮达到工具执行片段边界，已完成的工具结果都已保留。"
                "只继续尚未完成的部分，不要重跑已完成的工具。"
            )
        else:
            tag = _CORRECTION_TAG
            body = "上一轮只返回了自然语言，没有提交结构化结果。不要重复已经完成的分析。"
        return (
            f"<{tag}>\n{body}\n你仍是 {agent.id}，当前任务是 {task.id}。"
            f"完成后立即{_submit_hint(task, _generic(agent, task))}\n</{tag}>"
        )


def _compact_json(value: Any) -> str:
    """JSON without layout whitespace: the model reads it as well, for fewer tokens."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _store_key(task_id: str, task: TaskDefinition) -> str:
    """Result-store key: the action id, plus the task id when they differ.

    One conversation may run the same action id for different tasks (for
    example two workflows that both name their step ``revise``); keeping the
    task id in the key stops one result file from replacing the other.
    """

    return task_id if task_id == task.id else f"{task_id}--{task.id}"


def _submit_hint(task: TaskDefinition, generic: bool) -> str:
    if generic:
        return (
            "完成后调用 submit_result，参数直接按本任务消息里的 output_schema 填写，"
            "不要包一层。"
        )
    return f"完成后调用 submit_result 提交 {task.output_contract}；参数直接按 schema 填写。"


def _generic(agent: AgentDefinition, task: TaskDefinition) -> bool:
    """An Agent with several contracts gets the generic submit tool."""

    return len(set(agent.produces)) > 1 and task.output_contract in agent.produces


class _ProgressObserver:
    """Report progress when the conversation gained new model or tool events."""

    def __init__(self, loop: AgentSessionLoop) -> None:
        self._seen = _event_digests(loop)

    async def __call__(
        self,
        loop: AgentSessionLoop,
        _event_kind: RecoveryEventKind,
        _detail: dict[str, Any],
    ) -> Literal["progressed", "no_progress"]:
        current = _event_digests(loop)
        progressed = bool(current - self._seen)
        self._seen |= current
        return "progressed" if progressed else "no_progress"


def _event_digests(loop: AgentSessionLoop) -> set[str]:
    messages = getattr(loop, "conversation_messages", None)
    digests: set[str] = set()
    for message in messages() if callable(messages) else ():
        content = str(getattr(message, "content", "") or "")
        if getattr(message, "role", "") == "user" and any(
            f"<{tag}>" in content for tag in _INTERNAL_TAGS
        ):
            continue
        calls = [
            {"name": call.name, "arguments": call.arguments}
            for call in getattr(message, "tool_calls", None) or ()
        ]
        if not content.strip() and not calls:
            continue
        event = json.dumps(
            {"role": getattr(message, "role", ""), "content": content, "calls": calls},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digests.add(event)
    return digests


ATTENTION_BUDGET_EXCEEDED = "attention budget exceeded"


def _history_of(
    existing: Any, restore: AgentSessionRestore | None
) -> list[dict[str, Any]]:
    """The conversation the next turn starts from, as JSON messages."""

    if existing is not None:
        messages = getattr(existing.loop, "conversation_messages", None)
        if callable(messages):
            return [serialize_message(message) for message in messages()]
        return []
    if restore is None:
        return []
    return [serialize_message(message) for message in restore.messages]


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


__all__ = ["ATTENTION_BUDGET_EXCEEDED", "DefinitionAgentInvoker"]
