"""Pure workflow state transitions and external effect requests."""

from dataclasses import dataclass
from typing import Any

from .models import (
    ActionExecutionStatus,
    ResolvedPlan,
    WorkflowState,
    WorkflowStatus,
)


@dataclass(frozen=True, slots=True)
class StartWorkflow:
    """Request scheduling from the current persisted state."""


@dataclass(frozen=True, slots=True)
class ActionSucceeded:
    """Return one externally executed Action result to the Kernel."""

    action_id: str
    result: Any


@dataclass(frozen=True, slots=True)
class ActionFailed:
    """Return one externally executed Action failure to the Kernel."""

    action_id: str
    error: str


WorkflowEvent = StartWorkflow | ActionSucceeded | ActionFailed


@dataclass(frozen=True, slots=True)
class ExecuteAction:
    """Request that the Runtime Host execute one compiled Action."""

    action_id: str


@dataclass(frozen=True, slots=True)
class WorkflowTransition:
    """A new authoritative state plus effects for the Runtime Host."""

    state: WorkflowState
    effects: tuple[ExecuteAction, ...] = ()


class StatelessWorkflowKernel:
    """Reduce Plan + State + Event without persistence or external calls."""

    def transition(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        event: WorkflowEvent,
    ) -> WorkflowTransition:
        next_state = state.model_copy(deep=True)
        if isinstance(event, StartWorkflow):
            if next_state.status is WorkflowStatus.COMPLETED:
                return WorkflowTransition(next_state)
            next_state.status = WorkflowStatus.RUNNING
            return self._schedule(plan, next_state, next_state.next_action_index)
        if isinstance(event, ActionFailed):
            action_state = next_state.actions[event.action_id]
            action_state.status = ActionExecutionStatus.FAILED
            action_state.error = event.error
            next_state.status = WorkflowStatus.FAILED
            return WorkflowTransition(next_state)

        action_state = next_state.actions[event.action_id]
        result = event.result
        next_state.variables.update(result.variable_updates)
        next_state.outputs.update(result.output_updates)
        next_state.conversations.update(result.conversation_updates)
        if result.clear_waiting_input:
            next_state.waiting_input = None
        if result.waiting_input is not None:
            next_state.waiting_input = result.waiting_input
        action_state.output = result.output
        action_state.error = None
        if result.workflow_status is not None:
            next_state.status = result.workflow_status
        if next_state.status is WorkflowStatus.WAITING:
            action_state.status = ActionExecutionStatus.WAITING
            return WorkflowTransition(next_state)

        action_state.status = ActionExecutionStatus.COMPLETED
        current_index = next(
            index for index, action in enumerate(plan.actions) if action.id == event.action_id
        )
        next_state.next_action_index = current_index + 1
        if next_state.status is WorkflowStatus.COMPLETED:
            next_state.next_action_id = None
            return WorkflowTransition(next_state)
        next_state.status = WorkflowStatus.RUNNING
        return self._schedule(plan, next_state, current_index + 1)

    @staticmethod
    def _schedule(
        plan: ResolvedPlan,
        state: WorkflowState,
        start_index: int,
    ) -> WorkflowTransition:
        index = start_index
        while index < len(plan.actions):
            action = plan.actions[index]
            action_state = state.actions[action.id]
            if action_state.status is ActionExecutionStatus.COMPLETED:
                index += 1
                continue
            action_state.status = ActionExecutionStatus.RUNNING
            action_state.error = None
            state.next_action_index = index
            state.next_action_id = action.id
            return WorkflowTransition(state, (ExecuteAction(action.id),))
        state.next_action_index = len(plan.actions)
        state.next_action_id = None
        return WorkflowTransition(state)
