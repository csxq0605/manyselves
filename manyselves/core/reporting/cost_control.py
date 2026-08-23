"""Stage-boundary cost policy for recoverable reporting workflows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import CostControlMode

from .store import ReportingStore


class StageCostController:
    """Persist observe/warn/pause decisions without interrupting provider turns."""

    VERSION = 1

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        *,
        mode: CostControlMode,
        attempt_window: int,
        token_window: int,
    ):
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("run_id must be one safe path component")
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.mode = mode
        self.attempt_window = int(attempt_window)
        self.token_window = int(token_window)
        self.store = ReportingStore(self.workspace)
        self.relative_path = f"Work/runs/{run_id}/cost-control.json"
        self.state = self._load()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load(self) -> dict[str, Any]:
        target = self.workspace / self.relative_path
        if target.is_file():
            raw = json.loads(target.read_text(encoding="utf-8"))
            if raw.get("version") != self.VERSION or raw.get("run_id") != self.run_id:
                raise ValueError("cost-control state is incompatible with this run")
            raw["mode"] = self.mode
            raw["attempt_window"] = self.attempt_window
            raw["token_window"] = self.token_window
            raw.setdefault("boundaries", [])
            raw.setdefault("pending_boundary_evaluation", None)
            return raw
        return {
            "version": self.VERSION,
            "run_id": self.run_id,
            "mode": self.mode,
            "attempt_window": self.attempt_window,
            "token_window": self.token_window,
            "next_provider_attempt_threshold": self.attempt_window,
            "next_total_token_threshold": self.token_window,
            "pending_decision": None,
            "pending_boundary_evaluation": None,
            "last_resolution": None,
            "boundaries": [],
        }

    def _save(self) -> None:
        self.state["mode"] = self.mode
        self.state["attempt_window"] = self.attempt_window
        self.state["token_window"] = self.token_window
        self.state["boundaries"] = list(self.state.get("boundaries", []))[-200:]
        self.store.write_json(self.relative_path, self.state)

    def resolve_resume(self, usage: dict[str, Any]) -> bool:
        """Treat an explicit same-run resume as approval for one new cost window."""

        pending = self.state.get("pending_decision")
        if not isinstance(pending, dict):
            return False
        attempts = int(usage.get("provider_attempts", 0) or 0)
        tokens = int(usage.get("total_tokens", 0) or 0)
        self.state["last_resolution"] = {
            **pending,
            "status": "continued_same_run",
            "resolved_at": self._now(),
        }
        self.state["pending_decision"] = None
        self.state["next_provider_attempt_threshold"] = attempts + self.attempt_window
        self.state["next_total_token_threshold"] = tokens + self.token_window
        self._save()
        return True

    def prepare_boundary(
        self,
        *,
        completed_stage: str,
        next_stage: str | None,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        """Durably declare a safe boundary before its workflow checkpoint.

        The declaration closes the crash window between checkpoint persistence
        and policy evaluation.  A resumed workflow can evaluate this exact
        boundary before issuing another Provider request.
        """

        pending = self.state.get("pending_boundary_evaluation")
        if isinstance(pending, dict):
            if (
                pending.get("completed_stage") != completed_stage
                or pending.get("next_stage") != next_stage
            ):
                raise ValueError(
                    "a different cost boundary is already awaiting evaluation"
                )
            return dict(pending)
        prepared = {
            "boundary_id": uuid4().hex,
            "prepared_at": self._now(),
            "checkpoint_status": "prepared",
            "completed_stage": completed_stage,
            "next_stage": next_stage,
            "provider_attempts": int(
                usage.get("provider_attempts", 0) or 0
            ),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
        self.state["pending_boundary_evaluation"] = prepared
        self._save()
        return dict(prepared)

    def pending_boundary(self) -> dict[str, Any] | None:
        pending = self.state.get("pending_boundary_evaluation")
        return dict(pending) if isinstance(pending, dict) else None

    def confirm_boundary_checkpoint(self, boundary_id: str) -> dict[str, Any]:
        """Mark that the workflow checkpoint contains this exact boundary id."""

        pending = self.state.get("pending_boundary_evaluation")
        if (
            not isinstance(pending, dict)
            or pending.get("boundary_id") != boundary_id
        ):
            raise ValueError("cost boundary checkpoint id does not match pending intent")
        pending = {
            **pending,
            "checkpoint_status": "committed",
            "checkpoint_confirmed_at": self._now(),
        }
        self.state["pending_boundary_evaluation"] = pending
        self._save()
        return dict(pending)

    def discard_uncommitted_boundary(
        self,
        boundary_id: str,
        *,
        reason: str,
    ) -> bool:
        """Discard intent that never appeared in the durable workflow checkpoint."""

        pending = self.state.get("pending_boundary_evaluation")
        if (
            not isinstance(pending, dict)
            or pending.get("boundary_id") != boundary_id
        ):
            return False
        self.state["pending_boundary_evaluation"] = None
        self.state["last_discarded_boundary"] = {
            **pending,
            "discarded_at": self._now(),
            "discard_reason": reason,
        }
        self._save()
        return True

    def evaluate(
        self,
        *,
        completed_stage: str,
        next_stage: str | None,
        usage: dict[str, Any],
    ) -> dict[str, Any]:
        """Record one safe boundary and return whether the workflow must pause."""

        pending = self.state.get("pending_boundary_evaluation")
        if not isinstance(pending, dict):
            raise ValueError(
                "cost boundary evaluation requires a prepared and "
                "checkpoint-committed boundary"
            )
        if isinstance(pending, dict) and (
            pending.get("completed_stage") != completed_stage
            or pending.get("next_stage") != next_stage
        ):
            raise ValueError(
                "cost boundary evaluation does not match the durable pending boundary"
            )
        if (
            isinstance(pending, dict)
            and pending.get("checkpoint_status") != "committed"
        ):
            raise ValueError(
                "cost boundary cannot be evaluated before its checkpoint is committed"
            )
        attempts = int(usage.get("provider_attempts", 0) or 0)
        tokens = int(usage.get("total_tokens", 0) or 0)
        attempt_threshold = int(
            self.state.get("next_provider_attempt_threshold", self.attempt_window)
        )
        token_threshold = int(
            self.state.get("next_total_token_threshold", self.token_window)
        )
        reasons = []
        if attempts >= attempt_threshold:
            reasons.append("provider_attempts")
        if tokens >= token_threshold:
            reasons.append("total_tokens")

        action = "observe"
        if reasons and self.mode == "warn":
            action = "warn"
        elif reasons and self.mode == "pause_at_boundary":
            action = "pause"

        event = {
            "evaluated_at": self._now(),
            "completed_stage": completed_stage,
            "next_stage": next_stage,
            "provider_attempts": attempts,
            "total_tokens": tokens,
            "attempt_threshold": attempt_threshold,
            "token_threshold": token_threshold,
            "reasons": reasons,
            "action": action,
        }
        self.state.setdefault("boundaries", []).append(event)

        if action == "warn":
            self.state["next_provider_attempt_threshold"] = attempts + self.attempt_window
            self.state["next_total_token_threshold"] = tokens + self.token_window
        elif action == "pause":
            self.state["pending_decision"] = {
                **event,
                "status": "pending",
            }
        self.state["pending_boundary_evaluation"] = None
        self._save()
        return {
            **event,
            "pause": action == "pause",
            "state_ref": self.relative_path,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "attempt_window": self.attempt_window,
            "token_window": self.token_window,
            "next_provider_attempt_threshold": self.state.get(
                "next_provider_attempt_threshold"
            ),
            "next_total_token_threshold": self.state.get("next_total_token_threshold"),
            "pending_decision": self.state.get("pending_decision"),
            "pending_boundary_evaluation": self.state.get(
                "pending_boundary_evaluation"
            ),
            "last_resolution": self.state.get("last_resolution"),
            "last_discarded_boundary": self.state.get(
                "last_discarded_boundary"
            ),
            "state_ref": self.relative_path,
        }
