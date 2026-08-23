"""Application runtime binding owned by Distribution Reporting."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.domain.photo_bindings import (
    runtime_photo_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
    PublicAggregateExistingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.aggregate_provider import (
    build_aggregate_provider_composition,
)
from manyselves.capabilities.distribution_reporting.runtime.chief_provider import (
    build_chief_provider_composition,
)
from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
    ChiefChapterRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.content_snapshot import (
    snapshot_content,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_provider import (
    build_cross_provider_composition,
)
from manyselves.capabilities.distribution_reporting.runtime.final_chief_provider import (
    build_final_chief_provider_composition,
)
from manyselves.capabilities.distribution_reporting.runtime.final_provider import (
    build_final_provider_composition,
)
from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
    RunInputSnapshotStore,
)
from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
    build_module_provider_composition,
)
from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
    PublicReportingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.render_existing import (
    RenderExistingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
    ReportingTailComposition,
)
from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
    TemplateDistillationWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.template_provider import (
    TemplateDistillationProviderRuntime,
)
from manyselves.core.artifacts.content_store import ContentAddressedStore
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.capability_binding import CapabilityRunNotFoundError
from manyselves.runtime.state_store import FileWorkflowStateStore


class _TaskContractAgentRouter:
    """Select a Capability Agent implementation by its declared Task contract."""

    def __init__(self, default: Any, routes: dict[str, Any]) -> None:
        self.default = default
        self.routes = routes

    def _invoker(self, task: Any) -> Any:
        return self.routes.get(task.output_contract, self.default)

    async def invoke(self, agent: Any, task: Any, value: Any, conversation: Any, **kwargs: Any):
        return await self._invoker(task).invoke(
            agent,
            task,
            value,
            conversation,
            **kwargs,
        )

    async def invoke_with_recovery(
        self,
        agent: Any,
        task: Any,
        value: Any,
        conversation: Any,
        **kwargs: Any,
    ):
        return await self._invoker(task).invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            **kwargs,
        )


class DistributionReportingRuntimeBinding:
    """Route public Reporting roots to their Capability-owned Generic Hosts."""

    capability_id = "distribution-reporting"

    def __init__(self, workspace: Path, services: RuntimeServicesView) -> None:
        self.workspace = Path(workspace).resolve()
        self.services = services
        self._execution = AgentExecutionService(services.bus)
        snapshot_store = RunInputSnapshotStore(self.workspace)
        content_store = ContentAddressedStore(self.workspace)
        module = build_module_provider_composition(
            services,
            execution=self._execution,
        )
        aggregate = build_aggregate_provider_composition(
            services,
            execution=self._execution,
        )
        final = build_final_provider_composition(
            services,
            execution=self._execution,
        )
        final_chief = build_final_chief_provider_composition(
            services,
            execution=self._execution,
        )
        template = TemplateDistillationProviderRuntime(
            services,
            execution=self._execution,
        )
        cross_lifecycle = CrossOwnerRuntime(self.workspace)
        cross = build_cross_provider_composition(
            services,
            cross_runtime=cross_lifecycle,
            execution=self._execution,
        )
        chief_lifecycle = ChiefChapterRuntime(self.workspace)
        chief = build_chief_provider_composition(
            services,
            chief_runtime=chief_lifecycle,
            execution=self._execution,
        )
        full_final = build_final_provider_composition(
            services,
            workflow_id="public-reporting",
            execution=self._execution,
        )
        full_final_chief = build_final_chief_provider_composition(
            services,
            workflow_id="public-reporting",
            execution=self._execution,
        )
        tail = ReportingTailComposition(
            self.workspace,
            cross_runtime=cross_lifecycle,
            chief_runtime=chief,
        )
        chief_router = _TaskContractAgentRouter(
            chief.provider,
            {
                "declarative_final_chief_revision_agent_result": (
                    full_final_chief.provider
                )
            },
        )
        full_agents = {
            **cross.agent_invokers,
            **full_final.agent_invokers,
            "chief-editor": chief_router,
        }
        self._providers = (
            module.provider,
            aggregate,
            final,
            final_chief,
            template,
            cross,
            chief,
            full_final,
            full_final_chief,
        )

        public = PublicReportingWorkflowRuntime(
            self.workspace,
            input_snapshot=snapshot_store.load,
            snapshot_content=partial(
                snapshot_content,
                self.workspace,
                content_store,
            ),
            runtime_photo_ids=runtime_photo_ids,
            module_runtime=module.module_runtime,
            workflow_specializers=(tail.workflow_specializer,),
            additional_tool_implementations=tail.tool_implementations(),
            additional_agent_invokers=full_agents,
        )
        aggregate_invokers = {
            **aggregate.agent_invokers,
            **final.agent_invokers,
            **final_chief.agent_invokers,
        }
        self._runtimes = {
            "full-report": public,
            "module-report": public,
            "aggregate-existing": PublicAggregateExistingWorkflowRuntime(
                self.workspace,
                input_snapshot=snapshot_store,
                agent_invokers=aggregate_invokers,
            ),
            "render-existing": RenderExistingWorkflowRuntime(self.workspace),
            "distill-template-skill": TemplateDistillationWorkflowRuntime(
                self.workspace,
                agent_invoker=template,
            ),
        }

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        try:
            runtime = self._runtimes[workflow_id]
        except KeyError as exc:
            raise ValueError(f"workflow is not runnable: {workflow_id}") from exc
        return await runtime.start(command_id, workflow_id, values)

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        runtime = self._runtime_for_run(run_id)
        return await runtime.provide_input(
            command_id,
            run_id,
            input_id=input_id,
            values=values,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).get_run(run_id)

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).get_outputs(run_id)

    def get_cost(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).get_cost(run_id)

    async def close(self) -> None:
        """Close the Agent sessions owned by this account binding."""

        for provider in reversed(self._providers):
            await provider.close()

    def _runtime_for_run(self, run_id: str) -> Any:
        try:
            state = FileWorkflowStateStore(self.workspace).load(run_id)
            return self._runtimes[state.workflow_id]
        except (FileNotFoundError, KeyError) as exc:
            raise CapabilityRunNotFoundError(run_id) from exc


def build_runtime_binding(
    *,
    workspace: Path,
    services: RuntimeServicesView,
) -> DistributionReportingRuntimeBinding:
    return DistributionReportingRuntimeBinding(workspace, services)


__all__ = ["DistributionReportingRuntimeBinding", "build_runtime_binding"]
