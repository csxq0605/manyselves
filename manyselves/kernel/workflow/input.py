"""Pure application of externally supplied input to a waiting workflow state."""

from collections.abc import Mapping

from manyselves.kernel.contracts import ContractAdapter

from .models import (
    ActionExecutionStatus,
    RequestInputAction,
    ResolvedPlan,
    WorkflowState,
    WorkflowStatus,
)


class WorkflowInputError(ValueError):
    """Raised when supplied input does not match the current waiting action."""


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
