"""Generic Capability, Workflow, Run, Output, and Cost projections."""

from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.parameter_adjustment import (
    execute_parameter_adjustment,
    load_parameter_adjustment_capability,
)
from manyselves.core.reporting.models import ReportRequest, UserSupplement
from manyselves.core.usage_ledger import UsageLedger
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    CapabilityDefinition,
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
)
from manyselves.kernel.workflow import WorkflowState, WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore

_REPORTING_WORKFLOW_ID = "distribution-reporting"
_PARAMETER_WORKFLOW_ID = "parameter-adjustment"
_PARAMETER_RUN_PREFIX = f"{_PARAMETER_WORKFLOW_ID}-"


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
        self._definitions = [
            load_distribution_reporting_capability(),
            load_parameter_adjustment_capability(),
        ]

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
            for capability, registry in self._definitions
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
            for capability, registry in self._definitions
            for definition in sorted(
                registry.all(DefinitionKind.WORKFLOW),
                key=lambda item: item.id,
            )
            if isinstance(definition, WorkflowDefinition)
        ]

    def input_schema(self, workflow_id: str) -> dict[str, Any]:
        _capability, registry, workflow = self._find_workflow(workflow_id)
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
        capability, _registry, _workflow = self._find_workflow(workflow_id)
        if workflow_id != capability.id:
            raise WorkflowNotRunnableError(workflow_id)
        if workflow_id == _REPORTING_WORKFLOW_ID:
            request = ReportRequest.model_validate(values)
            accepted = self.reporting_adapter.start(command_id, request)
            return self._accepted(accepted, capability.id, workflow_id)
        if workflow_id == _PARAMETER_WORKFLOW_ID:
            run_id = f"{_PARAMETER_RUN_PREFIX}{command_id.hex}"
            await execute_parameter_adjustment(
                workspace=self.workspace,
                run_id=run_id,
                values=values,
            )
            return self._accepted(
                {"run_id": run_id, "task_id": None},
                capability.id,
                workflow_id,
            )
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
        if self._is_parameter_run(run_id):
            state = self._load_parameter_state(run_id)
            waiting_input = [state.waiting_input] if state.waiting_input is not None else []
            return {
                "run": {
                    "run_id": run_id,
                    "capability_id": _PARAMETER_WORKFLOW_ID,
                    "workflow_id": state.workflow_id,
                    "status": state.status.value,
                    "active": state.status in {
                        WorkflowStatus.PENDING,
                        WorkflowStatus.RUNNING,
                        WorkflowStatus.WAITING,
                    },
                    "task_id": None,
                },
                "state": state.model_dump(mode="json"),
                "waiting_input": waiting_input,
            }
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
        if self._is_parameter_run(run_id):
            state = self._load_parameter_state(run_id)
            return {
                "run_id": run_id,
                "outputs": [
                    {"id": output_id, "kind": "value", "value": value}
                    for output_id, value in state.outputs.items()
                ],
            }
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
    ) -> tuple[CapabilityDefinition, DefinitionRegistry, WorkflowDefinition]:
        for capability, registry in self._definitions:
            workflow = registry.get(DefinitionKind.WORKFLOW, workflow_id)
            if isinstance(workflow, WorkflowDefinition):
                return capability, registry, workflow
        raise WorkflowProjectionNotFoundError(workflow_id)

    @staticmethod
    def _is_parameter_run(run_id: str) -> bool:
        return run_id.startswith(_PARAMETER_RUN_PREFIX)

    def _load_parameter_state(self, run_id: str) -> WorkflowState:
        try:
            return FileWorkflowStateStore(self.workspace).load(run_id)
        except FileNotFoundError as exc:
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
