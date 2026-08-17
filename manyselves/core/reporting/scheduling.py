"""Deterministic, history-informed scheduling for authorized reporting tasks.

Scheduling here changes dispatch order only.  It does not reject work, reserve
tokens, rate-limit providers, or remove task inputs.  Long expected critical
paths are started first to reduce cohort makespan, while stable ordinal and
owner dispatch counts keep decisions reproducible and fair.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SchedulingCandidate(_StrictModel):
    task_id: str = Field(min_length=1)
    owner_key: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    priority: int = Field(default=50, ge=0, le=100)
    critical_path_weight: float = Field(default=1.0, gt=0)
    expected_duration_ms: int = Field(default=60_000, ge=1)


class SchedulingDecision(_StrictModel):
    sequence: int = Field(ge=1)
    task_id: str
    owner_key: str
    task_kind: str
    priority: int
    critical_path_score: float
    expected_duration_ms: int
    decided_at_ns: int = Field(ge=1)


class AdaptiveTaskScheduler:
    """Concurrency-safe longest-critical-path-first scheduler with stable ties."""

    def __init__(self, candidates: list[SchedulingCandidate]) -> None:
        if len({candidate.task_id for candidate in candidates}) != len(candidates):
            raise ValueError("scheduling candidates require unique task ids")
        self._pending = {candidate.task_id: candidate for candidate in candidates}
        self._owner_dispatches: Counter[str] = Counter()
        self._lock = asyncio.Lock()
        self._sequence = 0
        self.decisions: list[SchedulingDecision] = []

    @staticmethod
    def _rank(
        candidate: SchedulingCandidate,
        owner_dispatches: Counter[str],
    ) -> tuple[float, ...]:
        return (
            float(candidate.priority),
            candidate.expected_duration_ms * candidate.critical_path_weight,
            -float(owner_dispatches[candidate.owner_key]),
            -float(candidate.ordinal),
        )

    async def next(self) -> SchedulingCandidate | None:
        async with self._lock:
            if not self._pending:
                return None
            candidate = max(
                self._pending.values(),
                key=lambda item: self._rank(item, self._owner_dispatches),
            )
            self._pending.pop(candidate.task_id)
            self._owner_dispatches[candidate.owner_key] += 1
            self._sequence += 1
            self.decisions.append(
                SchedulingDecision(
                    sequence=self._sequence,
                    task_id=candidate.task_id,
                    owner_key=candidate.owner_key,
                    task_kind=candidate.task_kind,
                    priority=candidate.priority,
                    critical_path_score=(
                        candidate.expected_duration_ms
                        * candidate.critical_path_weight
                    ),
                    expected_duration_ms=candidate.expected_duration_ms,
                    decided_at_ns=time.time_ns(),
                )
            )
            return candidate

    async def pending_count(self) -> int:
        async with self._lock:
            return len(self._pending)


class TaskTimingHistory:
    """Cross-process append-only timing history used only for scheduling hints."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / ".manyselves" / "runtime" / "task-timings.jsonl"
        self.lock_path = self.path.with_suffix(".lock")

    def record(
        self,
        *,
        run_id: str,
        task_id: str,
        task_kind: str,
        owner_key: str,
        duration_ms: int,
        status: Literal["completed", "failed", "deferred"],
    ) -> dict:
        row = {
            "recorded_at_ns": time.time_ns(),
            "run_id": run_id,
            "task_id": task_id,
            "task_kind": task_kind,
            "owner_key": owner_key,
            "duration_ms": max(0, int(duration_ms)),
            "status": status,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    handle.flush()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return row

    def rows(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                return [
                    json.loads(line)
                    for line in self.path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def estimate_ms(
        self,
        task_kind: str,
        owner_key: str,
        *,
        default: int = 60_000,
        sample_limit: int = 12,
    ) -> int:
        samples = [
            int(row.get("duration_ms", 0) or 0)
            for row in self.rows()
            if row.get("task_kind") == task_kind
            and row.get("owner_key") == owner_key
            and row.get("status") == "completed"
            and int(row.get("duration_ms", 0) or 0) > 0
        ][-sample_limit:]
        if not samples:
            return max(1, int(default))
        # Recency-weighted mean: newer observations receive larger weights.
        weights = range(1, len(samples) + 1)
        return max(
            1,
            round(
                sum(value * weight for value, weight in zip(samples, weights))
                / sum(weights)
            ),
        )
