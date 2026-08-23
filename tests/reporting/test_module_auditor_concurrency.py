from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ModuleSubmission
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.reporting.workflow import AgentWorkflowError, ReportWorkflowRunner

MODULES = ("2.1", "2.2", "2.3", "2.4", "2.5")


class _Service:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.store = ReportingStore(workspace)


def _runner(tmp_path: Path, run_id: str) -> tuple[ReportWorkflowRunner, dict]:
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = _Service(tmp_path)
    runner._budget = None
    runner._checkpoint = lambda *_args, **_kwargs: None
    runner._bind_reviewed_module_to_authoring_context = lambda *_args, **_kwargs: None
    refs = {
        "coverage": f"Work/runs/{run_id}/coverage.json",
        "evidence": f"Work/runs/{run_id}/evidence.json",
        "manifest": f"Work/runs/{run_id}/manifest.json",
    }
    for ref in refs.values():
        runner.service.store.write_json(ref, {"ref": ref})
    state = {
        "run_id": run_id,
        "request": type("Request", (), {"execution_requirements": [], "missing_evidence_policy": "draft", "user_supplements": []})(),
        "preparation_refs": refs,
        "module_knowledge_refs": {},
        "module_submissions": {},
        "specialist_submissions": {},
        "module_review_completion_refs": {},
    }
    return runner, state


def _submission(runner: ReportWorkflowRunner, lane_state: dict, module_id: str) -> ModuleSubmission:
    run_id = lane_state["run_id"]
    submission = ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"{submodule_id} module body"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r0.json"
    review_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/{module_id}/completion-r0.json"
    )
    runner.service.store.write_json(subject_ref, submission.model_dump(mode="json"))
    runner.service.store.write_json(
        review_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key=f"module-auditor-{module_id}",
            subject_refs=[subject_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    lane_state.setdefault("module_submissions", {})[module_id] = submission
    lane_state.setdefault("specialist_submissions", {})[module_id] = submission
    lane_state.setdefault("module_review_completion_refs", {})[module_id] = review_ref
    return submission


@pytest.mark.asyncio
async def test_all_ready_module_lanes_admit_every_ready_module(tmp_path: Path) -> None:
    runner, state = _runner(tmp_path, "run-module-all-ready")
    active = 0
    maximum_active = 0
    started: list[str] = []
    cohort_ready = asyncio.Event()

    async def pipeline(module_id, lane_state, _workflow_id, **_kwargs):
        nonlocal active, maximum_active
        started.append(module_id)
        active += 1
        maximum_active = max(maximum_active, active)
        if len(started) == len(MODULES):
            cohort_ready.set()
        await cohort_ready.wait()
        active -= 1
        return _submission(runner, lane_state, module_id)

    runner._module_pipeline = pipeline
    await runner._run_module_lanes(
        MODULES,
        state,
        "workflow-module-all-ready",
    )

    assert maximum_active == len(MODULES)
    assert set(started) == set(MODULES)
    assert set(state["module_submissions"]) == set(MODULES)
    barrier = json.loads(
        (tmp_path / "Work/runs/run-module-all-ready/lanes/module-barrier.json").read_text()
    )
    assert barrier["scope"] == "full"
    assert barrier["target_modules"] == list(MODULES)


@pytest.mark.asyncio
async def test_module_lane_failure_drains_siblings_and_writes_terminal_barrier(
    tmp_path: Path,
) -> None:
    runner, state = _runner(tmp_path, "run-module-failure")
    started: list[str] = []
    completed: list[str] = []
    cohort_ready = asyncio.Event()
    failed = "2.2"

    async def pipeline(module_id, lane_state, _workflow_id, **_kwargs):
        started.append(module_id)
        if len(started) == len(MODULES):
            cohort_ready.set()
        await cohort_ready.wait()
        if module_id == failed:
            raise AgentWorkflowError("injected module lane failure")
        completed.append(module_id)
        return _submission(runner, lane_state, module_id)

    runner._module_pipeline = pipeline
    with pytest.raises(AgentWorkflowError, match="injected module lane failure"):
        await runner._run_module_lanes(
            MODULES,
            state,
            "workflow-module-failure",
        )

    assert set(started) == set(MODULES)
    assert set(completed) == set(MODULES) - {failed}
    barrier = json.loads(
        (tmp_path / "Work/runs/run-module-failure/lanes/module-barrier.json").read_text()
    )
    assert barrier["status"] == "failed"
    assert barrier["terminal_statuses"][failed] == "failed"
    assert set(barrier["completion_refs"]) == set(MODULES) - {failed}
    assert set(state["module_submissions"]) == set(MODULES) - {failed}
