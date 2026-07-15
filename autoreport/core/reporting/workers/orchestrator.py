"""Concurrent module drafting with deterministic result ordering."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

from ..models import EvidenceItem, ModuleDraft, ModuleTask, ReportingModel


class ModuleWorker(Protocol):
    def run(self, task: ModuleTask, evidence_items: list[EvidenceItem]) -> ModuleDraft: ...


class ModuleExecution(ReportingModel):
    module_id: str
    started_at: str
    finished_at: str
    elapsed_ms: float
    worker: str


def _run_one(
    task: ModuleTask,
    evidence_items: list[EvidenceItem],
    worker: ModuleWorker,
) -> tuple[ModuleDraft, ModuleExecution]:
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    draft = worker.run(task, evidence_items)
    finished_at = datetime.now(UTC)
    execution = ModuleExecution(
        module_id=task.module_id,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        worker=type(worker).__name__,
    )
    return draft, execution


async def draft_modules_parallel(
    tasks: list[ModuleTask],
    evidence_items: list[EvidenceItem],
    workers: dict[str, ModuleWorker],
) -> tuple[list[ModuleDraft], list[ModuleExecution]]:
    """Run independent module workers in threads and return fixed taxonomy order."""

    pending = []
    for task in tasks:
        try:
            worker = workers[task.module_id]
        except KeyError as exc:
            raise ValueError(f"no worker configured for module {task.module_id}") from exc
        pending.append(asyncio.to_thread(_run_one, task, evidence_items, worker))
    results = await asyncio.gather(*pending)
    results.sort(key=lambda result: result[0].module_id)
    return (
        [draft for draft, _execution in results],
        [execution for _draft, execution in results],
    )
