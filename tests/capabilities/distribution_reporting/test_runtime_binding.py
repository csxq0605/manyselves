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
from manyselves.kernel.workflow import WorkflowStatus
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
