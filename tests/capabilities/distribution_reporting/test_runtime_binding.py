"""Characterization for the public Distribution Reporting runtime binding."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.adapters.runtime import (
    DistributionReportingRuntimeBinding,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
    ModuleProviderRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
    PublicReportingWorkflowRuntime,
)
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.kernel.workflow import ResolvedPlan, WorkflowState, WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.webapi.routes.workflows import _projection


class _ModuleRuntimeSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, object]] = []

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: object,
    ) -> dict:
        self.calls.append((command_id, workflow_id, values))
        return {"run_id": "module-run", "task_id": None}


class _ResumableRuntime:
    workflow_id = "render-existing"

    def __init__(self, workspace: Path) -> None:
        self.store = FileWorkflowStateStore(workspace)
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resume(self, command_id: UUID, run_id: str) -> dict[str, object]:
        del command_id
        self.calls.append(run_id)
        state = self.store.load(run_id)
        self.store.save(state)
        self.started.set()
        await self.release.wait()
        return {"run_id": run_id, "task_id": None}

    def get_run(self, run_id: str) -> dict[str, object]:
        state = self.store.load(run_id)
        return {
            "run": {
                "run_id": run_id,
                "capability_id": "distribution-reporting",
                "workflow_id": state.workflow_id,
                "status": state.status.value,
                "active": state.status in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING},
                "task_id": None,
            },
            "state": state.model_dump(mode="json"),
            "waiting_input": [],
        }


@pytest.mark.asyncio
async def test_module_report_start_does_not_use_legacy_reporting_adapter(
    tmp_path: Path,
) -> None:
    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=MessageBus(),
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    binding = DistributionReportingRuntimeBinding(tmp_path, services)
    public = binding._runtimes["module-report"]
    assert isinstance(public, PublicReportingWorkflowRuntime)
    assert isinstance(
        public.module_runtime.agent_invokers["module-2.4-specialist"],
        ModuleProviderRuntime,
    )

    runtime = _ModuleRuntimeSpy()
    binding._runtimes["module-report"] = runtime
    command_id = UUID("60000000-0000-4000-8000-000000000001")
    values = {
        "operation": "module_report",
        "instruction": "Run the selected module report.",
        "target_modules": ["2.4"],
    }

    result = await binding.start(command_id, "module-report", values)

    assert result == {"run_id": "module-run", "task_id": None}
    assert runtime.calls == [(command_id, "module-report", values)]


def test_web_projection_reuses_the_account_runtime_catalog() -> None:
    projection = object()
    state = SimpleNamespace(workflow_projection=projection)
    request = SimpleNamespace(state=SimpleNamespace(tenant_runtime=state))

    assert _projection(request) is projection
    assert _projection(request) is projection


def test_full_report_binding_composes_the_complete_capability_tail(
    tmp_path: Path,
) -> None:
    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=MessageBus(),
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    binding = DistributionReportingRuntimeBinding(tmp_path, services)
    runtime = binding._runtimes["full-report"]
    request = ReportRequest(
        operation="full_report",
        instruction="Compile the complete report.",
        target_modules=list(REPORT_MODULE_IDS),
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )

    definitions, contracts, plan = runtime.compile_plan(request)
    tools = runtime._tools(definitions, contracts, plan)
    agents = runtime._agent_invokers()

    assert "distribution-cross-owner-local-2.4-module-review-lane" in (
        plan.subworkflow_plans
    )
    assert {
        "prepare-cross-owner-cohort",
        "prepare-chief-chapter-cohort",
        "prepare-final-chapter-cohort",
        "prepare-render-delivery",
        "complete-delivery",
    } <= tools.keys()
    assert {
        "module-2.4-specialist",
        "evidence-auditor",
        "cross-module-reviewer",
        "chief-editor",
        "chief-editor-auditor",
    } <= agents.keys()


def test_reporting_binding_owns_one_agent_execution_service_per_account(
    tmp_path: Path,
) -> None:
    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=MessageBus(),
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )

    binding = DistributionReportingRuntimeBinding(tmp_path, services)

    assert {id(provider.execution) for provider in binding._providers} == {
        id(binding._execution)
    }


@pytest.mark.asyncio
async def test_render_start_returns_after_initial_state_is_persisted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Inputs" / "approved.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Approved\n", encoding="utf-8")
    binding = DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=None,
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    runtime = binding._runtimes["render-existing"]
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def blocked_execute(plan, state, registry, contracts):
        del plan, registry, contracts
        runtime.state_store.save(
            state.model_copy(update={"status": WorkflowStatus.RUNNING})
        )
        started.set()
        await release.wait()
        runtime.state_store.save(
            state.model_copy(update={"status": WorkflowStatus.COMPLETED})
        )
        finished.set()

    runtime._execute = blocked_execute
    command_id = UUID("50000000-0000-4000-8000-000000000002")
    operation = asyncio.create_task(
        binding.start_detached(
            command_id,
            "render-existing",
            {
                "operation": "render_existing",
                "instruction": "Render the approved Markdown as DOCX.",
                "source_markdown_ref": "Inputs/approved.md",
                "output_filename": "approved.docx",
            },
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    accepted = await asyncio.wait_for(operation, timeout=1)

    assert accepted == {
        "run_id": f"render-existing-{command_id.hex}",
        "task_id": None,
    }
    assert binding.get_run(accepted["run_id"])["run"]["status"] == "running"
    assert binding.get_run(accepted["run_id"])["run"]["active"] is True

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert binding.get_run(accepted["run_id"])["run"]["status"] == "completed"
    assert binding.get_run(accepted["run_id"])["run"]["active"] is False
    await binding.close()


@pytest.mark.asyncio
async def test_waiting_input_returns_after_resumed_state_is_persisted(
    tmp_path: Path,
) -> None:
    binding = DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=None,
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    runtime = binding._runtimes["render-existing"]
    run_id = "render-existing-waiting"
    plan = ResolvedPlan(
        workflow_id="render-existing",
        workflow_version="1.0.0",
        actions=[],
    )
    waiting = WorkflowState.for_plan(run_id, plan)
    waiting.status = WorkflowStatus.WAITING
    waiting.waiting_input = {"input_id": "ask-evidence"}
    runtime.state_store.save(waiting)
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def blocked_provide_input(
        command_id: UUID,
        current_run_id: str,
        *,
        input_id: str | None,
        values: object,
    ) -> dict[str, object]:
        del command_id, input_id, values
        current = runtime.state_store.load(current_run_id)
        runtime.state_store.save(
            current.model_copy(
                update={"status": WorkflowStatus.RUNNING, "waiting_input": None}
            )
        )
        started.set()
        await release.wait()
        runtime.state_store.save(
            current.model_copy(
                update={"status": WorkflowStatus.COMPLETED, "waiting_input": None}
            )
        )
        finished.set()
        return {"run_id": current_run_id, "task_id": None}

    runtime.provide_input = blocked_provide_input
    operation = asyncio.create_task(
        binding.provide_input(
            UUID("50000000-0000-4000-8000-000000000004"),
            run_id,
            input_id="ask-evidence",
            values={"answer": "continue"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    accepted = await asyncio.wait_for(operation, timeout=1)

    assert accepted == {"run_id": run_id, "task_id": None}
    assert binding.get_run(run_id)["run"]["status"] == "running"
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert binding.get_run(run_id)["run"]["status"] == "completed"
    await binding.close()


@pytest.mark.asyncio
async def test_persisted_run_resume_reuses_one_in_process_task(
    tmp_path: Path,
) -> None:
    binding = DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=None,
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    runtime = _ResumableRuntime(tmp_path)
    runtime.store = binding._start_stores["render-existing"]
    binding._runtimes["render-existing"] = runtime
    run_id = "render-existing-process-restart"
    plan = ResolvedPlan(
        workflow_id="render-existing",
        workflow_version="1.0.0",
        actions=[],
    )
    state = WorkflowState.for_plan(run_id, plan)
    state.status = WorkflowStatus.RUNNING
    runtime.store.save_plan(run_id, plan)
    runtime.store.save(state)
    assert binding.get_run(run_id)["run"]["active"] is False

    first = asyncio.create_task(
        binding.resume(
            UUID("50000000-0000-4000-8000-000000000005"),
            run_id,
        )
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    accepted = await asyncio.wait_for(first, timeout=1)
    assert binding.get_run(run_id)["run"]["active"] is True
    duplicate = await binding.resume(
        UUID("50000000-0000-4000-8000-000000000006"),
        run_id,
    )

    assert accepted == {"run_id": run_id, "task_id": None}
    assert duplicate == {"run_id": run_id, "task_id": None}
    assert runtime.calls == [run_id]

    runtime.release.set()
    await binding.close()
    assert binding.get_run(run_id)["run"]["active"] is False


@pytest.mark.asyncio
async def test_binding_module_provider_reuses_persisted_completed_result_before_provider(
    tmp_path: Path,
) -> None:
    """The account binding must pass its persisted task identity to the bridge."""

    from manyselves.capabilities.distribution_reporting import (
        load_distribution_reporting_capability,
    )
    from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
        REPORT_TAXONOMY,
    )
    from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
        build_task_correlation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
        ModuleSubmission,
        TaskEnvelope,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleAuthoringPreparation,
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
    )
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        IdentityLeaseManager,
        TaskAttemptStore,
    )
    from manyselves.core.tools.registry import ToolRegistry
    from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
    from manyselves.kernel.definitions import (
        DefinitionKind,
        RecoveryPolicyDefinition,
        RecoveryRule,
    )

    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "module-2.4-specialist")
    task = registry.require(DefinitionKind.TASK, "module-2.4-authoring")
    workflow_id = "public-reporting"
    run_id = "binding-module-persisted"
    session_id = f"{workflow_id}:module-2.4"
    part_ids = list(REPORT_TAXONOMY["2.4"].submodules)
    envelope = TaskEnvelope(
        task_id=task.id,
        task_attempt_id="attempt-binding-module",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_submission"],
        allowed_tools=list(task.tools),
        target_submodule_ids=part_ids,
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id=workflow_id,
        reporting_state={"run_id": run_id},
        status="author_ready",
        authoring=DeclarativeModuleAuthoringPreparation(
            specialist_id=agent.id,
            envelope=envelope,
            revision=0,
            review=False,
            checkpoint=False,
        ),
    )
    submission = ModuleSubmission(
        module_id="2.4",
        submodule_narratives={part_id: f"正文 {part_id}" for part_id in part_ids},
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    lease_handle = IdentityLeaseManager(tmp_path, run_id).acquire(
        workflow_id,
        agent.id,
    )
    correlation = build_task_correlation(
        tmp_path,
        envelope,
        workflow_id=workflow_id,
        identity_key=agent.id,
        session_id=session_id,
        identity_lease=lease_handle.lease,
    )
    try:
        store = TaskAttemptStore(tmp_path, run_id)
        store.activate(correlation)
        store.persist_result(
            correlation,
            AgentResult(
                task_id=envelope.task_id,
                run_id=run_id,
                agent_id=agent.id,
                session_id=session_id,
                status=AgentRunStatus.COMPLETED,
                payload=submission,
            ).model_dump(mode="json"),
            status="completed",
        )
    finally:
        lease_handle.release()

    binding = DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    provider = binding._providers[0]
    provider.dependencies = ModuleProviderDependencies(
        artifact_gateway=object(),
        artifact_access=object(),
        result_index=object(),
    )
    provider.tool_builder = lambda *_args, **_kwargs: ToolRegistry()
    provider_calls = 0

    def forbidden_loop_builder(**_kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed result must be reused before Provider session")

    provider.loop_builder = forbidden_loop_builder
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id=agent.id, value="module-2.4", mode="run"),
        run_id=run_id,
    )
    try:
        outcome = await provider.invoke_with_recovery(
            agent,
            task,
            context,
            conversation,
            task_id=task.id,
            recovery_policy=RecoveryPolicyDefinition(
                id="binding-module-completed-reuse",
                version="1.0.0",
                description="reuse persisted completed result",
                rules={
                    "completed_tool_result": RecoveryRule(action="reuse_result"),
                },
            ),
        )
    finally:
        await binding.close()

    assert outcome.status == "ok"
    assert outcome.session_id == session_id
    assert outcome.result["status"] == "completed"
    assert ModuleSubmission.model_validate(outcome.result["module"]).module_id == "2.4"
    assert provider_calls == 0
