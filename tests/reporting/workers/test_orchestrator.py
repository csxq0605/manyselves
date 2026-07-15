import threading
import time

import pytest

from autoreport.core.reporting.models import ModuleDraft, ModuleTask
from autoreport.core.reporting.workers.orchestrator import draft_modules_parallel


class _BarrierWorker:
    def __init__(self, module_id: str, barrier: threading.Barrier):
        self.module_id = module_id
        self.barrier = barrier

    def run(self, task: ModuleTask, _evidence: list) -> ModuleDraft:
        self.barrier.wait(timeout=1)
        time.sleep(0.01 * (6 - int(task.module_id[-1])))
        return ModuleDraft(module_id=task.module_id, markdown=task.module_id, evidence_ids=[])


@pytest.mark.asyncio
async def test_draft_modules_runs_all_five_workers_concurrently_and_orders_output() -> None:
    module_ids = ["2.1", "2.2", "2.3", "2.4", "2.5"]
    barrier = threading.Barrier(5)
    tasks = [ModuleTask(id=f"module-{module_id}", module_id=module_id) for module_id in module_ids]
    workers = {module_id: _BarrierWorker(module_id, barrier) for module_id in module_ids}

    drafts, executions = await draft_modules_parallel(tasks, [], workers)

    assert [draft.module_id for draft in drafts] == module_ids
    assert [execution.module_id for execution in executions] == module_ids
    assert all(execution.elapsed_ms > 0 for execution in executions)
