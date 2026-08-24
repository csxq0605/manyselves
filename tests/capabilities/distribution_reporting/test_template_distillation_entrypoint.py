"""Characterization for the file-defined template Skill distillation entrypoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
    TEMPLATE_SKILL_EXCLUSION_CATEGORIES,
    TEMPLATE_SKILL_TRANSFER_CATEGORIES,
    TemplateSkillBoundaryManifest,
    TemplateSkillSubmission,
)
from manyselves.kernel.definitions import DefinitionKind


def test_distill_template_skill_compiles_with_typed_agent_contracts() -> None:
    """The file definition now resolves through the generic compiler."""

    from manyselves.kernel.executors import build_builtin_executor_registry
    from manyselves.kernel.workflow import WorkflowCompiler

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "distill-template-skill")

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)

    assert plan.workflow_id == "distill-template-skill"
    assert plan.tool_ids == [
        "project-public-template-distillation-input",
        "prepare-template-distillation",
        "materialize-template-skill",
    ]


@pytest.mark.asyncio
async def test_distill_template_skill_host_entrypoint_materializes_typed_agent_output(
    tmp_path: Path,
) -> None:
    """The file workflow must execute through the Generic Host once bound."""

    from manyselves.kernel.ports import AgentInvocationOutcome

    class RecordingAgentInvoker:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def invoke(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
        ) -> AgentInvocationOutcome:
            self.calls.append(
                {
                    "agent": agent.id,
                    "task": task.id,
                    "value": value,
                    "conversation_id": conversation.conversation_id,
                    "session_key": conversation.key.value,
                    "task_id": task_id,
                }
            )
            return AgentInvocationOutcome(
                status="ok",
                result=_submission().model_dump(mode="json"),
                session_id="template-distiller-session",
            )

        async def invoke_with_recovery(
            self,
            agent,
            task,
            value,
            conversation,
            *,
            task_id: str,
            recovery_policy,
        ) -> AgentInvocationOutcome:
            del recovery_policy
            return await self.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )

    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TemplateDistillationWorkflowRuntime,
    )

    template = tmp_path / "Templates/report_template.docx"
    template.parent.mkdir(parents=True)
    template.write_bytes(b"template source")
    command_id = UUID("60000000-0000-4000-8000-000000000001")
    run_id = f"distill-template-skill-{command_id.hex}"
    request = {"template_ref": "Templates/report_template.docx"}
    invoker = RecordingAgentInvoker()

    runtime = TemplateDistillationWorkflowRuntime(
        tmp_path,
        agent_invoker=invoker,
    )
    result = await runtime.start(
        command_id,
        "distill-template-skill",
        request,
    )

    assert result["run_id"] == run_id
    assert len(invoker.calls) == 1
    call = invoker.calls[0]
    assert call["agent"] == "template-distiller"
    assert call["task"] == "template-skill-distillation"
    assert call["session_key"] == "template-distillation"
    assert call["value"].template_ref == (
        f"Work/runs/{run_id}/templates/template-for-skill.docx"
    )
    assert (
        tmp_path / f"Work/runs/{run_id}/context/template-distillation-input.json"
    ).is_file()
    assert (
        tmp_path / f"Work/runs/{run_id}/templates/template-for-skill.docx"
    ).is_symlink()
    run = runtime.get_run(result["run_id"])
    assert run["run"]["status"] == "completed"
    outputs = runtime.get_outputs(result["run_id"])
    materialization = next(
        item["value"] for item in outputs["outputs"] if item["id"] == "result"
    )
    assert materialization["kind"] == "template_skill_materialization"
    assert len(materialization["skill_refs"]) == len(TEMPLATE_ROLE_SKILL_IDS)
    assert all((tmp_path / ref).is_file() for ref in materialization["skill_refs"])


@pytest.mark.asyncio
async def test_distill_template_skill_host_composes_capability_agent_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standalone file Workflow must use the Capability bridge end to end."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TemplateDistillationWorkflowRuntime,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.reporting.agent_runner import ReportingAgentRunner
    from manyselves.core.reporting.workflow import ReportWorkflowRunner
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.runtime.agent_execution import AgentExecutionService

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy reporting runner must not be called")

    monkeypatch.setattr(ReportingAgentRunner, "run", forbidden)
    monkeypatch.setattr(ReportWorkflowRunner, "run", forbidden)

    command_id = UUID("60000000-0000-4000-8000-000000000002")
    run_id = f"distill-template-skill-{command_id.hex}"
    result_ref = f"Work/runs/{run_id}/results/template-skill.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_submission().model_dump(mode="json")),
        encoding="utf-8",
    )
    template = tmp_path / "Templates/report_template.docx"
    template.parent.mkdir(parents=True)
    template.write_bytes(b"template source")
    request = {"template_ref": "Templates/report_template.docx"}

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())

    class ScriptedLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    loops: list[ScriptedLoop] = []

    def session_factory(runtime_id: str) -> ScriptedLoop:
        loop = ScriptedLoop(runtime_id)
        loops.append(loop)
        return loop

    service = AgentExecutionService(bus, timeout=1)
    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
    )
    runtime = TemplateDistillationWorkflowRuntime(
        tmp_path,
        agent_invoker=bridge,
    )
    try:
        result = await runtime.start(
            command_id,
            "distill-template-skill",
            request,
        )

        assert result["run_id"] == run_id
        assert len(loops) == 1
        assert [message.turn_kind for message in loops[0].received] == [
            "task_initial",
        ]
        run = runtime.get_run(run_id)
        assert run["run"]["status"] == "completed"
        outputs = runtime.get_outputs(run_id)
        materialization = next(
            item["value"] for item in outputs["outputs"] if item["id"] == "result"
        )
        assert materialization["kind"] == "template_skill_materialization"
        assert len(materialization["skill_refs"]) == len(TEMPLATE_ROLE_SKILL_IDS)
        assert all((tmp_path / ref).is_file() for ref in materialization["skill_refs"])
    finally:
        await service.close_workflow("distill-template-skill")
        bus.shutdown()
        await bus_task


def test_template_provider_tool_builder_owns_the_exact_agent_tool_set(
    tmp_path: Path,
) -> None:
    """The Provider-facing Template tools are assembled by the Capability."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.storage import (
        ReportingStore,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TEMPLATE_DISTILLATION_ALLOWED_TOOLS,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_tools import (
        build_template_distillation_provider_tools,
    )
    from manyselves.core.loops.bus import MessageBus

    run_id = "template-provider-tools"
    template_input = TemplateDistillationInput(
        run_id=run_id,
        template_ref=f"Work/runs/{run_id}/templates/template-for-skill.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    envelope = TaskEnvelope(
        task_id="template-skill-distillation",
        run_id=run_id,
        agent_id="template-distiller",
        objective="distill one template",
        allowed_outputs=["template_skill_submission"],
        allowed_tools=list(TEMPLATE_DISTILLATION_ALLOWED_TOOLS),
        input_refs=[
            f"Work/runs/{run_id}/context/template-distillation-input.json",
            template_input.template_ref,
        ],
        input_contract_kind="template_distillation_input",
        input_contract_ref=(
            f"Work/runs/{run_id}/context/template-distillation-input.json"
        ),
    )

    registry = build_template_distillation_provider_tools(
        tmp_path,
        envelope=envelope,
        template_input=template_input,
        session_id="template-provider-session",
        workflow_id="distill-template-skill",
        bus=MessageBus(),
        store=ReportingStore(tmp_path),
        tool_names=TEMPLATE_DISTILLATION_ALLOWED_TOOLS,
    )

    assert set(registry.get_all()) == set(TEMPLATE_DISTILLATION_ALLOWED_TOOLS)
    assert registry._schema_cache["submit_result"]["properties"]["kind"]["const"] == (
        "template_skill_submission"
    )
    assert "boundary_manifest" not in registry._schema_cache["submit_result"]["properties"]
    assert registry.get("write_result_part").expected_part_ids == TEMPLATE_ROLE_SKILL_IDS
    assert registry._schema_cache["write_result_part"]["required"] == [
        "part_id",
        "content",
    ]


