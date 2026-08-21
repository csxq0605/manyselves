"""Sequential resolved-plan executor for the WP-02 action subset."""

from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    ResolvedPlan,
    WorkflowState,
    WorkflowStatus,
)

from .base import ExecutorRegistry, RuntimeContext


class SequentialWorkflowExecutor:
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
        state.status = WorkflowStatus.RUNNING
        self._state_store.save(state)
        action_positions = {
            action.id: index for index, action in enumerate(plan.actions)
        }
        index = state.next_action_index
        while index < len(plan.actions):
            action = plan.actions[index]
            action_state = state.actions[action.id]
            if action_state.status is ActionExecutionStatus.COMPLETED:
                index += 1
                state.next_action_index = index
                continue
            action_state.status = ActionExecutionStatus.RUNNING
            action_state.error = None
            self._state_store.save(state)
            try:
                result = await self._executors.require(action.kind).execute(
                    action,
                    state,
                    context,
                )
            except Exception as exc:
                action_state.status = ActionExecutionStatus.FAILED
                action_state.error = str(exc)
                state.status = WorkflowStatus.FAILED
                self._state_store.save(state)
                raise
            state.variables.update(result.variable_updates)
            state.outputs.update(result.output_updates)
            state.conversations.update(result.conversation_updates)
            if result.clear_waiting_input:
                state.waiting_input = None
            if result.waiting_input is not None:
                state.waiting_input = result.waiting_input
            action_state.output = result.output
            if result.workflow_status is not None:
                state.status = result.workflow_status
            if state.status is WorkflowStatus.WAITING:
                action_state.status = ActionExecutionStatus.WAITING
                state.next_action_index = index
                self._state_store.save(state)
                return state
            action_state.status = ActionExecutionStatus.COMPLETED
            if result.next_action_id is None:
                index += 1
            else:
                index = action_positions[result.next_action_id]
            state.next_action_index = index
            self._state_store.save(state)
        return state
