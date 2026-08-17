"""Repeatable same-input benchmarks for reporting scheduler changes.

The benchmark deliberately measures orchestration rather than Provider quality.
Both modes execute the exact same task fixtures and compare every output digest
and invocation count before reporting latency.  Fake-latency executors make the
regression deterministic; Provider-backed SLA measurements remain a separate
deployment exercise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import Field

from .agentic_models import StrictModel
from .scheduling import AdaptiveTaskScheduler, SchedulingCandidate


class BenchmarkTask(StrictModel):
    task_id: str = Field(min_length=1)
    owner_key: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=50, ge=0, le=100)
    critical_path_weight: float = Field(default=1.0, gt=0)
    expected_duration_ms: int = Field(ge=1)

    def scheduling_candidate(self) -> SchedulingCandidate:
        return SchedulingCandidate(
            task_id=self.task_id,
            owner_key=self.owner_key,
            task_kind=self.task_kind,
            ordinal=self.ordinal,
            priority=self.priority,
            critical_path_weight=self.critical_path_weight,
            expected_duration_ms=self.expected_duration_ms,
        )


class BenchmarkModeMetrics(StrictModel):
    mode: Literal["serial_baseline", "adaptive_parallel"]
    repetitions: int = Field(ge=1)
    concurrency: int = Field(ge=1)
    invocation_count: int = Field(ge=0)
    run_duration_ms: list[float]
    task_duration_ms: list[float]
    run_p50_ms: float = Field(ge=0)
    run_p95_ms: float = Field(ge=0)
    task_p50_ms: float = Field(ge=0)
    task_p95_ms: float = Field(ge=0)
    first_run_dispatch_order: list[str]
    output_sha256: dict[str, str]


class ParallelBenchmarkReport(StrictModel):
    schema_version: Literal["1"] = "1"
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Literal["deterministic_orchestration_not_provider_sla"] = (
        "deterministic_orchestration_not_provider_sla"
    )
    baseline: BenchmarkModeMetrics
    optimized: BenchmarkModeMetrics
    outputs_identical: Literal[True] = True
    invocation_counts_identical: Literal[True] = True
    p50_speedup: float = Field(gt=0)
    p95_speedup: float = Field(gt=0)


BenchmarkExecutor = Callable[[BenchmarkTask], Awaitable[Any]]


def _canonical_digest(value: Any) -> str:
    if isinstance(value, bytes):
        content = value
    elif isinstance(value, str):
        content = value.encode("utf-8")
    else:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


class SameInputOrchestrationBenchmark:
    """Compare serial manifest order with adaptive bounded parallel dispatch."""

    def __init__(self, tasks: list[BenchmarkTask]) -> None:
        if not tasks:
            raise ValueError("benchmark requires at least one task")
        if len({task.task_id for task in tasks}) != len(tasks):
            raise ValueError("benchmark task ids must be unique")
        self.tasks = sorted(tasks, key=lambda item: item.ordinal)
        fixture_payload = [task.model_dump(mode="json") for task in self.tasks]
        self.fixture_sha256 = _canonical_digest(fixture_payload)

    async def _execute_one(
        self,
        task: BenchmarkTask,
        executor: BenchmarkExecutor,
    ) -> tuple[str, float]:
        started = time.perf_counter_ns()
        output = await executor(task)
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        return _canonical_digest(output), duration_ms

    async def _serial_once(
        self,
        executor: BenchmarkExecutor,
    ) -> tuple[float, list[float], dict[str, str], list[str]]:
        run_started = time.perf_counter_ns()
        outputs: dict[str, str] = {}
        task_durations: list[float] = []
        order: list[str] = []
        for task in self.tasks:
            digest, duration = await self._execute_one(task, executor)
            outputs[task.task_id] = digest
            task_durations.append(duration)
            order.append(task.task_id)
        return (
            (time.perf_counter_ns() - run_started) / 1_000_000,
            task_durations,
            outputs,
            order,
        )

    async def _parallel_once(
        self,
        executor: BenchmarkExecutor,
        concurrency: int,
    ) -> tuple[float, list[float], dict[str, str], list[str]]:
        scheduler = AdaptiveTaskScheduler(
            [task.scheduling_candidate() for task in self.tasks]
        )
        tasks_by_id = {task.task_id: task for task in self.tasks}
        outputs: dict[str, str] = {}
        task_durations: list[float] = []
        run_started = time.perf_counter_ns()

        async def worker() -> None:
            while True:
                candidate = await scheduler.next()
                if candidate is None:
                    return
                digest, duration = await self._execute_one(
                    tasks_by_id[candidate.task_id], executor
                )
                outputs[candidate.task_id] = digest
                task_durations.append(duration)

        await asyncio.gather(
            *(worker() for _ in range(min(concurrency, len(self.tasks))))
        )
        order = [decision.task_id for decision in scheduler.decisions]
        return (
            (time.perf_counter_ns() - run_started) / 1_000_000,
            task_durations,
            outputs,
            order,
        )

    @staticmethod
    def _metrics(
        mode: Literal["serial_baseline", "adaptive_parallel"],
        *,
        repetitions: int,
        concurrency: int,
        run_durations: list[float],
        task_durations: list[float],
        dispatch_order: list[str],
        outputs: dict[str, str],
        task_count: int,
    ) -> BenchmarkModeMetrics:
        return BenchmarkModeMetrics(
            mode=mode,
            repetitions=repetitions,
            concurrency=concurrency,
            invocation_count=repetitions * task_count,
            run_duration_ms=run_durations,
            task_duration_ms=task_durations,
            run_p50_ms=_percentile(run_durations, 0.50),
            run_p95_ms=_percentile(run_durations, 0.95),
            task_p50_ms=_percentile(task_durations, 0.50),
            task_p95_ms=_percentile(task_durations, 0.95),
            first_run_dispatch_order=dispatch_order,
            output_sha256=outputs,
        )

    async def compare(
        self,
        executor: BenchmarkExecutor,
        *,
        repetitions: int = 3,
        concurrency: int = 5,
    ) -> ParallelBenchmarkReport:
        if repetitions < 1:
            raise ValueError("benchmark repetitions must be positive")
        if concurrency < 1:
            raise ValueError("benchmark concurrency must be positive")
        baseline_runs: list[float] = []
        baseline_tasks: list[float] = []
        optimized_runs: list[float] = []
        optimized_tasks: list[float] = []
        baseline_outputs: dict[str, str] | None = None
        optimized_outputs: dict[str, str] | None = None
        baseline_order: list[str] = []
        optimized_order: list[str] = []

        for repetition in range(repetitions):
            run_ms, task_ms, outputs, order = await self._serial_once(executor)
            baseline_runs.append(run_ms)
            baseline_tasks.extend(task_ms)
            if repetition == 0:
                baseline_outputs = outputs
                baseline_order = order
            elif outputs != baseline_outputs:
                raise RuntimeError("serial benchmark outputs changed across repetitions")

        for repetition in range(repetitions):
            run_ms, task_ms, outputs, order = await self._parallel_once(
                executor, concurrency
            )
            optimized_runs.append(run_ms)
            optimized_tasks.extend(task_ms)
            if repetition == 0:
                optimized_outputs = outputs
                optimized_order = order
            elif outputs != optimized_outputs:
                raise RuntimeError("parallel benchmark outputs changed across repetitions")

        assert baseline_outputs is not None and optimized_outputs is not None
        if baseline_outputs != optimized_outputs:
            raise RuntimeError("parallel scheduler changed benchmark output identity")
        baseline = self._metrics(
            "serial_baseline",
            repetitions=repetitions,
            concurrency=1,
            run_durations=baseline_runs,
            task_durations=baseline_tasks,
            dispatch_order=baseline_order,
            outputs=baseline_outputs,
            task_count=len(self.tasks),
        )
        optimized = self._metrics(
            "adaptive_parallel",
            repetitions=repetitions,
            concurrency=min(concurrency, len(self.tasks)),
            run_durations=optimized_runs,
            task_durations=optimized_tasks,
            dispatch_order=optimized_order,
            outputs=optimized_outputs,
            task_count=len(self.tasks),
        )
        if baseline.invocation_count != optimized.invocation_count:
            raise RuntimeError("parallel scheduler changed invocation count")
        return ParallelBenchmarkReport(
            fixture_sha256=self.fixture_sha256,
            baseline=baseline,
            optimized=optimized,
            p50_speedup=baseline.run_p50_ms / max(optimized.run_p50_ms, 0.001),
            p95_speedup=baseline.run_p95_ms / max(optimized.run_p95_ms, 0.001),
        )