@pytest.mark.asyncio
async def test_template_provider_shares_declared_recovery_with_submit_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting.runtime import template_provider
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.tools.registry import ToolRegistry
    from manyselves.kernel.conversations import (
        ConversationKey,
        ConversationMode,
        ConversationRecord,
    )
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )

    captured: dict[str, object] = {}

    def capture_tools(*args, **kwargs):
        del args
        captured.update(kwargs)
        return ToolRegistry()

    monkeypatch.setattr(
        template_provider,
        "build_template_distillation_provider_tools",
        capture_tools,
    )
    bus = MessageBus()
    runtime = template_provider.TemplateDistillationProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        )
    )
    value = TemplateDistillationInput(
        run_id="template-schema-run",
        template_ref="Inputs/template.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="Template distiller",
        instructions="Distill the template.",
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="Template distillation",
        agent=agent.id,
        objective="Distill one template.",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
        tools=["submit_result"],
    )
    policy = RecoveryPolicyDefinition(
        id="template-schema-recovery",
        version="1.0.0",
        description="correct invalid structured output",
        rules={
            "invalid_structured_output": RecoveryRule(action="correct"),
        },
    )
    conversation = ConversationRecord(
        conversation_id="template-schema-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode=ConversationMode.RUN,
        ),
        run_id=value.run_id,
    )

    bridge = runtime._bridge(
        agent,
        task,
        value,
        conversation,
        task_id="invoke-template-distiller",
        recovery_policy=policy,
    )
    decision = await captured["recovery_event_callback"](
        "invalid_structured_output",
        {"task_id": task.id},
    )

    assert decision.action.value == "correct"
    assert bridge.recovery_driver.snapshot_attempts() == {
        "invalid_structured_output": 1,
    }
    assert bridge.progress_observer is not None


