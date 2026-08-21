"""Application runtime binding owned by distribution reporting."""

from pathlib import Path
from typing import Any
from uuid import UUID

from manyselves.application.reporting_facade import ReportingNotFoundError
from manyselves.core.reporting.models import ReportRequest, UserSupplement
from manyselves.core.usage_ledger import UsageLedger
from manyselves.runtime.capability_binding import CapabilityRunNotFoundError


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
        values: dict[str, Any],
    ) -> dict[str, Any]:
        request = ReportRequest.model_validate(values)
        return self.reporting_adapter.start_declarative(command_id, request)

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

    def get_run(self, run_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(run_id)
        current = snapshot.get("run", {})
        state = snapshot.get("state", {})
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": "distribution-reporting",
                "status": current.get("status") or state.get("status") or "unknown",
                "active": bool(current.get("active", False)),
                "task_id": current.get("task_id"),
            },
            "state": state,
            "waiting_input": snapshot.get("waitingInput", []),
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


def build_runtime_binding(
    *,
    workspace: Path,
    host: Any,
) -> DistributionReportingRuntimeBinding:
    return DistributionReportingRuntimeBinding(workspace, host)


__all__ = ["DistributionReportingRuntimeBinding", "build_runtime_binding"]
