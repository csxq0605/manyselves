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
        state.status = WorkflowStatus.RUNNING
        self._state_store.save(state)
        for index in range(state.next_action_index, len(plan.actions)):
            action = plan.actions[index]
            action_state = state.actions[action.id]
            if action_state.status is ActionExecutionStatus.COMPLETED:
                state.next_action_index = index + 1
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
            action_state.output = result.output
            action_state.status = ActionExecutionStatus.COMPLETED
            state.next_action_index = index + 1
            if result.workflow_status is not None:
                state.status = result.workflow_status
            self._state_store.save(state)
        return state
