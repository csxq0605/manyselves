"""Application runtime binding owned by distribution reporting."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.application.reporting_facade import (
    ReportingInvalidTransitionError,
    ReportingNotFoundError,
    ReportingStateInvalidError,
)
from manyselves.core.reporting.models import ReportRequest, UserSupplement
from manyselves.core.usage_ledger import UsageLedger
from manyselves.kernel.workflow import WorkflowState, WorkflowStatus
from manyselves.runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from manyselves.runtime.state_store import FileWorkflowStateStore


class DistributionReportingRuntimeBinding:
    """Translate generic Run operations at the Reporting capability boundary."""

    capability_id = "distribution-reporting"

    def __init__(self, workspace: Path, reporting_adapter: Any) -> None:
        self.workspace = Path(workspace)
        self.reporting_adapter = reporting_adapter

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        request = ReportRequest.model_validate(values)
        try:
            return self.reporting_adapter.start_declarative(command_id, request)
        except ReportingInvalidTransitionError as exc:
            raise CapabilityRunInputError(str(exc)) from exc
        except ReportingStateInvalidError as exc:
            raise CapabilityRunStateError(str(exc)) from exc

    def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        try:
            runtime_state = self._runtime_state(run_id)
            if input_id is not None and runtime_state is not None:
                if runtime_state.status is WorkflowStatus.WAITING:
                    return self.reporting_adapter.resume_workflow_input(
                        command_id,
                        run_id,
                        input_id,
                        values,
                    )
            if not isinstance(values, Mapping):
                raise CapabilityRunInputError(
                    "legacy reporting input requires a JSON object"
                )
            supplements = [
                UserSupplement.model_validate(item)
                for item in values.get("supplements", [])
            ]
            if input_id is not None:
                action = values.get("action")
                if not isinstance(action, str) or not action:
                    raise ValueError("decision input requires action")
                return self.reporting_adapter.resume_decision(
                    command_id,
                    input_id,
                    action,
                    supplements,
                )
            return self.reporting_adapter.resume_run(
                command_id,
                run_id,
                max_provider_attempts=values.get("max_provider_attempts"),
                max_total_tokens=values.get("max_total_tokens"),
                supplements=supplements,
            )
        except ReportingNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        except ReportingInvalidTransitionError as exc:
            raise CapabilityRunInputError(str(exc)) from exc
        except ReportingStateInvalidError as exc:
            raise CapabilityRunStateError(str(exc)) from exc

    def get_run(self, run_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(run_id)
        current = snapshot.get("run", {})
        runtime_state = self._runtime_state(run_id)
        state = (
            runtime_state.model_dump(mode="json")
            if runtime_state is not None
            else snapshot.get("state", {})
        )
        waiting_input = (
            [runtime_state.waiting_input]
            if runtime_state is not None and runtime_state.waiting_input is not None
            else snapshot.get("waitingInput", [])
        )
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": (
                    runtime_state.workflow_id
                    if runtime_state is not None
                    else "distribution-reporting"
                ),
                "status": state.get("status") or current.get("status") or "unknown",
                "active": bool(current.get("active", False)),
                "task_id": current.get("task_id"),
            },
            "state": state,
            "waiting_input": waiting_input,
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(run_id)
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
        self._snapshot(run_id)
        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    def _snapshot(self, run_id: str) -> dict[str, Any]:
        try:
            return self.reporting_adapter.snapshot(run_id)
        except ReportingNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        except ReportingStateInvalidError as exc:
            raise CapabilityRunStateError(str(exc)) from exc

    def _runtime_state(self, run_id: str) -> WorkflowState | None:
        try:
            return FileWorkflowStateStore(self.workspace).load(run_id)
        except FileNotFoundError:
            return None


def build_runtime_binding(
    *,
    workspace: Path,
    host: Any,
) -> DistributionReportingRuntimeBinding:
    return DistributionReportingRuntimeBinding(workspace, host)


__all__ = ["DistributionReportingRuntimeBinding", "build_runtime_binding"]
