"""Capability-owned durable progress observation for Reporting continuations.

This is the existing Reporting snapshot semantics moved out of the Legacy
runner.  It observes only durable artifacts, result-part metadata, and unique
semantic conversation events.  Recovery decisions remain in the neutral
Runtime and Kernel recovery controller.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from manyselves.kernel.recovery import RecoveryEventKind
from manyselves.runtime.agent_execution import AgentSessionLoop

from .models.agentic import TaskEnvelope
from .storage import ReportingStore


def continuation_conversation_event_digests(loop: AgentSessionLoop) -> set[str]:
    """Hash unique semantic events while ignoring harness-owned prompts."""

    def normalized_content(value: Any) -> str:
        content = str(value or "")
        if (
            "<same_identity_continuation>" in content
            or "<submission_correction>" in content
            or "<progress_check>" in content
            or "<working_memory_checkpoint>" in content
        ):
            return ""
        marker = (
            "[Provider output reached the per-request max_tokens limit "
            "before a typed tool submission. The task is not complete.]"
        )
        content = content.replace(marker, "").strip()
        if not content:
            return ""
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return content
        if isinstance(parsed, dict) and parsed.get("truncated") is True:
            parsed = dict(parsed)
            parsed.pop("full_result_ref", None)
            return json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        return content

    digests: set[str] = set()
    for message in getattr(loop, "_conversation_history", ()):
        tool_calls = []
        for call in getattr(message, "tool_calls", None) or ():
            tool_calls.append(
                {
                    "name": str(getattr(call, "name", "") or ""),
                    "arguments": dict(getattr(call, "arguments", {}) or {}),
                }
            )
        event = {
            "role": str(getattr(message, "role", "") or ""),
            "content": normalized_content(getattr(message, "content", "")),
            "is_tool_result": bool(getattr(message, "is_tool_result", False)),
            "tool_calls": tool_calls,
        }
        if not event["content"] and not tool_calls:
            continue
        serialized = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digests.add(hashlib.sha256(serialized.encode("utf-8")).hexdigest())
    return digests


async def continuation_progress_snapshot(
    workspace: Path,
    loop: AgentSessionLoop,
    envelope: TaskEnvelope,
) -> dict[str, Any]:
    """Return the existing content-free durable/conversation snapshot."""

    workspace = Path(workspace).resolve()
    durable: dict[str, str] = {}
    run_root = workspace / "Work" / "runs" / envelope.run_id
    durable_roots = (
        run_root / "drafts" / envelope.task_id / f"r{envelope.revision}",
        run_root / "results" / "attempts" / envelope.task_id,
    )
    for root in durable_roots:
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(workspace).as_posix()
            durable[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical_result = run_root / "results" / f"{envelope.task_id}.json"
    if canonical_result.is_file():
        relative = canonical_result.relative_to(workspace).as_posix()
        durable[relative] = hashlib.sha256(canonical_result.read_bytes()).hexdigest()

    result_parts: dict[str, Any] | None = None
    tools = getattr(loop, "tools", None)
    list_parts = tools.get("list_result_parts") if tools is not None else None
    if list_parts is not None:
        try:
            listed = await list_parts()
        except Exception as exc:  # pragma: no cover - existing telemetry behavior
            result_parts = {"status": "unreadable", "error_type": type(exc).__name__}
        else:
            result_parts = {
                "complete": bool(listed.get("complete", False)),
                "ready_part_ids": sorted(listed.get("ready_part_ids", ())),
                "missing_part_ids": sorted(listed.get("missing_part_ids", ())),
                "rewrite_part_ids": sorted(listed.get("rewrite_part_ids", ())),
                "parts": sorted(
                    (
                        str(item.get("part_id", "")),
                        int(item.get("characters", 0)),
                        bool(item.get("ready", False)),
                    )
                    for item in listed.get("parts", ())
                ),
            }

    durable_payload = json.dumps(
        {"files": durable, "result_parts": result_parts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "durable_sha256": hashlib.sha256(
            durable_payload.encode("utf-8")
        ).hexdigest(),
        "durable_file_count": len(durable),
        "result_parts": result_parts,
        "conversation_event_sha256": sorted(
            continuation_conversation_event_digests(loop)
        ),
    }


class ReportingContinuationProgressObserver:
    """Persist and compare the existing progress baseline for one typed attempt."""

    def __init__(self, workspace: Path, envelope: TaskEnvelope) -> None:
        self.workspace = Path(workspace).resolve()
        self.envelope = envelope
        safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", envelope.task_id)
        safe_attempt_id = re.sub(
            r"[^A-Za-z0-9_.-]", "_", envelope.task_attempt_id
        )
        self.state_ref = (
            f"Work/runs/{envelope.run_id}/continuations/{safe_task_id}/"
            f"{safe_attempt_id}.json"
        )
        self._path = self.workspace / self.state_ref
        self._store = ReportingStore(self.workspace)

    def _load(self) -> dict[str, Any]:
        if self._path.is_file():
            return json.loads(self._path.read_text(encoding="utf-8"))
        return {
            "kind": "reporting_continuation_harness_state",
            "version": 1,
            "run_id": self.envelope.run_id,
            "task_id": self.envelope.task_id,
            "task_attempt_id": self.envelope.task_attempt_id,
            "revision": self.envelope.revision,
            "no_progress_observations": 0,
            "observed_conversation_event_sha256": [],
            "last_durable_sha256": None,
            "status": "active",
            "events": [],
        }

    async def observe(
        self,
        loop: AgentSessionLoop,
        event_kind: RecoveryEventKind,
        detail: dict[str, Any],
    ) -> Literal["progressed", "no_progress"]:
        """Record one existing progress comparison and return its typed result."""

        state = self._load()
        snapshot = await continuation_progress_snapshot(
            self.workspace,
            loop,
            self.envelope,
        )
        observed = set(state.get("observed_conversation_event_sha256", ()))
        current_events = set(snapshot["conversation_event_sha256"])
        previous_durable = state.get("last_durable_sha256")
        first_observation = previous_durable is None
        durable_changed = (
            not first_observation
            and previous_durable != snapshot["durable_sha256"]
        )
        new_events = current_events - observed
        progressed = first_observation or durable_changed or bool(new_events)
        if progressed:
            state["no_progress_observations"] = 0
        else:
            state["no_progress_observations"] = (
                int(state.get("no_progress_observations", 0)) + 1
            )
        state["last_durable_sha256"] = snapshot["durable_sha256"]
        state["observed_conversation_event_sha256"] = sorted(
            observed | current_events
        )
        state.setdefault("events", []).append(
            {
                "sequence": len(state.get("events", ())) + 1,
                "event_kind": event_kind.value,
                "progressed": progressed,
                "new_conversation_event_count": len(new_events),
                "durable_changed": durable_changed,
                "durable_file_count": snapshot["durable_file_count"],
                "result_parts": snapshot["result_parts"],
                "detail": detail,
            }
        )
        state["status"] = "progressed" if progressed else "no_progress"
        self._store.write_json(self.state_ref, state)
        return "progressed" if progressed else "no_progress"


__all__ = [
    "ReportingContinuationProgressObserver",
    "continuation_conversation_event_digests",
    "continuation_progress_snapshot",
]