@pytest.mark.asyncio
async def test_template_provider_runtime_composes_loop_and_reuses_same_session(
    tmp_path: Path,
) -> None:
    """Runtime composition must inject the account Provider resources once."""

    from manyselves.application.runtime_services import RuntimeServicesView
    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_provider import (
        TemplateDistillationProviderRuntime,
    )
    from manyselves.config.schema import AgentDefaults
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.runtime.agent_execution import AgentExecutionService

    run_id = "template-provider-runtime"
    template_ref = f"Work/runs/{run_id}/templates/template-for-skill.docx"
    input_ref = f"Work/runs/{run_id}/context/template-distillation-input.json"
    template_path = tmp_path / template_ref
    template_path.parent.mkdir(parents=True)
    template_path.write_bytes(b"template source")
    template_input = TemplateDistillationInput(
        run_id=run_id,
        template_ref=template_ref,
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    input_path = tmp_path / input_ref
    input_path.parent.mkdir(parents=True)
    input_path.write_text(template_input.model_dump_json(), encoding="utf-8")
    result_ref = f"Work/runs/{run_id}/results/template-skill.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_submission().model_dump(mode="json")),
        encoding="utf-8",
    )

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "template-distiller")
    task = registry.require(DefinitionKind.TASK, "template-skill-distillation")

    class ActiveProvider:
        provider_type = "scripted"
        model = "scripted-model"

    provider = ActiveProvider()
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    built_kwargs: list[dict[str, object]] = []
    loops: list[object] = []

    class ScriptedLoop:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.runtime_id = str(kwargs["agent_type"])
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def loop_builder(**kwargs):
        built_kwargs.append(kwargs)
        loop = ScriptedLoop(kwargs)
        loops.append(loop)
        return loop

    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=bus,
        active_provider=provider,
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    runtime = TemplateDistillationProviderRuntime(
        services,
        execution=service,
        loop_builder=loop_builder,
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=run_id,
    )
    try:
        first = await runtime.invoke(
            agent,
            task,
            template_input,
            conversation,
            task_id="invoke-template-distiller",
        )
        second = await runtime.invoke(
            agent,
            task,
            template_input,
            conversation,
            task_id="invoke-template-distiller",
        )

        assert first.status == "ok"
        assert second.status == "ok"
        assert len(built_kwargs) == 1
        assert built_kwargs[0]["bus"] is bus
        assert built_kwargs[0]["llm_provider"] is provider
        assert built_kwargs[0]["config"] is services.agent_defaults
        assert built_kwargs[0]["system_prompt"] == agent.instructions
        assert set(built_kwargs[0]["tools"].get_all()) == {
            "inspect_document",
            "write_result_part",
            "list_result_parts",
            "submit_result",
            "report_blocked",
        }
        assert built_kwargs[0]["tools"].get("submit_result").task_id == (
            "invoke-template-distiller"
        )
        assert len(loops) == 1
        assert len(loops[0].received) == 2
        assert conversation.external_session_id == first.session_id
        assert first.session_id == second.session_id
        assert len(service.sessions) == 1
    finally:
        await runtime.close()
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_template_distillation_bridge_uses_generic_agent_execution_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Capability bridge must not dispatch through either reporting runner."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.core.reporting.agent_runner import ReportingAgentRunner
    from manyselves.core.reporting.workflow import ReportWorkflowRunner
    from manyselves.interfaces.types import AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import AgentDefinition, TaskDefinition
    from manyselves.runtime.agent_execution import AgentExecutionService

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy reporting runner must not be called")

    monkeypatch.setattr(ReportingAgentRunner, "run", forbidden)
    monkeypatch.setattr(ReportWorkflowRunner, "run", forbidden)

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    result_ref = "Work/runs/run-template-bridge/results/template-skill.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_submission().model_dump(mode="json")),
        encoding="utf-8",
    )

    class ScriptedLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
        tools=["inspect_document", "write_result_part", "list_result_parts", "submit_result"],
    )
    value = TemplateDistillationInput(
        run_id="run-template-bridge",
        template_ref="Work/runs/run-template-bridge/templates/template-for-skill.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=value.run_id,
    )
    service = AgentExecutionService(bus, timeout=1)
    loops: list[ScriptedLoop] = []

    def session_factory(runtime_id: str) -> ScriptedLoop:
        loop = ScriptedLoop(runtime_id)
        loops.append(loop)
        return loop

    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
        workflow_id="distill-template-skill",
    )

    outcome = await bridge.invoke(
        agent,
        task,
        value,
        conversation,
        task_id="invoke-template-distiller",
    )

    assert outcome.status == "ok"
    assert TemplateSkillSubmission.model_validate(outcome.result).kind == (
        "template_skill_submission"
    )
    assert outcome.session_id == conversation.external_session_id

    second = await bridge.invoke(
        agent,
        task,
        value,
        conversation,
        task_id="invoke-template-distiller",
    )
    assert second.status == "ok"
    assert second.session_id == outcome.session_id
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        "task_initial",
    ]
    assert len(service.sessions) == 1

    await service.close_workflow("distill-template-skill")
    bus.shutdown()
    await bus_task


