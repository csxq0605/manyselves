from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.core.reporting.benchmark import (
    BenchmarkTask,
    SameInputOrchestrationBenchmark,
)


def _tasks() -> list[BenchmarkTask]:
    durations = [15, 20, 80, 40, 25]
    return [
        BenchmarkTask(
            task_id=f"module-{index}",
            owner_key=f"owner-{index}",
            task_kind="module_lane",
            ordinal=index,
            payload={"complete_input": f"fixture-{index}"},
            expected_duration_ms=duration,
        )
        for index, duration in enumerate(durations)
    ]


@pytest.mark.asyncio
async def test_same_input_parallel_benchmark_proves_identity_and_speedup(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def execute(task: BenchmarkTask) -> dict:
        calls.append(task.task_id)
        await asyncio.sleep(task.expected_duration_ms / 1000)
        return {
            "task_id": task.task_id,
            "complete_input": task.payload["complete_input"],
            "result": "lossless",
        }

    report = await SameInputOrchestrationBenchmark(_tasks()).compare(
        execute,
        repetitions=3,
        concurrency=3,
    )
    report_path = tmp_path / "parallel-benchmark.json"
    report_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")

    assert report.outputs_identical is True
    assert report.invocation_counts_identical is True
    assert report.baseline.output_sha256 == report.optimized.output_sha256
    assert report.baseline.invocation_count == report.optimized.invocation_count == 15
    assert len(calls) == 30
    assert report.optimized.first_run_dispatch_order[0] == "module-2"
    assert report.p50_speedup > 1.5
    assert report.p95_speedup > 1.5
    assert report.optimized.run_p50_ms < report.baseline.run_p50_ms
    assert report_path.is_file()
