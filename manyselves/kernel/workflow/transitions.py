"""Pure workflow state transitions and external effect requests."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .models import (
    ActionExecutionStatus,
    ConditionGroupAction,
    ForEachAction,
    GotoAction,
    IfAction,
    ResolvedPlan,
    VariableCondition,
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
    result: Any = None


@dataclass(frozen=True, slots=True)
class CompleteNestedExecution:
    """Close one isolated branch before its parent Join executes."""


WorkflowEvent = StartWorkflow | ActionSucceeded | ActionFailed | CompleteNestedExecution


@dataclass(frozen=True, slots=True)
class ExecuteAction:
    """Request that the Runtime Host execute one compiled Action."""

    action_id: str


@dataclass(frozen=True, slots=True)
class WorkflowControlEvent:
    """Business-neutral semantic event produced by pure control flow."""

    kind: str
    action_id: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorkflowTransition:
    """A new authoritative state plus effects for the Runtime Host."""

    state: WorkflowState
    effects: tuple[ExecuteAction, ...] = ()
    events: tuple[WorkflowControlEvent, ...] = ()


def apply_action_result(state: WorkflowState, result: Any) -> WorkflowState:
    """Return a deep-copied State with one executor result patch applied."""

    next_state = state.model_copy(deep=True)
    next_state.variables.update(deepcopy(result.variable_updates))
    next_state.outputs.update(deepcopy(result.output_updates))
    next_state.conversations.update(deepcopy(result.conversation_updates))
    next_state.parallel_results.update(deepcopy(result.parallel_result_updates))
    next_state.parallel_states.update(deepcopy(result.parallel_state_updates))
    next_state.subworkflow_states.update(deepcopy(result.subworkflow_state_updates))
    if result.clear_waiting_input:
        next_state.waiting_input = None
    if result.waiting_input is not None:
        next_state.waiting_input = deepcopy(result.waiting_input)
    if result.workflow_status is not None:
        next_state.status = result.workflow_status
    return next_state


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
            return self._schedule(
                plan,
                next_state,
                next_state.next_action_index,
                reuse_completed=True,
            )
        if isinstance(event, CompleteNestedExecution):
            next_state.status = WorkflowStatus.COMPLETED
            next_state.next_action_id = None
            return WorkflowTransition(next_state)
        if isinstance(event, ActionFailed):
            if event.result is not None:
                next_state = apply_action_result(state, event.result)
            action_state = next_state.actions[event.action_id]
            action_state.status = ActionExecutionStatus.FAILED
            action_state.error = event.error
            next_state.status = WorkflowStatus.FAILED
            return WorkflowTransition(next_state)

        result = event.result
        next_state = apply_action_result(state, result)
        action_state = next_state.actions[event.action_id]
        action_state.output = deepcopy(result.output)
        action_state.error = None
        if next_state.status is WorkflowStatus.WAITING:
            action_state.status = ActionExecutionStatus.WAITING
            return WorkflowTransition(next_state)

        action_state.status = ActionExecutionStatus.COMPLETED
        current_index = next(
            index for index, action in enumerate(plan.actions) if action.id == event.action_id
        )
        next_index = current_index + 1
        if result.next_action_id is not None:
            next_index = next(
                index
                for index, action in enumerate(plan.actions)
                if action.id == result.next_action_id
            )
        next_state.next_action_index = next_index
        if next_state.status is WorkflowStatus.COMPLETED:
            next_state.next_action_id = None
            return WorkflowTransition(next_state)
        next_state.status = WorkflowStatus.RUNNING
        return self._schedule(
            plan,
            next_state,
            next_index,
            reuse_completed=False,
        )

    @staticmethod
    def _schedule(
        plan: ResolvedPlan,
        state: WorkflowState,
        start_index: int,
        *,
        reuse_completed: bool,
    ) -> WorkflowTransition:
        positions = {action.id: index for index, action in enumerate(plan.actions)}
        index = start_index
        events: list[WorkflowControlEvent] = []
        while index < len(plan.actions):
            action = plan.actions[index]
            action_state = state.actions[action.id]
            if reuse_completed and action_state.status is ActionExecutionStatus.COMPLETED:
                index += 1
                continue
            reuse_completed = False
            action_state.status = ActionExecutionStatus.RUNNING
            action_state.error = None
            state.next_action_index = index
            state.next_action_id = action.id
            if isinstance(
                action,
                (IfAction, ConditionGroupAction, GotoAction, ForEachAction),
            ):
                state.control_steps += 1
                if (
                    plan.max_iterations is not None
                    and state.control_steps > plan.max_iterations
                ):
                    action_state.status = ActionExecutionStatus.FAILED
                    action_state.error = (
                        "workflow exceeded declared max_iterations: "
                        f"{plan.max_iterations}"
                    )
                    state.status = WorkflowStatus.FAILED
                    return WorkflowTransition(state)
                target, output = _control_target(action, state)
                action_state.output = output
                action_state.status = ActionExecutionStatus.COMPLETED
                events.append(
                    WorkflowControlEvent(
                        kind="branch.selected",
                        action_id=action.id,
                        data={"target": target},
                    )
                )
                index = positions[target]
                continue
            return WorkflowTransition(
                state,
                (ExecuteAction(action.id),),
                tuple(events),
            )
        state.next_action_index = len(plan.actions)
        state.next_action_id = None
        return WorkflowTransition(state, events=tuple(events))


def _control_target(
    action: IfAction | ConditionGroupAction | GotoAction | ForEachAction,
    state: WorkflowState,
) -> tuple[str, Any]:
    if isinstance(action, IfAction):
        target = action.then if _matches(action.condition, state.variables) else action.otherwise
        return target, target
    if isinstance(action, ConditionGroupAction):
        target = next(
            (
                branch.target
                for branch in action.branches
                if _matches(branch.condition, state.variables)
            ),
            action.default,
        )
        return target, target
    if isinstance(action, GotoAction):
        return action.target, action.target

    frame = state.control_frames.get(action.id)
    if frame is None:
        raw_items = state.variables[action.items_variable]
        if not isinstance(raw_items, (list, tuple)):
            raise ValueError(f"for_each {action.id} requires a list or tuple")
        items = list(raw_items)
        if not items:
            return action.after, None
        frame = {"items": items, "index": 0}
        state.control_frames[action.id] = frame
    else:
        frame["index"] += 1
        if frame["index"] >= len(frame["items"]):
            state.control_frames.pop(action.id, None)
            return action.after, None
    item = frame["items"][frame["index"]]
    state.variables[action.item_variable] = item
    return action.body, item


def _matches(condition: VariableCondition, variables: dict[str, Any]) -> bool:
    left = variables[condition.variable]
    right = condition.value
    if condition.operator == "eq":
        return left == right
    if condition.operator == "ne":
        return left != right
    if condition.operator == "lt":
        return left < right
    if condition.operator == "lte":
        return left <= right
    if condition.operator == "gt":
        return left > right
    if condition.operator == "gte":
        return left >= right
    if condition.operator == "truthy":
        return bool(left)
    if condition.operator == "falsy":
        return not bool(left)
    if condition.operator == "in":
        return left in right
    return left not in right
