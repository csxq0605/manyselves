"""Generic Capability, Workflow, Run, Output, and Cost projections."""

from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.core.reporting.models import ReportRequest, UserSupplement
from manyselves.core.usage_ledger import UsageLedger
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    WorkflowDefinition,
)


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
        self.capability, self.registry = load_distribution_reporting_capability()

    def list_capabilities(self) -> list[dict[str, Any]]:
        workflow_ids = sorted(
            definition.id
            for definition in self.registry.all(DefinitionKind.WORKFLOW)
        )
        return [
            {
                "id": self.capability.id,
                "version": self.capability.version,
                "description": self.capability.description,
                "workflow_ids": workflow_ids,
            }
        ]

    def list_workflows(self) -> list[dict[str, Any]]:
        workflows = self.registry.all(DefinitionKind.WORKFLOW)
        return [
            {
                "id": definition.id,
                "capability_id": self.capability.id,
                "version": definition.version,
                "description": definition.description,
                "input_contract": definition.input_contract,
                "output_contract": definition.output_contract,
                "runnable": definition.id == self.capability.id,
            }
            for definition in sorted(workflows, key=lambda item: item.id)
            if isinstance(definition, WorkflowDefinition)
        ]

    def input_schema(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.registry.get(DefinitionKind.WORKFLOW, workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise WorkflowProjectionNotFoundError(workflow_id)
        if workflow.input_contract is None:
            schema: dict[str, Any] = {}
            contract_id = None
        else:
            contract = self.registry.get(
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

    def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        if workflow_id != self.capability.id:
            raise WorkflowNotRunnableError(workflow_id)
        request = ReportRequest.model_validate(values)
        accepted = self.reporting_adapter.start(command_id, request)
        return self._accepted(accepted, workflow_id)

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
        return self._accepted(accepted, self.capability.id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        snapshot = self.reporting_adapter.snapshot(run_id)
        current = snapshot.get("run", {})
        state = snapshot.get("state", {})
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability.id,
                "workflow_id": self.capability.id,
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

    def _accepted(self, payload: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        return {
            "status": "accepted",
            "run_id": payload["run_id"],
            "task_id": payload.get("task_id"),
            "capability_id": self.capability.id,
            "workflow_id": workflow_id,
        }

