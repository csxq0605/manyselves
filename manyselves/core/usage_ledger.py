"""Concurrency-safe per-provider-attempt usage ledger."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def _integer(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _uncached_input_tokens(row: dict[str, Any]) -> int:
    """Read the explicit field or derive it for pre-field ledger rows."""

    if "uncached_input_tokens" in row:
        return max(0, _integer(row, "uncached_input_tokens"))
    return max(
        0,
        _integer(row, "input_tokens")
        - _integer(row, "cached_input_tokens")
        - _integer(row, "cache_write_input_tokens"),
    )


class UsageLedger:
    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, workspace: Path, run_id: str):
        safe_run = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
        self.path = Path(workspace).resolve() / ".manyselves" / "usage" / f"{safe_run}.jsonl"

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(path, threading.Lock())

    def record_attempt(self, **record: Any) -> dict[str, Any]:
        row = {"timestamp": datetime.now().astimezone().isoformat(), **record}
        row.setdefault("uncached_input_tokens", _uncached_input_tokens(row))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with self._lock_for(self.path):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        return row

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock_for(self.path):
            return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def summarize(self, *, group_by: str = "stage") -> dict[str, Any]:
        """Aggregate provider cost signals without inventing provider prices."""

        if group_by not in {"stage", "task_id", "agent_id", "model", "phase"}:
            raise ValueError(
                "group_by must be one of stage, task_id, agent_id, model, or phase"
            )
        rows = self.rows()
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "total_tokens",
            "request_chars",
            "message_chars",
            "tool_schema_chars",
            "duration_ms",
            "response_tool_call_count",
        )

        def empty_bucket() -> dict[str, Any]:
            return {
                "provider_attempts": 0,
                "successful_attempts": 0,
                "failed_attempts": 0,
                "duplicate_request_attempts": 0,
                "repeated_request_chars": 0,
                "repeated_message_chars": 0,
                "repeated_tool_schema_chars": 0,
                **{field: 0 for field in fields},
            }

        totals = empty_bucket()
        groups: dict[str, dict[str, Any]] = {}
        request_counts: dict[str, int] = {}
        message_counts: dict[str, int] = {}
        tool_schema_counts: dict[str, int] = {}
        repeated_request_chars = 0
        repeated_message_chars = 0
        repeated_tool_schema_chars = 0

        for row in rows:
            group_name = str(row.get(group_by) or "unknown")
            bucket = groups.setdefault(group_name, empty_bucket())
            for target in (totals, bucket):
                target["provider_attempts"] += 1
                if str(row.get("status") or "").casefold() == "success":
                    target["successful_attempts"] += 1
                else:
                    target["failed_attempts"] += 1
                for field in fields:
                    target[field] += (
                        _uncached_input_tokens(row)
                        if field == "uncached_input_tokens"
                        else _integer(row, field)
                    )

            request_key = str(row.get("request_fingerprint") or "")
            if request_key:
                request_counts[request_key] = request_counts.get(request_key, 0) + 1
                if request_counts[request_key] > 1:
                    repeated_chars = _integer(
                        row, "message_chars"
                    ) + _integer(row, "tool_schema_chars")
                    repeated_request_chars += repeated_chars
                    bucket["duplicate_request_attempts"] += 1
                    bucket["repeated_request_chars"] += repeated_chars

            message_key = str(row.get("message_fingerprint") or "")
            if message_key:
                message_counts[message_key] = message_counts.get(message_key, 0) + 1
                if message_counts[message_key] > 1:
                    repeated_chars = _integer(row, "message_chars")
                    repeated_message_chars += repeated_chars
                    bucket["repeated_message_chars"] += repeated_chars

            tool_key = str(row.get("tool_schema_fingerprint") or "")
            if tool_key:
                tool_schema_counts[tool_key] = tool_schema_counts.get(tool_key, 0) + 1
                if tool_schema_counts[tool_key] > 1:
                    repeated_chars = _integer(row, "tool_schema_chars")
                    repeated_tool_schema_chars += repeated_chars
                    bucket["repeated_tool_schema_chars"] += repeated_chars

        totals.update(
            {
                "duplicate_request_attempts": sum(
                    max(0, count - 1) for count in request_counts.values()
                ),
                "repeated_request_chars": repeated_request_chars,
                "repeated_message_chars": repeated_message_chars,
                "repeated_tool_schema_chars": repeated_tool_schema_chars,
                "pricing_status": "unconfigured",
                "pricing_table_version": None,
                "pricing_currency": None,
                "estimated_cost": None,
            }
        )
        return {
            "group_by": group_by,
            "totals": totals,
            "groups": dict(sorted(groups.items())),
        }
