"""Pure application of externally supplied input to a waiting workflow state."""

from collections.abc import Mapping

from manyselves.kernel.contracts import ContractAdapter

from .models import (
    ActionExecutionStatus,
    ParallelAction,
    RequestInputAction,
    ResolvedPlan,
    WorkflowState,
    WorkflowStatus,
)


class WorkflowInputError(ValueError):
    """Raised when supplied input does not match the current waiting action."""


def retry_parallel_branches(
    plan: ResolvedPlan,
    state: WorkflowState,
    *,
    parallel_action_id: str,
    branch_ids: set[str],
) -> WorkflowState:
    """Return a runnable copy that preserves every unselected branch result."""

    parallel_index, parallel_action = next(
        (index, action)
        for index, action in enumerate(plan.actions)
        if action.id == parallel_action_id
    )
    if not isinstance(parallel_action, ParallelAction):
        raise WorkflowInputError(f"action is not parallel: {parallel_action_id}")

    resumed = state.model_copy(deep=True)
    results = resumed.parallel_results.get(parallel_action_id, {})
    branch_states = resumed.parallel_states.get(parallel_action_id, {})
    for branch_id in branch_ids:
        results.pop(branch_id, None)
        branch_states.pop(branch_id, None)

    for action in plan.actions[parallel_index:]:
        action_state = resumed.actions[action.id]
        action_state.status = ActionExecutionStatus.PENDING
        action_state.output = None
        action_state.error = None
        resumed.control_frames.pop(action.id, None)
        output_variable = getattr(action, "output_variable", None)
        if output_variable is not None:
            resumed.variables.pop(output_variable, None)

    resumed.status = WorkflowStatus.PENDING
    resumed.outputs.clear()
    resumed.waiting_input = None
    resumed.next_action_index = parallel_index
    resumed.next_action_id = parallel_action_id
    return resumed


def resume_waiting_input(
    plan: ResolvedPlan,
    state: WorkflowState,
    *,
    input_id: str,
    values: object,
    contracts: Mapping[str, ContractAdapter],
) -> WorkflowState:
    """Return a new runnable state containing validated external input."""

    waiting = state.waiting_input
    if state.status is not WorkflowStatus.WAITING or waiting is None:
        raise WorkflowInputError("workflow is not waiting for input")
    if waiting.get("input_id") != input_id:
        raise WorkflowInputError(f"workflow is waiting for input: {waiting.get('input_id')}")
    action = next((item for item in plan.actions if item.id == input_id), None)
    if not isinstance(action, RequestInputAction):
        raise WorkflowInputError(f"waiting action is not request_input: {input_id}")
    contract_id = waiting.get("contract_id")
    try:
        contract = contracts[contract_id]
    except KeyError as exc:
        raise WorkflowInputError(f"missing input contract adapter: {contract_id}") from exc
    validated = contract.validate(values)
    resumed = state.model_copy(deep=True)
    resumed.variables[action.output_variable] = validated
    resumed.waiting_input = None
    resumed.status = WorkflowStatus.RUNNING
    resumed.actions[action.id].status = ActionExecutionStatus.PENDING
    resumed.actions[action.id].error = None
    resumed.next_action_id = action.id
    resumed.next_action_index = next(
        index for index, item in enumerate(plan.actions) if item.id == action.id
    )
    return resumed
