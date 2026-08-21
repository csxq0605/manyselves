"""Generic Capability, Workflow, Run, Output, and Cost projections."""

from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.capabilities import load_builtin_capability_catalog
from manyselves.core.reporting.models import ReportRequest, UserSupplement
from manyselves.core.usage_ledger import UsageLedger
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    CapabilityCatalogError,
    ContractDefinition,
    DefinitionKind,
    LoadedCapability,
    WorkflowDefinition,
)

_REPORTING_WORKFLOW_ID = "distribution-reporting"


class WorkflowProjectionNotFoundError(LookupError):
    """Raised when a requested Capability, Workflow, or Run is unavailable."""


class WorkflowNotRunnableError(ValueError):
    """Raised when an indexed subworkflow is not a public run entry point."""


class WorkflowInputError(ValueError):
    """Raised when a Capability adapter cannot interpret supplied run input."""


class WorkflowProjectionFacade:
    """Project neutral definitions over current Capability-owned run adapters."""

    def __init__(self, workspace: Path, reporting_adapter: Any) -> None:
        self.workspace = Path(workspace)
        self.reporting_adapter = reporting_adapter
        self._catalog = load_builtin_capability_catalog()

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

    def list_workflows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": definition.id,
                "capability_id": capability.id,
                "version": definition.version,
                "description": definition.description,
                "input_contract": definition.input_contract,
                "output_contract": definition.output_contract,
                "runnable": definition.id == capability.id,
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
        values: dict[str, Any],
    ) -> dict[str, Any]:
        loaded, _workflow = self._find_workflow(workflow_id)
        capability = loaded.definition
        if workflow_id != capability.id:
            raise WorkflowNotRunnableError(workflow_id)
        if workflow_id == _REPORTING_WORKFLOW_ID:
            request = ReportRequest.model_validate(values)
            accepted = self.reporting_adapter.start_declarative(command_id, request)
            return self._accepted(accepted, capability.id, workflow_id)
        raise WorkflowNotRunnableError(workflow_id)

    def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        supplements = [
            UserSupplement.model_validate(item)
            for item in values.get("supplements", [])
        ]
        if input_id is not None:
            action = values.get("action")
            if not isinstance(action, str) or not action:
                raise WorkflowInputError("decision input requires action")
            accepted = self.reporting_adapter.resume_decision(
                command_id,
                input_id,
                action,
                supplements,
            )
        else:
            accepted = self.reporting_adapter.resume_run(
                command_id,
                run_id,
                max_provider_attempts=values.get("max_provider_attempts"),
                max_total_tokens=values.get("max_total_tokens"),
                supplements=supplements,
            )
        return self._accepted(
            accepted,
            _REPORTING_WORKFLOW_ID,
            _REPORTING_WORKFLOW_ID,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        snapshot = self.reporting_adapter.snapshot(run_id)
        current = snapshot.get("run", {})
        state = snapshot.get("state", {})
        return {
            "run": {
                "run_id": run_id,
                "capability_id": _REPORTING_WORKFLOW_ID,
                "workflow_id": _REPORTING_WORKFLOW_ID,
                "status": current.get("status") or state.get("status") or "unknown",
                "active": bool(current.get("active", False)),
                "task_id": current.get("task_id"),
            },
            "state": state,
            "waiting_input": snapshot.get("waitingInput", []),
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        snapshot = self.reporting_adapter.snapshot(run_id)
        return {
            "run_id": run_id,
            "outputs": [
                {
                    "id": output.get("path", ""),
                    "kind": "artifact",
                    "path": output.get("path", ""),
                    "exists": bool(output.get("exists", False)),
                    "size": int(output.get("size", 0) or 0),
                }
                for output in snapshot.get("outputs", [])
            ],
        }

    def get_cost(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    def _find_workflow(
        self,
        workflow_id: str,
    ) -> tuple[LoadedCapability, WorkflowDefinition]:
        try:
            return self._catalog.require_workflow(workflow_id)
        except CapabilityCatalogError as exc:
            raise WorkflowProjectionNotFoundError(workflow_id) from exc

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