@pytest.mark.asyncio
async def test_template_distillation_bridge_reuses_completed_result_before_session(
    tmp_path: Path,
) -> None:
    """A persisted completed result must bypass session creation and Provider work."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService

    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
    )
    value = TemplateDistillationInput(
        run_id="run-template-reuse",
        template_ref="Work/runs/run-template-reuse/templates/template-for-skill.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=value.run_id,
    )
    service = AgentExecutionService(MessageBus())
    loader_calls: list[str] = []

    def load_completed(_agent, loaded_task, loaded_value, _conversation):
        loader_calls.append(loaded_task.id)
        assert loaded_value.run_id == value.run_id
        return _submission()

    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=lambda _runtime_id: (
            (_ for _ in ()).throw(AssertionError("Provider session must not start"))
        ),
        completed_result_loader=load_completed,
    )

    outcome = await bridge.invoke_with_recovery(
        agent,
        task,
        value,
        conversation,
        task_id="invoke-template-distiller",
        recovery_policy=RecoveryPolicyDefinition(
            id="completed-reuse",
            version="1.0.0",
            description="reuse persisted completed result",
            rules={
                "completed_tool_result": RecoveryRule(action="reuse_result"),
            },
        ),
    )

    assert outcome.status == "ok"
    assert TemplateSkillSubmission.model_validate(outcome.result).kind == (
        "template_skill_submission"
    )
    assert loader_calls == [task.id]
    assert service.sessions == {}
    assert conversation.external_session_id is None


@pytest.mark.asyncio
async def test_template_distillation_bridge_loads_persisted_completed_result(
    tmp_path: Path,
) -> None:
    """The Capability loader must reuse the durable attempt before a session."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
        load_completed_agent_result,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        IdentityLeaseManager,
        TaskAttemptStore,
        TaskCorrelation,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService

    workflow_id = "distill-template-skill"
    run_id = "run-template-persisted-reuse"
    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
    )
    value = TemplateDistillationInput(
        run_id=run_id,
        template_ref=f"Work/runs/{run_id}/templates/template-for-skill.docx",
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=run_id,
    )
    identity_key = agent.id
    session_id = "session-template-persisted"
    lease_manager = IdentityLeaseManager(tmp_path, run_id)
    lease_handle = lease_manager.acquire(workflow_id, identity_key)
    correlation = TaskCorrelation(
        workflow_id=workflow_id,
        run_id=run_id,
        task_id=task.id,
        task_attempt_id="attempt-template-persisted",
        agent_id=agent.id,
        identity_key=identity_key,
        session_id=session_id,
        lease_owner_id=lease_handle.lease.owner_id,
        lease_epoch=lease_handle.lease.lease_epoch,
    )
    persisted = AgentResult(
        task_id=task.id,
        run_id=run_id,
        agent_id=agent.id,
        session_id=session_id,
        status=AgentRunStatus.COMPLETED,
        payload=_submission(),
    )
    try:
        store = TaskAttemptStore(tmp_path, run_id)
        store.activate(correlation)
        store.persist_result(
            correlation,
            persisted.model_dump(mode="json"),
            status="completed",
        )
    finally:
        lease_handle.release()

    def load_completed(_agent, _task, _value, _conversation):
        recovered = load_completed_agent_result(tmp_path, correlation)
        return None if recovered is None else recovered.payload

    service = AgentExecutionService(MessageBus())
    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=lambda _runtime_id: (
            (_ for _ in ()).throw(AssertionError("Provider session must not start"))
        ),
        completed_result_loader=load_completed,
    )
    conversation.external_session_id = session_id

    outcome = await bridge.invoke_with_recovery(
        agent,
        task,
        value,
        conversation,
        task_id=task.id,
        recovery_policy=RecoveryPolicyDefinition(
            id="completed-reuse",
            version="1.0.0",
            description="reuse persisted completed result",
            rules={
                "completed_tool_result": RecoveryRule(action="reuse_result"),
            },
        ),
    )

    assert outcome.status == "ok"
    assert outcome.session_id == session_id
    assert TemplateSkillSubmission.model_validate(outcome.result).kind == (
        "template_skill_submission"
    )
    assert service.sessions == {}


