"""Run-scoped, content-addressed memory for completed local tool results.

This is intentionally independent from ``AgentLoop``.  Callers can consult
the index before executing a tool and record a result afterwards; the index is
bounded to one run and never turns an ambiguous Provider call into an implicit
replay.  Writes replace the complete JSONL file atomically so a crash leaves a
valid previous snapshot or a complete new one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def canonical_tool_arguments(arguments: Any) -> str:
    """Serialize arguments deterministically for a local dedupe fingerprint."""

    try:
        return json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return json.dumps(str(arguments), ensure_ascii=False, separators=(",", ":"))


def tool_result_fingerprint(
    run_id: str,
    task_id: str,
    tool_name: str,
    arguments: Any = None,
    *,
    ref: str | None = None,
) -> str:
    """Return the SHA-256 key for one run/task/tool/argument identity."""

    payload = {
        "run_id": str(run_id),
        "task_id": str(task_id),
        "tool_name": str(tool_name),
        "arguments": arguments,
        "ref": ref,
    }
    return hashlib.sha256(canonical_tool_arguments(payload).encode("utf-8")).hexdigest()


class RunToolResultIndex:
    """A small durable index of local tool results for one run."""

    def __init__(
        self,
        root: Path,
        run_id: str | None = None,
        *,
        filename: str = "tool-result-index.jsonl",
    ) -> None:
        self.root = Path(root).resolve()
        self.run_id = str(run_id) if run_id is not None else None
        if self.run_id is not None:
            if not self.run_id or "/" in self.run_id or "\\" in self.run_id or self.run_id in {".", ".."}:
                raise ValueError("run_id must be a simple run directory name")
            # Accept either a workspace root or an already-selected run dir.
            if self.root.name == self.run_id:
                self.path = self.root / filename
            else:
                self.path = self.root / "Work" / "runs" / self.run_id / filename
        else:
            self.path = self.root / filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def fingerprint(
        run_id: str,
        task_id: str,
        tool_name: str,
        arguments: Any = None,
        *,
        ref: str | None = None,
    ) -> str:
        return tool_result_fingerprint(run_id, task_id, tool_name, arguments, ref=ref)

    def key(
        self,
        *parts: Any,
        run_id: str | None = None,
        ref: str | None = None,
        task_id: str | None = None,
        tool_name: str | None = None,
        arguments: Any = None,
    ) -> str:
        # Accept both a run-bound form ``key(task, tool, args)`` and the
        # explicit identity form ``key(run, task, tool, args)``.
        if parts:
            if len(parts) == 4:
                run_id, task_id, tool_name, arguments = parts
            elif len(parts) == 3:
                task_id, tool_name, arguments = parts
            elif len(parts) == 2:
                task_id, tool_name = parts
            else:
                raise TypeError("key expects task/tool/arguments or run/task/tool/arguments")
        if task_id is None or tool_name is None:
            raise TypeError("task_id and tool_name are required")
        effective_run = str(run_id if run_id is not None else self.run_id or "")
        if not effective_run:
            raise ValueError("run_id is required for a result key")
        return tool_result_fingerprint(effective_run, task_id, tool_name, arguments, ref=ref)

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A torn legacy append is ignored; future atomic writes
                    # replace the file with the valid in-memory snapshot.
                    continue
                if isinstance(entry, dict) and entry.get("key"):
                    self._entries[str(entry["key"])] = entry

    def _write_atomic(self) -> None:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for entry in self._entries.values():
                    handle.write(
                        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def lookup(
        self,
        *parts: Any,
        task_id: str | None = None,
        tool_name: str | None = None,
        arguments: Any = None,
        run_id: str | None = None,
        ref: str | None = None,
    ) -> dict[str, Any] | None:
        key = self.key(*parts, task_id=task_id, tool_name=tool_name, arguments=arguments, run_id=run_id, ref=ref)
        with self._lock:
            value = self._entries.get(key)
            return dict(value) if value is not None else None

    get = lookup

    def contains(
        self,
        *parts: Any,
        task_id: str | None = None,
        tool_name: str | None = None,
        arguments: Any = None,
        run_id: str | None = None,
        ref: str | None = None,
    ) -> bool:
        return self.lookup(*parts, task_id=task_id, tool_name=tool_name, arguments=arguments, run_id=run_id, ref=ref) is not None

    seen = contains

    def record(
        self,
        *parts: Any,
        task_id: str | None = None,
        tool_name: str | None = None,
        arguments: Any = None,
        result: Any = None,
        run_id: str | None = None,
        ref: str | None = None,
        status: str = "completed",
        artifact_refs: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Record one result, returning the existing entry on duplicate."""

        # As with ``key``, support explicit ``(run, task, tool, args, result)``
        # calls in addition to the run-bound keyword-friendly form.
        if parts:
            if len(parts) == 5:
                run_id, task_id, tool_name, arguments, result = parts
            elif len(parts) == 4:
                task_id, tool_name, arguments, result = parts
            elif len(parts) == 3:
                task_id, tool_name, result = parts
            else:
                raise TypeError("record expects task/tool/arguments/result or run/task/tool/arguments/result")
        if task_id is None or tool_name is None:
            raise TypeError("task_id and tool_name are required")
        effective_run = str(run_id if run_id is not None else self.run_id or "")
        if not effective_run:
            raise ValueError("run_id is required for a result record")
        key = self.key(task_id, tool_name, arguments, run_id=effective_run, ref=ref)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                duplicate = dict(existing)
                duplicate["deduplicated"] = True
                return duplicate
            entry: dict[str, Any] = {
                "key": key,
                "run_id": effective_run,
                "task_id": str(task_id),
                "tool_name": str(tool_name),
                "arguments": arguments,
                "ref": ref,
                "status": str(status),
                "result": result,
                "artifact_refs": list(artifact_refs or ()),
                "deduplicated": False,
            }
            # JSON serialization is the contract boundary.  Fail before
            # mutating the in-memory index if a caller gave an unserializable
            # result rather than silently dropping it.
            json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            self._entries[key] = entry
            self._write_atomic()
            return dict(entry)

    put = record
    remember = record

    def should_execute(
        self,
        task_id: str,
        tool_name: str,
        arguments: Any = None,
        *,
        run_id: str | None = None,
        ref: str | None = None,
    ) -> bool:
        """Return false only for a previously completed local result."""

        existing = self.lookup(task_id, tool_name, arguments, run_id=run_id, ref=ref)
        return existing is None or str(existing.get("status", "completed")) != "completed"

    def entries(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(entry) for entry in self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


# Common short names used by callers that treat this as result memory rather
# than an index implementation detail.
ResultMemory = RunToolResultIndex
ToolResultMemory = RunToolResultIndex
RunResultMemory = RunToolResultIndex
ToolResultIndex = RunToolResultIndex
canonical_args = canonical_tool_arguments
result_fingerprint = tool_result_fingerprint


__all__ = [
    "ResultMemory",
    "RunResultMemory",
    "RunToolResultIndex",
    "ToolResultIndex",
    "ToolResultMemory",
    "canonical_args",
    "canonical_tool_arguments",
    "result_fingerprint",
    "tool_result_fingerprint",
]
