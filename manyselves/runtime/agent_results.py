"""Durable typed Agent results for one Run.

One file per (conversation, task action).  It records the validated input the
result was produced for, so a Same-run resume reuses a result only for the
same input instead of trusting a file name.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

AgentResultStatus = Literal["completed", "failed", "blocked"]


def _safe_component(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_" for char in str(value)
    ) or "_"


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class FileAgentResultStore:
    """Persist and look up typed Agent results under the Run directory."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace)
        self.run_id = run_id
        self.root = (
            self.workspace / "Work" / "runs" / _safe_component(run_id) / "agent-results"
        )

    def path(self, conversation_id: str, task_id: str) -> Path:
        return (
            self.root
            / _safe_component(conversation_id)
            / f"{_safe_component(task_id)}.json"
        )

    def relative_path(self, conversation_id: str, task_id: str) -> str:
        return self.path(conversation_id, task_id).relative_to(self.workspace).as_posix()

    def save(
        self,
        *,
        conversation_id: str,
        task_id: str,
        agent_id: str,
        input_value: Any,
        status: AgentResultStatus,
        result: Any = None,
        reason: str | None = None,
    ) -> str:
        path = self.path(conversation_id, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "status": status,
                    "input": _json_value(input_value),
                    "result": _json_value(result),
                    "reason": reason,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return self.relative_path(conversation_id, task_id)

    def load(self, result_ref: str) -> dict[str, Any]:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("run_id") != self.run_id:
            raise ValueError(f"agent result does not belong to run {self.run_id}")
        return dict(payload)

    def completed_for(
        self,
        *,
        conversation_id: str,
        task_id: str,
        input_value: Any,
    ) -> dict[str, Any] | None:
        """Return the completed record produced for exactly this input, if any."""

        path = self.path(conversation_id, task_id)
        if not path.is_file():
            return None
        try:
            payload = self.load(str(path))
        except (OSError, ValueError):
            return None
        if payload.get("status") != "completed":
            return None
        if payload.get("input") != _json_value(input_value):
            return None
        return payload


__all__ = ["AgentResultStatus", "FileAgentResultStore"]
