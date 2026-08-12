from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.core.reporting.agent_runner import ProviderAttemptRecoveryRequired
from manyselves.core.reporting.agentic_models import SubmoduleDiscoverySubmission
from manyselves.core.reporting.workflow import AgentWorkflowError, ReportWorkflowRunner
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.store import ReportingStore


class _Service:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.store = ReportingStore(workspace)


@pytest.mark.asyncio
async def test_all_ready_leaf_scheduler_honors_cap_and_drains_entire_queue(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    leaves = tuple(
        REPORT_TAXONOMY[module_id].submodules
        for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
    )
    ready = tuple(item for group in leaves for item in group)
    active = 0
    maximum_active = 0
    completed: list[str] = []
    failed = ready[0]
    concurrency = 8
    cohort_started = asyncio.Event()

    async def execute(submodule_id: str) -> str:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == concurrency:
            cohort_started.set()
        await cohort_started.wait()
        await asyncio.sleep(0)
        active -= 1
        if submodule_id == failed:
            raise AgentWorkflowError("injected leaf failure")
        completed.append(submodule_id)
        return submodule_id

    with pytest.raises(AgentWorkflowError, match="injected leaf failure"):
        await runner._run_scheduled_submodule_stage(
            ready,
            run_id="run-all-ready",
            workflow_id="workflow-all-ready",
            task_kind="submodule_authoring",
            concurrency=concurrency,
            all_ready=True,
            execute=execute,
        )

    assert len(ready) == 37
    assert maximum_active == concurrency
    assert set(completed) == set(ready) - {failed}


@pytest.mark.asyncio
async def test_accepted_or_unknown_leaf_attempt_is_marked_and_not_replayed(
    tmp_path: Path,
) -> None:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    run_id = "run-ambiguous-leaf"
    submodule_id = "2.1.1"
    context_sha256 = "a" * 64
    state = {"run_id": run_id}
    calls = 0

    async def execute(_submodule_id: str) -> None:
        nonlocal calls
        calls += 1
        raise ProviderAttemptRecoveryRequired(["provider-call.json"])

    with pytest.raises(ProviderAttemptRecoveryRequired):
        await runner._run_scheduled_submodule_stage(
            (submodule_id,),
            run_id=run_id,
            workflow_id="workflow-ambiguous-leaf",
            task_kind="submodule_discovery",
            concurrency=1,
            all_ready=True,
            state=state,
            wave="wave-1a",
            context_sha256={submodule_id: context_sha256},
            execute=execute,
        )

    assert calls == 1
    marker_ref = state["submodule_ambiguous_task_refs"][
        f"submodule-discovery-{submodule_id}"
    ]
    marker = (tmp_path / marker_ref).read_text(encoding="utf-8")
    assert '"attempt_disposition": "accepted_or_unknown"' in marker

    with pytest.raises(AgentWorkflowError, match="accepted_or_unknown"):
        runner._load_collaboration_submission(
            run_id=run_id,
            artifact_ref=(
                f"Work/runs/{run_id}/collaboration/wave-1/submodules/{submodule_id}.json"
            ),
            task_id=f"submodule-discovery-{submodule_id}",
            module_id="2.1",
            submodule_id=submodule_id,
            expected_type=SubmoduleDiscoverySubmission,
            wave="wave-1a",
            expected_context_sha256=context_sha256,
        )
