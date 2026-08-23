"""Durable user decisions for missing report evidence."""

from datetime import datetime, timezone
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceDecisionAction,
    EvidenceDecisionRequest,
)

from .store import ReportingStore


class EvidenceDecisionStore:
    """Persist decisions beneath their run and recover them without process state."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)

    @staticmethod
    def _safe_id(value: str, label: str) -> str:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError(f"{label} must be a safe path segment")
        return value

    def create(self, decision: EvidenceDecisionRequest) -> EvidenceDecisionRequest:
        run_id = self._safe_id(decision.run_id, "run_id")
        decision_id = self._safe_id(decision.decision_id, "decision_id")
        path = self.workspace / f"Work/runs/{run_id}/decisions/{decision_id}.json"
        if path.exists():
            raise ValueError(f"evidence decision already exists: {decision_id}")
        self.store.write_json(
            path.relative_to(self.workspace).as_posix(), decision.model_dump(mode="json")
        )
        return decision

    def load(self, decision_id: str) -> EvidenceDecisionRequest:
        safe_id = self._safe_id(decision_id, "decision_id")
        matches = list((self.workspace / "Work/runs").glob(f"*/decisions/{safe_id}.json"))
        if not matches:
            raise FileNotFoundError(f"unknown evidence decision: {safe_id}")
        if len(matches) != 1:
            raise ValueError(f"duplicate evidence decision id: {safe_id}")
        return EvidenceDecisionRequest.model_validate_json(matches[0].read_text(encoding="utf-8"))

    def resolve(
        self,
        decision_id: str,
        action: EvidenceDecisionAction,
        decision_note: str | None = None,
    ) -> EvidenceDecisionRequest:
        current = self.load(decision_id)
        if current.status != "pending":
            raise ValueError(f"evidence decision already resolved: {decision_id}")
        if action not in current.allowed_actions:
            raise ValueError(f"action is not allowed for evidence decision: {action}")
        resolved = current.model_copy(
            update={
                "status": "resolved",
                "selected_action": action,
                "decision_note": decision_note,
                "resolved_at": datetime.now(timezone.utc),
            }
        )
        self.store.write_json(
            f"Work/runs/{resolved.run_id}/decisions/{resolved.decision_id}.json",
            resolved.model_dump(mode="json"),
        )
        return resolved
