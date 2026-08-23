import asyncio
from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.agentic_models import (
    ModuleReviewFindingSubmission,
    ModuleSubmission,
)
from manyselves.core.reporting.declarative_module_cohort import (
    execute_declarative_module_cohort,
)
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore
from tests.reporting.test_agent_workflow import _module
from tests.reporting.test_declarative_module_lane import _passed_validation

MODULE_IDS = tuple(REPORT_TAXONOMY)


class _CohortInvoker:
    def __init__(
        self,
        module_id: str,
        target: str,
        *,
        tracker: dict[str, Any],
        wait_for: int | None = None,
        fail: bool = False,
    ) -> None:
        self.module_id = module_id
        self.target = target
        self.tracker = tracker
        self.wait_for = wait_for
        self.fail = fail
        self.calls = 0

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        self.calls += 1
        self.tracker["active"] += 1
        self.tracker["maximum"] = max(
            self.tracker["maximum"],
            self.tracker["active"],
        )
        self.tracker["started"].append(self.module_id)
        ready = self.tracker["ready"]
        if self.wait_for is not None and len(self.tracker["started"]) == self.wait_for:
            ready.set()
        if self.wait_for is not None:
            await ready.wait()
        else:
            await asyncio.sleep(0.01)
        self.tracker["active"] -= 1
        if self.fail:
            return AgentInvocationOutcome(status="failed", error="injected lane failure")
        return AgentInvocationOutcome(
            status="ok",
            result=ModuleReviewFindingSubmission(
                coverage={"submodule_ids": [self.target]},
                findings=[],
            ),
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        assert recovery_policy.id == "current-reporting-recovery"
        return await self.invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )


def _cohort_inputs(
    tracker: dict[str, Any],
    *,
    wait_for: int | None = None,
    failed: str | None = None,
) -> tuple[
    dict[str, ModuleSubmission],
    dict[str, set[str]],
    dict[str, dict[str, _CohortInvoker]],
]:
    modules = {module_id: _module(module_id) for module_id in MODULE_IDS}
    scopes = {
        module_id: {next(iter(REPORT_TAXONOMY[module_id].submodules))}
        for module_id in MODULE_IDS
    }
    invokers = {
        module_id: {
            "evidence-auditor": _CohortInvoker(
                module_id,
                next(iter(scopes[module_id])),
                tracker=tracker,
                wait_for=wait_for,
                fail=module_id == failed,
            )
        }
        for module_id in MODULE_IDS
    }
    return modules, scopes, invokers


def _tracker() -> dict[str, Any]:
    return {
        "active": 0,
        "maximum": 0,
        "started": [],
        "ready": asyncio.Event(),
    }


@pytest.mark.asyncio
async def test_declarative_module_cohort_joins_all_five_ready_lanes(
    tmp_path: Path,
) -> None:
    tracker = _tracker()
    modules, scopes, invokers = _cohort_inputs(tracker, wait_for=len(MODULE_IDS))

    completed = await execute_declarative_module_cohort(
        run_id="run-wp08-all-ready",
        workflow_id="workflow-wp08-all-ready",
        modules=modules,
        initial_scopes=scopes,
        lane_agent_invokers=invokers,
        validate_subject=_passed_validation,
        state_store=FileWorkflowStateStore(tmp_path),
        max_concurrency=len(MODULE_IDS),
    )

    assert set(completed) == set(MODULE_IDS)
    assert tracker["maximum"] == len(MODULE_IDS)
    assert all(completed[module_id].module_id == module_id for module_id in MODULE_IDS)


@pytest.mark.asyncio
async def test_declarative_module_cohort_limits_concurrency(
    tmp_path: Path,
) -> None:
    tracker = _tracker()
    modules, scopes, invokers = _cohort_inputs(tracker)

    completed = await execute_declarative_module_cohort(
        run_id="run-wp08-limited",
        workflow_id="workflow-wp08-limited",
        modules=modules,
        initial_scopes=scopes,
        lane_agent_invokers=invokers,
        validate_subject=_passed_validation,
        state_store=FileWorkflowStateStore(tmp_path),
        max_concurrency=2,
    )

    assert set(completed) == set(MODULE_IDS)
    assert tracker["maximum"] == 2


@pytest.mark.asyncio
async def test_failed_lane_does_not_reexecute_or_erase_completed_lane(
    tmp_path: Path,
) -> None:
    run_id = "run-wp08-failure"
    state_store = FileWorkflowStateStore(tmp_path)
    tracker = _tracker()
    modules, scopes, invokers = _cohort_inputs(tracker, failed="2.2")
    completed_invoker = invokers["2.1"]["evidence-auditor"]

    with pytest.raises(RuntimeError, match="injected lane failure"):
        await execute_declarative_module_cohort(
            run_id=run_id,
            workflow_id="workflow-wp08-failure",
            modules=modules,
            initial_scopes=scopes,
            lane_agent_invokers=invokers,
            validate_subject=_passed_validation,
            state_store=state_store,
            max_concurrency=2,
        )

    assert completed_invoker.calls == 1
    cohort_state = state_store.load(run_id)
    assert cohort_state.status is WorkflowStatus.FAILED
    assert cohort_state.outputs == {}
    assert (
        cohort_state.parallel_states["module-cohort"]["2.1"]["status"]
        == WorkflowStatus.COMPLETED
    )

    retry_tracker = _tracker()
    retry_modules, retry_scopes, retry_invokers = _cohort_inputs(retry_tracker)
    completed = await execute_declarative_module_cohort(
        run_id=run_id,
        workflow_id="workflow-wp08-failure",
        modules=retry_modules,
        initial_scopes=retry_scopes,
        lane_agent_invokers=retry_invokers,
        validate_subject=_passed_validation,
        state_store=state_store,
        max_concurrency=2,
    )

    assert set(completed) == set(MODULE_IDS)
    assert completed_invoker.calls == 1
    assert retry_invokers["2.1"]["evidence-auditor"].calls == 0
    assert retry_invokers["2.2"]["evidence-auditor"].calls == 1
    assert all(
        retry_invokers[module_id]["evidence-auditor"].calls == 0
        for module_id in MODULE_IDS
        if module_id != "2.2"
    )
    recovered_state = state_store.load(run_id)
    assert recovered_state.status is WorkflowStatus.COMPLETED
    assert set(recovered_state.parallel_states["module-cohort"]) == set(MODULE_IDS)
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        run_id
    ]
