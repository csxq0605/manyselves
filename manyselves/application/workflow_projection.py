"""Generic Capability, Workflow, Run, Output, Cost, and Event projections."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.capabilities import load_builtin_capability_catalog
from manyselves.core.mimo_pricing import calculate_mimo_run_cost, format_mimo_cost
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    CapabilityCatalog,
    CapabilityCatalogError,
    ContractDefinition,
    DefinitionKind,
    LoadedCapability,
    WorkflowDefinition,
)
from manyselves.runtime.capability_binding import (
    CapabilityRunNotFoundError,
    RuntimeBindingCatalog,
    load_runtime_bindings,
)
from manyselves.runtime.state_store import FileWorkflowStateStore


class WorkflowProjectionNotFoundError(LookupError):
    """Raised when a requested Capability, Workflow, or Run is unavailable."""


class WorkflowNotRunnableError(ValueError):
    """Raised when an indexed subworkflow is not a public run entry point."""


class WorkflowInputError(ValueError):
    """Raised when a Capability adapter cannot interpret supplied run input."""


_RUN_STATE_FIELDS = (
    "run_id",
    "workflow_id",
    "status",
    "next_action_index",
    "next_action_id",
    "control_steps",
    "activity",
    "stage",
    "phase",
)
_RUN_STATE_ERROR_FIELDS = ("error", "error_message", "message")


def _bounded_run_state(state: Any) -> dict[str, Any]:
    """Keep the small state projection needed by the generic Run console.

    Capability bindings may retain large execution contexts in their state
    snapshots.  The generic Run endpoint exposes status and diagnostics, not
    those durable execution contexts; returning them makes opening a Run
    proportional to the entire workflow history and can block the browser.
    """

    if not isinstance(state, Mapping):
        return {}
    projected: dict[str, Any] = {}
    for key in _RUN_STATE_FIELDS:
        value = state.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                projected[key] = value
    for key in _RUN_STATE_ERROR_FIELDS:
        value = state.get(key)
        if isinstance(value, str) and value:
            projected[key] = value
    actions = state.get("actions")
    if isinstance(actions, Mapping):
        for action_id, action in actions.items():
            if not isinstance(action, Mapping):
                continue
            action_error = action.get("error")
            if isinstance(action_error, str) and action_error:
                projected.setdefault("error", action_error)
                projected.setdefault("error_action_id", str(action_id))
                break
    return projected


class WorkflowProjectionFacade:
    """Project neutral definitions over current Capability-owned run adapters."""

    def __init__(
        self,
        workspace: Path,
        runtime_services: Any,
        *,
        catalog: CapabilityCatalog | None = None,
        runtime_bindings: RuntimeBindingCatalog | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.runtime_services = runtime_services
        self._catalog = catalog or load_builtin_capability_catalog()
        self._runtime_bindings = runtime_bindings or load_runtime_bindings(
            self._catalog,
            workspace=self.workspace,
            services=runtime_services,
        )

    def list_capabilities(self) -> list[dict[str, Any]]:
        return [
            {
                "id": capability.id,
                "version": capability.version,
                "description": capability.description,
                "workflow_ids": sorted(
                    definition.id
                    for definition in registry.all(DefinitionKind.WORKFLOW)
                ),
            }
            for loaded in self._catalog.all()
            for capability, registry in [(loaded.definition, loaded.registry)]
        ]

    async def close(self) -> None:
        """Close the account-scoped Capability runtimes behind this projection."""

        await self._runtime_bindings.close()

    def list_workflows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": definition.id,
                "capability_id": capability.id,
                "version": definition.version,
                "description": definition.description,
                "input_contract": definition.input_contract,
                "output_contract": definition.output_contract,
                "runnable": (
                    definition.id in capability.entrypoints
                    and self._runtime_bindings.has(capability.id)
                ),
            }
            for loaded in self._catalog.all()
            for capability, registry in [(loaded.definition, loaded.registry)]
            for definition in sorted(
                registry.all(DefinitionKind.WORKFLOW),
                key=lambda item: item.id,
            )
            if isinstance(definition, WorkflowDefinition)
        ]

    def input_schema(self, workflow_id: str) -> dict[str, Any]:
        loaded, workflow = self._find_workflow(workflow_id)
        registry = loaded.registry
        if workflow.input_contract is None:
            schema: dict[str, Any] = {}
            contract_id = None
        else:
            contract = registry.get(
                DefinitionKind.CONTRACT,
                workflow.input_contract,
            )
            if not isinstance(contract, ContractDefinition):
                raise WorkflowProjectionNotFoundError(workflow.input_contract)
            contract_id = contract.id
            schema = build_contract_adapter(contract).json_schema()
        return {
            "workflow_id": workflow.id,
            "contract_id": contract_id,
            "schema": schema,
        }

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        loaded, _workflow = self._find_workflow(workflow_id)
        capability = loaded.definition
        if workflow_id not in capability.entrypoints:
            raise WorkflowNotRunnableError(workflow_id)
        binding = self._runtime_bindings.require(capability.id)
        accepted = await binding.start_detached(command_id, workflow_id, values)
        return self._accepted(accepted, capability.id, workflow_id)

    @property
    def active(self) -> bool:
        return self._runtime_bindings.active

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        binding, projection = self._locate_run(run_id)
        accepted = await binding.provide_input(
            command_id,
            run_id,
            input_id=input_id,
            values=values,
        )
        current = projection["run"]
        return self._accepted(
            accepted,
            current["capability_id"],
            current["workflow_id"],
        )

    async def resume(
        self,
        command_id: UUID,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume one persisted Run through its Capability-owned runtime."""

        binding, projection = self._locate_run(run_id)
        accepted = await binding.resume(command_id, run_id)
        current = projection["run"]
        return self._accepted(
            accepted,
            current["capability_id"],
            current["workflow_id"],
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        _binding, projection = self._locate_run(run_id)
        return {
            **projection,
            "state": _bounded_run_state(projection.get("state")),
        }

    def list_runs(self) -> list[dict[str, Any]]:
        """Project persisted Runs without requiring callers to know their ids."""

        store = FileWorkflowStateStore(self.workspace)
        projections: list[dict[str, Any]] = []
        for run_id in store.list_run_ids():
            try:
                projections.append(self.get_run(run_id))
            except WorkflowProjectionNotFoundError:
                continue
        return projections

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        binding, _projection = self._locate_run(run_id)
        return binding.get_outputs(run_id)

    def get_cost(self, run_id: str) -> dict[str, Any]:
        binding, _projection = self._locate_run(run_id)
        payload = binding.get_cost(run_id)
        # Provider pricing belongs to the application projection, not the
        # business-neutral usage ledger or a particular Capability's workflow.
        pricing = calculate_mimo_run_cost(self.workspace, run_id)
        if pricing is None:
            return payload
        usage = dict(payload.get("usage", {}))
        usage["pricing"] = pricing
        usage["pricing_summary"] = (
            "按已配置版本估算，非实际账单。\n" + format_mimo_cost(pricing)
        )
        usage["totals"] = {
            **usage.get("totals", {}),
            "pricing_status": "partial" if pricing["unpriced_attempts"] else "estimated",
            "pricing_table_version": pricing["pricing_version"],
            "pricing_currency": pricing["currency"],
            # Subscription allocation and API equivalent are separate in
            # pricing; neither is the customer's actual charge.
        }
        return {**payload, "usage": usage}

    def get_events(self, run_id: str) -> dict[str, Any]:
        """Project the Runtime Host trace persisted for this Run."""

        self._locate_run(run_id)
        path = self.workspace / "Work" / "runs" / run_id / "workflow-events.jsonl"
        if not path.exists():
            return {"run_id": run_id, "events": []}
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("run_id") == run_id:
                events.append(event)
        return {"run_id": run_id, "events": events}

    def _find_workflow(
        self,
        workflow_id: str,
    ) -> tuple[LoadedCapability, WorkflowDefinition]:
        try:
            return self._catalog.require_workflow(workflow_id)
        except CapabilityCatalogError as exc:
            raise WorkflowProjectionNotFoundError(workflow_id) from exc

    def _locate_run(self, run_id: str):
        try:
            return self._runtime_bindings.locate_run(run_id)
        except CapabilityRunNotFoundError as exc:
            raise WorkflowProjectionNotFoundError(run_id) from exc

    def _accepted(
        self,
        payload: dict[str, Any],
        capability_id: str,
        workflow_id: str,
    ) -> dict[str, Any]:
        return {
            "status": "accepted",
            "run_id": payload["run_id"],
            "task_id": payload.get("task_id"),
            "capability_id": capability_id,
            "workflow_id": workflow_id,
        }
