"""Small internal control-flow runtime over one authoritative WorkflowState."""

import asyncio
from copy import deepcopy
from typing import Any

from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    ConditionGroupAction,
    ForEachAction,
    GotoAction,
    IfAction,
    JoinAction,
    ParallelAction,
    ResolvedAction,
    ResolvedPlan,
    SubworkflowAction,
    VariableCondition,
    WorkflowState,
    WorkflowStatus,
)

from .base import ExecutorRegistry, RuntimeContext, RuntimeExecutionError


class ControlFlowWorkflowExecutor:
    """Execute compiled branches, loops, joins, and child workflows."""

    def __init__(
        self,
        executors: ExecutorRegistry,
        state_store: WorkflowStateStore,
    ) -> None:
        self._executors = executors
        self._state_store = state_store

    async def execute(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> WorkflowState:
        if state.status is WorkflowStatus.COMPLETED:
            return state
        self._state_store.save_plan(state.run_id, plan)
        if state.next_action_id is None:
            state.next_action_id = plan.entry_action_id
        return await self._execute_until(plan, state, context, persist=True)

    async def _execute_until(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        context: RuntimeContext,
        *,
        persist: bool,
        stop_at: set[str] | None = None,
    ) -> WorkflowState:
        actions = {action.id: action for action in plan.actions}
        positions = {action.id: index for index, action in enumerate(plan.actions)}
        stop_at = stop_at or set()
        state.status = WorkflowStatus.RUNNING
        self._save(state, persist)

        while state.next_action_id is not None:
            if state.next_action_id in stop_at:
                return state
            try:
                action = actions[state.next_action_id]
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"missing compiled action: {state.next_action_id}"
                ) from exc
            state.control_steps += 1
            if plan.max_iterations is not None and state.control_steps > plan.max_iterations:
                state.status = WorkflowStatus.FAILED
                self._save(state, persist)
                raise RuntimeExecutionError(
                    f"workflow exceeded declared max_iterations: {plan.max_iterations}"
                )

            action_state = state.actions[action.id]
            action_state.status = ActionExecutionStatus.RUNNING
            action_state.error = None
            self._save(state, persist)
            try:
                next_action_id, output = await self._execute_action(
                    action,
                    plan,
                    state,
                    context,
                    positions,
                )
            except Exception as exc:
                action_state.status = ActionExecutionStatus.FAILED
                action_state.error = str(exc)
                state.status = WorkflowStatus.FAILED
                self._save(state, persist)
                raise

            action_state.output = output
            if state.status is WorkflowStatus.WAITING:
                action_state.status = ActionExecutionStatus.WAITING
                state.next_action_id = action.id
                state.next_action_index = positions[action.id]
                self._save(state, persist)
                return state
            action_state.status = ActionExecutionStatus.COMPLETED
            state.next_action_id = next_action_id
            state.next_action_index = (
                positions[next_action_id] if next_action_id in positions else len(plan.actions)
            )
            self._save(state, persist)
            if state.status is WorkflowStatus.COMPLETED:
                state.next_action_id = None
                self._save(state, persist)
                return state
        return state

    async def _execute_action(
        self,
        action: ResolvedAction,
        plan: ResolvedPlan,
        state: WorkflowState,
        context: RuntimeContext,
        positions: dict[str, int],
    ) -> tuple[str | None, Any]:
        if isinstance(action, IfAction):
            target = (
                action.then if _matches(action.condition, state.variables) else action.otherwise
            )
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
        if isinstance(action, ForEachAction):
            frame = state.control_frames.get(action.id)
            if frame is None:
                raw_items = state.variables[action.items_variable]
                if not isinstance(raw_items, (list, tuple)):
                    raise RuntimeExecutionError(f"for_each {action.id} requires a list or tuple")
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
        if isinstance(action, ParallelAction):
            if action.id not in state.parallel_results:
                semaphore = asyncio.Semaphore(
                    action.max_concurrency or len(action.branches)
                )

                async def execute_branch(
                    branch_id: str,
                    start_action_id: str,
                ) -> tuple[str, dict[str, Any], dict[str, Any]]:
                    async with semaphore:
                        return await self._execute_branch(
                            branch_id,
                            start_action_id,
                            action.join,
                            plan,
                            state,
                            context,
                        )

                branch_results = await asyncio.gather(
                    *(
                        execute_branch(
                            branch_id,
                            start_action_id,
                        )
                        for branch_id, start_action_id in action.branches.items()
                    )
                )
                state.parallel_results[action.id] = {
                    branch_id: variables for branch_id, variables, _actions in branch_results
                }
                for _branch_id, _variables, action_updates in branch_results:
                    for action_id, action_state in action_updates.items():
                        if action_state.status is not ActionExecutionStatus.PENDING:
                            state.actions[action_id] = action_state
            return action.join, sorted(action.branches)
        if isinstance(action, JoinAction):
            try:
                branches = state.parallel_results[action.parallel]
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"join {action.id} has no completed parallel result"
                ) from exc
            joined = {
                branch_id: branches[branch_id][variable]
                for branch_id, variable in action.inputs.items()
            }
            state.variables[action.output_variable] = joined
            return _next_id(action.id, plan.actions, positions), joined
        if isinstance(action, SubworkflowAction):
            try:
                child_plan = context.subworkflows[action.workflow]
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"missing compiled subworkflow: {action.workflow}"
                ) from exc
            child_run_id = f"{state.run_id}--{action.id}"
            try:
                child_state = self._state_store.load(child_run_id)
            except FileNotFoundError:
                child_state = WorkflowState.for_plan(child_run_id, child_plan)
                child_state.variables[action.child_input_variable] = state.variables[
                    action.input_variable
                ]
            child_completed = await self.execute(child_plan, child_state, context)
            try:
                child_output = child_completed.outputs[action.child_output_name]
            except KeyError as exc:
                raise RuntimeExecutionError(
                    f"subworkflow {action.workflow} has no output {action.child_output_name}"
                ) from exc
            state.variables[action.output_variable] = child_output
            return _next_id(action.id, plan.actions, positions), child_output

        result = await self._executors.require(action.kind).execute(action, state, context)
        state.variables.update(result.variable_updates)
        state.outputs.update(result.output_updates)
        state.conversations.update(result.conversation_updates)
        if result.clear_waiting_input:
            state.waiting_input = None
        if result.waiting_input is not None:
            state.waiting_input = result.waiting_input
        if result.workflow_status is not None:
            state.status = result.workflow_status
        return (
            result.next_action_id or _next_id(action.id, plan.actions, positions),
            result.output,
        )

    async def _execute_branch(
        self,
        branch_id: str,
        start_action_id: str,
        join_action_id: str,
        plan: ResolvedPlan,
        parent: WorkflowState,
        context: RuntimeContext,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        branch_state = WorkflowState.for_plan(
            f"{parent.run_id}--parallel-{branch_id}",
            plan,
        )
        branch_state.variables = deepcopy(parent.variables)
        branch_state.next_action_id = start_action_id
        completed = await self._execute_until(
            plan,
            branch_state,
            context,
            persist=False,
            stop_at={join_action_id},
        )
        return branch_id, completed.variables, completed.actions

    def _save(self, state: WorkflowState, persist: bool) -> None:
        if persist:
            self._state_store.save(state)


def _next_id(
    action_id: str,
    actions: list[ResolvedAction],
    positions: dict[str, int],
) -> str | None:
    next_index = positions[action_id] + 1
    return actions[next_index].id if next_index < len(actions) else None


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
