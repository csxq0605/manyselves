#!/usr/bin/env python3
"""Run the deterministic same-input reporting orchestration benchmark."""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from manyselves.core.reporting.benchmark import (
    BenchmarkTask,
    SameInputOrchestrationBenchmark,
)


def fixture() -> list[BenchmarkTask]:
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


async def execute(task: BenchmarkTask) -> dict[str, str]:
    await asyncio.sleep(task.expected_duration_ms / 1000)
    return {
        "task_id": task.task_id,
        "complete_input": str(task.payload["complete_input"]),
        "result": "lossless",
    }


async def run(repetitions: int, concurrency: int) -> str:
    report = await SameInputOrchestrationBenchmark(fixture()).compare(
        execute,
        repetitions=repetitions,
        concurrency=concurrency,
    )
    return report.model_dump_json(indent=2) + "\n"


def atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    content = asyncio.run(run(args.repetitions, args.concurrency))
    if args.output is None:
        print(content, end="")
    else:
        atomic_write(args.output, content)
        print(args.output.resolve())


if __name__ == "__main__":
    main()