@pytest.mark.asyncio
async def test_template_distillation_bridge_corrects_natural_language_in_same_session(
    tmp_path: Path,
) -> None:
    """A natural-language terminal must become one typed correction turn."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService

    result_ref = "Work/runs/run-template-correction/results/template-skill.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_submission().model_dump(mode="json")),
        encoding="utf-8",
    )

    class ScriptedCorrectionLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                if message.turn_kind == "task_initial":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content="已完成当前分析，但没有提交结构化结果。",
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
    )
    value = TemplateDistillationInput(
        run_id="run-template-correction",
        template_ref=(
            "Work/runs/run-template-correction/templates/template-for-skill.docx"
        ),
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=value.run_id,
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    loops: list[ScriptedCorrectionLoop] = []

    def session_factory(runtime_id: str) -> ScriptedCorrectionLoop:
        loop = ScriptedCorrectionLoop(runtime_id)
        loops.append(loop)
        return loop

    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id="invoke-template-correction",
            recovery_policy=RecoveryPolicyDefinition(
                id="natural-language-correction",
                version="1.0.0",
                description="correct a natural-language terminal",
                rules={
                    "natural_language_without_submission": RecoveryRule(
                        action="correct"
                    ),
                },
            ),
        )
    finally:
        await service.close_workflow("distill-template-skill")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert TemplateSkillSubmission.model_validate(outcome.result).kind == (
        "template_skill_submission"
    )
    assert len(loops) == 1
    assert len(loops[0].received) == 2
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        "submission_correction",
    ]
    assert loops[0].received[1].internal is True
    assert "submission_correction" in loops[0].received[1].content
    assert "submit_result" in loops[0].received[1].content
    assert outcome.session_id == conversation.external_session_id
    assert len(service.sessions) == 0


@pytest.mark.asyncio
async def test_template_distillation_bridge_continues_max_tokens_in_same_session(
    tmp_path: Path,
) -> None:
    """The existing AgentLoop max-token marker must use a continuation turn."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.core.loops.agent_loop import AGENT_MAX_TOKENS_CONTINUATION_REQUIRED
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService

    result_ref = "Work/runs/run-template-max-tokens/results/template-skill.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_submission().model_dump(mode="json")),
        encoding="utf-8",
    )

    class ScriptedMaxTokensLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                if message.turn_kind == "task_initial":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content=AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
                            internal=True,
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                if message.turn_kind != "max_tokens_continuation":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content="错误的恢复类型",
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
    )
    value = TemplateDistillationInput(
        run_id="run-template-max-tokens",
        template_ref=(
            "Work/runs/run-template-max-tokens/templates/template-for-skill.docx"
        ),
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=value.run_id,
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    loops: list[ScriptedMaxTokensLoop] = []

    def session_factory(runtime_id: str) -> ScriptedMaxTokensLoop:
        loop = ScriptedMaxTokensLoop(runtime_id)
        loops.append(loop)
        return loop

    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id="invoke-template-max-tokens",
            recovery_policy=RecoveryPolicyDefinition(
                id="max-tokens-continuation",
                version="1.0.0",
                description="continue an existing AgentLoop max-token boundary",
                rules={
                    "max_tokens": RecoveryRule(action="continue"),
                },
            ),
        )
    finally:
        await service.close_workflow("distill-template-skill")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert TemplateSkillSubmission.model_validate(outcome.result).kind == (
        "template_skill_submission"
    )
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        "max_tokens_continuation",
    ]
    assert loops[0].received[1].internal is True
    assert "max_tokens" in loops[0].received[1].content
    assert "submit_result" in loops[0].received[1].content
    assert outcome.session_id == conversation.external_session_id
    assert len(service.sessions) == 0


