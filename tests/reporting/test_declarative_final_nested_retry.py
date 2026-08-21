from __future__ import annotations

import pytest

from manyselves.core.reporting.declarative_final_chapter_cohort import (
    compile_final_chapter_workflows,
    retry_failed_final_chapter_lanes,
)
from manyselves.core.reporting.declarative_final_review_cycle import (
    compile_final_review_workflows,
)
from manyselves.core.reporting.declarative_reporting_tail import (
    build_reporting_tail_definition,
)
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    WorkflowState,
    WorkflowStatus,
)


@pytest.mark.parametrize(
    ("nested_action_id", "cohort_workflow_id", "parallel_action_id", "outcome_prefix"),
    [
        (
            "run-final-chief-revision-cohort",
            "distribution-final-chief-revision-cohort",
            "final-chief-revision-cohort",
            "revision-outcome",
        ),
        (
            "run-final-recheck-cohort",
            "distribution-final-recheck-cohort",
            "final-recheck-cohort",
            "recheck-outcome",
        ),
    ],
)
def test_retry_nested_final_review_preserves_completed_siblings(
    nested_action_id: str,
    cohort_workflow_id: str,
    parallel_action_id: str,
    outcome_prefix: str,
) -> None:
    definitions, _contracts, _tail = build_reporting_tail_definition()
    executors = build_builtin_executor_registry()
    outer_plan, _lane_plans = compile_final_chapter_workflows(definitions, executors)
    review_plans = compile_final_review_workflows(definitions, executors)
    cycle_plan = review_plans["distribution-final-review-cycle"]
    cohort_plan = review_plans[cohort_workflow_id]

    outer = WorkflowState.for_plan("run-nested-final-retry", outer_plan)
    outer.status = WorkflowStatus.FAILED
    outer.actions["run-final-review-cycle"].status = ActionExecutionStatus.FAILED
    outer.variables["completed-final-state"] = {"subject": "r0"}

    cycle = WorkflowState.for_plan("run-nested-final-retry", cycle_plan)
    cycle.status = WorkflowStatus.FAILED
    cycle.actions[nested_action_id].status = ActionExecutionStatus.FAILED
    cycle.variables["review"] = {"revision_number": 1}

    cohort = WorkflowState.for_plan("run-nested-final-retry", cohort_plan)
    cohort.status = WorkflowStatus.FAILED
    cohort.actions[f"reduce-{parallel_action_id}"].status = ActionExecutionStatus.FAILED
    cohort.variables["review"] = {"revision_number": 1}
    cohort.parallel_results[parallel_action_id] = {
        branch_id: {
            f"{outcome_prefix}-{branch_id}": {
                "status": "failed" if branch_id == "3" else "completed",
            }
        }
        for branch_id in ("1", "3", "4")
    }
    cohort.parallel_states[parallel_action_id] = {}
    for branch_id in ("1", "3", "4"):
        branch = WorkflowState.for_plan("run-nested-final-retry", cohort_plan)
        branch.status = WorkflowStatus.COMPLETED
        cohort.parallel_states[parallel_action_id][branch_id] = branch.model_dump(mode="json")

    cycle.subworkflow_states[nested_action_id] = cohort.model_dump(mode="json")
    outer.subworkflow_states["run-final-review-cycle"] = cycle.model_dump(mode="json")

    resumed = retry_failed_final_chapter_lanes(outer_plan, outer)

    assert resumed.status is WorkflowStatus.PENDING
    assert resumed.next_action_id == "run-final-review-cycle"
    assert resumed.actions["run-final-review-cycle"].status is ActionExecutionStatus.PENDING

    resumed_cycle = WorkflowState.model_validate(
        resumed.subworkflow_states["run-final-review-cycle"]
    )
    assert resumed_cycle.status is WorkflowStatus.PENDING
    assert resumed_cycle.next_action_id == nested_action_id
    assert resumed_cycle.actions[nested_action_id].status is ActionExecutionStatus.PENDING
    assert resumed_cycle.variables["review"] == {"revision_number": 1}

    resumed_cohort = WorkflowState.model_validate(
        resumed_cycle.subworkflow_states[nested_action_id]
    )
    assert resumed_cohort.status is WorkflowStatus.PENDING
    assert resumed_cohort.next_action_id == parallel_action_id
    assert set(resumed_cohort.parallel_results[parallel_action_id]) == {"1", "4"}
    assert set(resumed_cohort.parallel_states[parallel_action_id]) == {"1", "4"}
    assert all(
        WorkflowState.model_validate(
            resumed_cohort.parallel_states[parallel_action_id][branch_id]
        ).status
        is WorkflowStatus.COMPLETED
        for branch_id in ("1", "4")
    )
    assert resumed_cohort.variables["review"] == {"revision_number": 1}
