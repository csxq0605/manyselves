"""Application runtime binding owned by Distribution Reporting."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

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
from manyselves.runtime.run_lifecycle import (
    DetachedRunTaskOwner,
    DetachedRuntime,
    StartAwareFileWorkflowStateStore,
)
from manyselves.runtime.services import RuntimeServicesView
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
        self._detached_runs = DetachedRunTaskOwner()
        public_state_store = StartAwareFileWorkflowStateStore(self.workspace)
        aggregate_state_store = StartAwareFileWorkflowStateStore(self.workspace)
        render_state_store = StartAwareFileWorkflowStateStore(self.workspace)
        template_state_store = StartAwareFileWorkflowStateStore(self.workspace)
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
            state_store=public_state_store,
        )
        aggregate_invokers = {
            **aggregate.agent_invokers,
            **final.agent_invokers,
            **final_chief.agent_invokers,
        }
        self._runtimes: dict[str, DetachedRuntime] = {
            "full-report": public,
            "module-report": public,
            "aggregate-existing": PublicAggregateExistingWorkflowRuntime(
                self.workspace,
                input_snapshot=snapshot_store,
                agent_invokers=aggregate_invokers,
                state_store=aggregate_state_store,
            ),
            "render-existing": RenderExistingWorkflowRuntime(
                self.workspace,
                state_store=render_state_store,
            ),
            "distill-template-skill": TemplateDistillationWorkflowRuntime(
                self.workspace,
                agent_invoker=template,
                state_store=template_state_store,
            ),
        }
        self._start_stores = {
            "full-report": public_state_store,
            "module-report": public_state_store,
            "aggregate-existing": aggregate_state_store,
            "render-existing": render_state_store,
            "distill-template-skill": template_state_store,
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

    async def start_detached(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        """Accept a Run after its Host has persisted the initial state."""

        try:
            runtime = self._runtimes[workflow_id]
        except KeyError as exc:
            raise ValueError(f"workflow is not runnable: {workflow_id}") from exc
        store = self._start_stores[workflow_id]
        run_id = runtime.run_id_for(command_id, workflow_id)
        return await self._detached_runs.accept_after_persisted_state(
            run_id=run_id,
            state_store=store,
            operation=runtime.start(command_id, workflow_id, values),
        )

    @property
    def active(self) -> bool:
        return self._detached_runs.active

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        """Accept a continuation after the Host persists its next state."""

        runtime, store = self._runtime_and_store_for_run(run_id)
        return await self._detached_runs.accept_after_persisted_state(
            run_id=run_id,
            state_store=store,
            operation=runtime.provide_input(
                command_id,
                run_id,
                input_id=input_id,
                values=values,
            ),
        )

    async def resume(
        self,
        command_id: UUID,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume a persisted Run after an in-process task disappeared."""

        if self._detached_runs.is_active(run_id):
            return {"run_id": run_id, "task_id": None}
        runtime, store = self._runtime_and_store_for_run(run_id)
        return await self._detached_runs.accept_after_persisted_state(
            run_id=run_id,
            state_store=store,
            operation=runtime.resume(command_id, run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        projection = self._runtime_for_run(run_id).get_run(run_id)
        run = projection.get("run")
        if isinstance(run, dict):
            run["active"] = self._detached_runs.is_active(run_id)
        return projection

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).get_outputs(run_id)

    def get_cost(self, run_id: str) -> dict[str, Any]:
        return self._runtime_for_run(run_id).get_cost(run_id)

    async def close(self) -> None:
        """Close the Agent sessions owned by this account binding."""

        await self._detached_runs.close()
        for provider in reversed(self._providers):
            await provider.close()

    def _runtime_for_run(self, run_id: str) -> Any:
        runtime, _store = self._runtime_and_store_for_run(run_id)
        return runtime

    def _runtime_and_store_for_run(
        self,
        run_id: str,
    ) -> tuple[DetachedRuntime, StartAwareFileWorkflowStateStore]:
        try:
            state = FileWorkflowStateStore(self.workspace).load(run_id)
            return self._runtimes[state.workflow_id], self._start_stores[state.workflow_id]
        except (FileNotFoundError, KeyError) as exc:
            raise CapabilityRunNotFoundError(run_id) from exc


def build_runtime_binding(
    *,
    workspace: Path,
    services: RuntimeServicesView,
) -> DistributionReportingRuntimeBinding:
    return DistributionReportingRuntimeBinding(workspace, services)


__all__ = ["DistributionReportingRuntimeBinding", "build_runtime_binding"]