@pytest.mark.asyncio
async def test_template_distillation_bridge_continues_tool_slice_in_same_session(
    tmp_path: Path,
) -> None:
    """The existing AgentLoop tool-slice marker must use a continuation turn."""

    from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
        TemplateDistillationAgentBridge,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        TemplateDistillationInput,
    )
    from manyselves.core.loops.agent_loop import AGENT_TURN_CONTINUATION_REQUIRED
    from manyselves.core.loops.bus import MessageBus
    from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        AgentDefinition,
        RecoveryPolicyDefinition,
        RecoveryRule,
        TaskDefinition,
    )
    from manyselves.runtime.agent_execution import AgentExecutionService

    result_ref = "Work/runs/run-template-tool-slice/results/template-skill.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_submission().model_dump(mode="json")),
        encoding="utf-8",
    )

    class ScriptedToolSliceLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                if message.turn_kind == "task_initial":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content=AGENT_TURN_CONTINUATION_REQUIRED,
                            internal=True,
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                if message.turn_kind != "tool_slice_continuation":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content="错误的恢复类型",
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                await bus.publish(
                    AgentResultMessage(
                        sender=self.runtime_id,
                        workflow_id=message.workflow_id,
                        task_id=message.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    agent = AgentDefinition(
        id="template-distiller",
        version="1.0.0",
        description="typed template distiller",
        instructions="distill",
        accepts=["template_distillation_input"],
        produces=["template_skill_submission"],
    )
    task = TaskDefinition(
        id="template-skill-distillation",
        version="1.0.0",
        description="distill task",
        agent=agent.id,
        objective="distill one template",
        input_contract="template_distillation_input",
        output_contract="template_skill_submission",
    )
    value = TemplateDistillationInput(
        run_id="run-template-tool-slice",
        template_ref=(
            "Work/runs/run-template-tool-slice/templates/template-for-skill.docx"
        ),
        inspect_max_chars=100_000,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="template-distillation",
            mode="run",
        ),
        run_id=value.run_id,
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=1)
    loops: list[ScriptedToolSliceLoop] = []

    def session_factory(runtime_id: str) -> ScriptedToolSliceLoop:
        loop = ScriptedToolSliceLoop(runtime_id)
        loops.append(loop)
        return loop

    bridge = TemplateDistillationAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id="invoke-template-tool-slice",
            recovery_policy=RecoveryPolicyDefinition(
                id="tool-slice-continuation",
                version="1.0.0",
                description="continue an existing AgentLoop tool-slice boundary",
                rules={
                    "tool_slice_boundary": RecoveryRule(action="continue"),
                },
            ),
        )
    finally:
        await service.close_workflow("distill-template-skill")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert TemplateSkillSubmission.model_validate(outcome.result).kind == (
        "template_skill_submission"
    )
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        "tool_slice_continuation",
    ]
    assert loops[0].received[1].internal is True
    assert "tool_slice" in loops[0].received[1].content
    assert "已完成" in loops[0].received[1].content
    assert "submit_result" in loops[0].received[1].content
    assert outcome.session_id == conversation.external_session_id
    assert len(service.sessions) == 0


