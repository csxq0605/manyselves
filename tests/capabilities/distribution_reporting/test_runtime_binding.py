"""Characterization for the public Distribution Reporting runtime binding."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting.adapters.runtime import (
    DistributionReportingRuntimeBinding,
)
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.workflow import ResolvedPlan, WorkflowState, WorkflowStatus
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.webapi.routes.workflows import _projection


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


def test_web_projection_reuses_the_account_runtime_catalog() -> None:
    projection = object()
    state = SimpleNamespace(workflow_projection=projection)
    request = SimpleNamespace(state=SimpleNamespace(tenant_runtime=state))

    assert _projection(request) is projection
    assert _projection(request) is projection


def test_full_report_binding_runs_the_ledger_flow_on_the_generic_binding(
    tmp_path: Path,
) -> None:
    from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=MessageBus(),
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    binding = DistributionReportingRuntimeBinding(tmp_path, services)
    runtime = binding._runtimes["full-report"]

    assert isinstance(runtime, GenericCapabilityRuntimeBinding)
    assert binding._start_stores["full-report"] is runtime.state_store
    from manyselves.runtime.generic_binding import _contracts

    registry, plan = runtime._compile("full-report")
    tools = runtime._tools(plan, _contracts(registry), None)
    assert {
        "prepare-ledger-context",
        "build-judgment-ledger",
        "finalize-judgment-ledger",
        "check-report",
        "compose-report",
    } <= tools.keys()
    assert "distribution-reporting-tail" not in plan.subworkflow_plans


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


