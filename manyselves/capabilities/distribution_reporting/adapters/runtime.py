"""Application runtime binding owned by Distribution Reporting."""

from __future__ import annotations

from collections.abc import Awaitable
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.capabilities.distribution_reporting.runtime.content_snapshot import (
    snapshot_content,
)
from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
    RunInputSnapshotStore,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_report import (
    build_ledger_report_binding,
)
from manyselves.capabilities.distribution_reporting.runtime.render_existing import (
    RenderExistingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.template_skill_binding import (
    build_template_skill_binding,
)
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.artifacts.content_store import ContentAddressedStore
from manyselves.runtime.capability_binding import CapabilityRunNotFoundError
from manyselves.runtime.run_lifecycle import (
    DetachedRunTaskOwner,
    DetachedRuntime,
    StartAwareFileWorkflowStateStore,
)
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.state_store import FileWorkflowStateStore


class DistributionReportingRuntimeBinding:
    """Route public Reporting roots to their Capability-owned Generic Hosts."""

    capability_id = "distribution-reporting"

    def __init__(self, workspace: Path, services: RuntimeServicesView) -> None:
        self.workspace = Path(workspace).resolve()
        self.services = services
        self._execution = AgentExecutionService(services.bus)
        self._detached_runs = DetachedRunTaskOwner()
        render_state_store = StartAwareFileWorkflowStateStore(self.workspace)
        snapshot_store = RunInputSnapshotStore(self.workspace)
        content_store = ContentAddressedStore(self.workspace)
        ledger_report = build_ledger_report_binding(
            self.workspace,
            services,
            input_snapshot=snapshot_store.load,
            snapshot_content=partial(snapshot_content, self.workspace, content_store),
            execution=self._execution,
        )
        template_skills = build_template_skill_binding(
            self.workspace, services, execution=self._execution
        )
        self._providers = (ledger_report, template_skills)
        self._runtimes: dict[str, DetachedRuntime] = {
            "full-report": ledger_report,
            "extract-template-skills": template_skills,
            "triage-writing-lessons": template_skills,
            "render-existing": RenderExistingWorkflowRuntime(
                self.workspace,
                state_store=render_state_store,
            ),
        }
        self._start_stores = {
            "full-report": ledger_report.state_store,
            "extract-template-skills": template_skills.state_store,
            "triage-writing-lessons": template_skills.state_store,
            "render-existing": render_state_store,
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

    async def rerun(
        self,
        command_id: UUID,
        run_id: str,
        values: Any = None,
    ) -> dict[str, Any]:
        """Run a report again in place as its next version (same run id)."""

        runtime, _store = self._runtime_and_store_for_run(run_id)
        rerun = getattr(runtime, "rerun", None)
        if not callable(rerun):
            raise ValueError(f"run {run_id} cannot be run again in place")
        return await rerun(command_id, run_id, values)

    def versions(self, run_id: str) -> list[dict[str, Any]]:
        runtime, _store = self._runtime_and_store_for_run(run_id)
        versions = getattr(runtime, "versions", None)
        return list(versions(run_id)) if callable(versions) else []

    def add_settled_listener(self, listener: Any) -> None:
        for provider in self._providers:
            provider.add_settled_listener(listener)

    def cancel(self, run_id: str) -> Awaitable[Any] | None:
        """Request a stop of this process's in-flight execution of one Run."""

        self._runtime_for_run(run_id)
        return self._detached_runs.cancel(run_id)

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