def _submission() -> TemplateSkillSubmission:
    description = (
        "将模板中的证据限定、分析推进、综合表达、图证叙事与质量检查方法"
        "转化为可复用的角色职责指导。"
    )
    skills = {
        skill_id: (
            "---\n"
            f"name: report-template-{skill_id}\n"
            f"description: {description}\n"
            "---\n\n"
            f"# {skill_id} 模板方法\n\n"
            "先界定证据边界，再组织观察、判断、原因、影响和行动；"
            "使用去事实化结构样例检查表达是否可复用，并在提交前复核职责范围。\n"
            * 7
        )
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    }
    return TemplateSkillSubmission(
        skills=skills,
        boundary_manifest=TemplateSkillBoundaryManifest(
            transferred_categories=sorted(TEMPLATE_SKILL_TRANSFER_CATEGORIES),
            excluded_categories=sorted(TEMPLATE_SKILL_EXCLUSION_CATEGORIES),
            boundary_statement=(
                "只迁移可复用的分析、综合、图证和质量检查方法；项目事实、专业知识、"
                "标准阈值、客户身份、风险结论、建议以及证据编号必须来自当前运行时输入；"
                "Skill 不得自行补充任何项目判断，也不替代模块、Knowledge 或 Evidence 来源。"
            ),
        ),
    )


def test_template_distillation_plan_preserves_single_inspection_session_and_slices() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TEMPLATE_DISTILLATION_ALLOWED_TOOLS,
        TEMPLATE_DISTILLATION_SESSION_KEY,
        build_template_distillation_plan,
    )

    plan = build_template_distillation_plan(
        "run-template-entrypoint",
        "Work/runs/run-template-entrypoint/templates/template-for-skill.docx",
    )

    assert plan.task.task_id == "template-skill-distillation"
    assert plan.task.agent_id == "template-distiller"
    assert plan.task.run_id == "run-template-entrypoint"
    assert plan.session_key == TEMPLATE_DISTILLATION_SESSION_KEY == "template-distillation"
    assert plan.inspect_document.path == plan.input.template_ref
    assert plan.inspect_document.max_chars == 100_000
    assert plan.inspect_document.once is True
    assert plan.required_part_ids == TEMPLATE_ROLE_SKILL_IDS
    assert plan.allowed_tools == TEMPLATE_DISTILLATION_ALLOWED_TOOLS == (
        "inspect_document",
        "write_result_part",
        "list_result_parts",
        "submit_result",
        "report_blocked",
    )
    assert plan.continuation_part_ids(TEMPLATE_ROLE_SKILL_IDS[:6]) == (
        *TEMPLATE_ROLE_SKILL_IDS[6:],
    )
    assert plan.continuation_part_ids(TEMPLATE_ROLE_SKILL_IDS) == ()


@pytest.mark.asyncio
async def test_template_distillation_materializes_fourteen_skills_boundary_and_source(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.storage import (
        ReportingStore,
    )
    from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
        TemplateSkillMaterialization,
        build_template_distillation_tool_implementations,
    )
    from manyselves.kernel.contracts import build_contract_catalog
    from manyselves.kernel.definitions import ToolDefinition
    from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory

    store = ReportingStore(tmp_path)
    source_metadata = {
        "source": "packaged",
        "template_ref": "Work/runs/run-template-entrypoint/templates/template-for-skill.docx",
        "inspection_ref": (
            "Work/runs/run-template-entrypoint/context/template-inspection.json"
        ),
    }
    _capability, registry = load_distribution_reporting_capability()
    contracts = build_contract_catalog(registry)
    definition = registry.require(DefinitionKind.TOOL, "materialize-template-skill")
    assert isinstance(definition, ToolDefinition)
    adapter = CapabilityToolAdapterFactory(
        "distribution-reporting",
        build_template_distillation_tool_implementations(
            store,
            source_metadata=source_metadata,
        ),
        contracts,
    ).build(definition)
    outcome = await adapter.invoke(
        _submission(),
        task_id="run-template-entrypoint:distill-template-skill",
    )
    assert outcome.status == "ok"
    result = TemplateSkillMaterialization.model_validate(outcome.result)

    assert result.skill_refs == tuple(
        f"Work/report-template-role-skills/{skill_id}/SKILL.md"
        for skill_id in TEMPLATE_ROLE_SKILL_IDS
    )
    assert result.boundary_ref == "Work/report-template-role-skills/boundary.json"
    assert result.source_ref == "Work/report-template-role-skills/source.json"
    assert all((tmp_path / ref).is_file() for ref in result.skill_refs)

    boundary = json.loads(
        (tmp_path / "Work/report-template-role-skills/boundary.json").read_text(
            encoding="utf-8"
        )
    )
    source = json.loads(
        (tmp_path / "Work/report-template-role-skills/source.json").read_text(
            encoding="utf-8"
        )
    )
    assert boundary == _submission().boundary_manifest.model_dump(mode="json")
    assert source == {
        "source": "packaged",
        "template_ref": (
            "Work/runs/run-template-entrypoint/templates/template-for-skill.docx"
        ),
        "inspection_ref": (
            "Work/runs/run-template-entrypoint/context/template-inspection.json"
        ),
        "producer": "template-distiller",
        "task_id": "template-skill-distillation",
        "skill_root": "Work/report-template-role-skills",
        "boundary_policy_version": 1,
        "boundary_ref": "Work/report-template-role-skills/boundary.json",
    }


def test_distill_template_skill_definition_uses_generic_agent_actions() -> None:
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "distill-template-skill")
    task = registry.require(DefinitionKind.TASK, "template-skill-distillation")

    assert workflow.tasks == ["template-skill-distillation"]
    assert workflow.output_contract == "template_skill_materialization"
    assert [action["kind"] for action in workflow.actions] == [
        "invoke_tool",
        "invoke_tool",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "publish_result",
        "end_workflow",
    ]
    assert workflow.actions[0]["tool"] == "project-public-template-distillation-input"
    assert workflow.actions[1]["tool"] == "prepare-template-distillation"
    assert workflow.actions[1]["output_variable"] == "prepared-template-distillation-input"
    assert workflow.actions[2]["conversation_key"] == "template-distillation"
    assert workflow.actions[4]["tool"] == "materialize-template-skill"
    assert workflow.actions[4]["input_variable"] == "template-distillation-result"
    assert workflow.actions[5]["output"] == "template-skill-materialization"
    assert task.agent == "template-distiller"
    assert task.tools == [
        "inspect_document",
        "write_result_part",
        "list_result_parts",
        "submit_result",
        "report_blocked",
    ]
    assert task.input_contract == "template_distillation_input"
    assert task.output_contract == "template_skill_submission"
    agent = registry.require(DefinitionKind.AGENT, "template-distiller")
    assert agent.accepts == ["output_artifacts", "template_distillation_input"]
    assert agent.produces == ["output_artifacts", "template_skill_submission"]
    assert registry.require(
        DefinitionKind.TOOL,
        "prepare-template-distillation",
    ).model_visible is False
    assert registry.require(
        DefinitionKind.TOOL,
        "materialize-template-skill",
    ).model_visible is False
    assert registry.require(
        DefinitionKind.OUTPUT,
        "template-skill-materialization",
    ).contract == "template_skill_materialization"
