"""Effect execution, state persistence, and event logging around the Kernel."""

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from manyselves.kernel.executors import ActionResult, ExecutorRegistry, RuntimeContext
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import (
    ActionFailed,
    ActionKind,
    ActionSucceeded,
    JoinAction,
    ParallelAction,
    ResolvedPlan,
    StartWorkflow,
    StatelessWorkflowKernel,
    SubworkflowAction,
    WorkflowState,
    WorkflowStatus,
)


class WorkflowRuntimeEvent(BaseModel):
    """Stable event-log projection emitted by the Runtime Host."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    run_id: str
    workflow_id: str
    action_id: str | None = None
    error: str | None = None


class WorkflowEventSink(Protocol):
    def append(self, event: WorkflowRuntimeEvent) -> None: ...


class InMemoryWorkflowEventSink:
    def __init__(self) -> None:
        self.events: list[WorkflowRuntimeEvent] = []

    def append(self, event: WorkflowRuntimeEvent) -> None:
        self.events.append(event)


class FileWorkflowEventSink:
    """Append the execution trace beside one Run's authoritative state."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    def append(self, event: WorkflowRuntimeEvent) -> None:
        path = (
            self._workspace
            / "Work"
            / "runs"
            / event.run_id
            / "workflow-events.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json())
            stream.write("\n")


class WorkflowRuntimeHost:
    """Execute Kernel effects while the Kernel remains persistence-free."""

    def __init__(
        self,
        executors: ExecutorRegistry,
        state_store: WorkflowStateStore,
        events: WorkflowEventSink,
        *,
        kernel: StatelessWorkflowKernel | None = None,
    ) -> None:
        self._executors = executors
        self._state_store = state_store
        self._events = events
        self._kernel = kernel or StatelessWorkflowKernel()

    async def execute(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> WorkflowState:
        if state.status is WorkflowStatus.COMPLETED:
            return state
        actions = {action.id: action for action in plan.actions}
        self._emit("workflow.started", state)
        event = StartWorkflow()
        while True:
            transition = self._kernel.transition(plan, state, event)
            state = transition.state
            self._state_store.save(state)
            if isinstance(event, ActionSucceeded):
                action = actions[event.action_id]
                if action.kind is ActionKind.PUBLISH_RESULT:
                    self._emit("output.published", state, action_id=action.id)
                if state.status is WorkflowStatus.WAITING:
                    self._emit("action.waiting", state, action_id=action.id)
                else:
                    self._emit("action.completed", state, action_id=action.id)
            if not transition.effects:
                if state.status is WorkflowStatus.WAITING:
                    self._emit("workflow.waiting", state)
                elif state.status is WorkflowStatus.COMPLETED:
                    self._emit("workflow.completed", state)
                elif state.status is WorkflowStatus.FAILED:
                    self._emit("workflow.failed", state)
                return state

            effect = transition.effects[0]
            action = actions[effect.action_id]
            self._emit("action.started", state, action_id=action.id)
            try:
                result = await self._execute_action(plan, action, state, context)
            except Exception as exc:
                failed = self._kernel.transition(
                    plan,
                    state,
                    ActionFailed(action.id, str(exc)),
                )
                self._state_store.save(failed.state)
                self._emit("action.failed", failed.state, action_id=action.id, error=str(exc))
                self._emit("workflow.failed", failed.state, error=str(exc))
                raise
            event = ActionSucceeded(action.id, result)

    async def _execute_action(
        self,
        plan: ResolvedPlan,
        action,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        if isinstance(action, ParallelAction):
            return await self._execute_parallel(plan, action, state, context)
        if isinstance(action, JoinAction):
            try:
                branches = state.parallel_results[action.parallel]
            except KeyError as exc:
                raise RuntimeError(
                    f"join {action.id} has no completed parallel result"
                ) from exc
            joined = {
                branch_id: branches[branch_id][variable]
                for branch_id, variable in action.inputs.items()
            }
            return ActionResult(
                output=joined,
                variable_updates={action.output_variable: joined},
            )
        if isinstance(action, SubworkflowAction):
            return await self._execute_subworkflow(action, state, context)
        return await self._executors.require(action.kind).execute(
            action,
            state,
            context,
        )

    async def _execute_parallel(
        self,
        plan: ResolvedPlan,
        action: ParallelAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        semaphore = asyncio.Semaphore(action.max_concurrency or len(action.branches))
        existing_results = state.parallel_results.get(action.id, {})
        existing_states = state.parallel_states.get(action.id, {})

        async def execute_branch(branch_id: str, start_action_id: str):
            if branch_id in existing_results and branch_id in existing_states:
                return branch_id, WorkflowState.model_validate(
                    existing_states[branch_id]
                )
            async with semaphore:
                branch_state = WorkflowState.for_plan(state.run_id, plan)
                branch_state.variables = deepcopy(state.variables)
                branch_state.conversations = deepcopy(state.conversations)
                branch_state.next_action_index = next(
                    index
                    for index, candidate in enumerate(plan.actions)
                    if candidate.id == start_action_id
                )
                branch_state.next_action_id = start_action_id
                completed = await self._execute_nested(
                    plan,
                    branch_state,
                    context,
                    stop_at=action.join,
                )
                return branch_id, completed

        completed_branches = await asyncio.gather(
            *(
                execute_branch(branch_id, start_action_id)
                for branch_id, start_action_id in action.branches.items()
            )
        )
        return ActionResult(
            output=sorted(action.branches),
            next_action_id=action.join,
            parallel_result_updates={
                action.id: {
                    **existing_results,
                    **{
                        branch_id: branch_state.variables
                        for branch_id, branch_state in completed_branches
                    },
                }
            },
            parallel_state_updates={
                action.id: {
                    **existing_states,
                    **{
                        branch_id: branch_state.model_dump(mode="json")
                        for branch_id, branch_state in completed_branches
                    },
                }
            },
        )

    async def _execute_subworkflow(
        self,
        action: SubworkflowAction,
        state: WorkflowState,
        context: RuntimeContext,
    ) -> ActionResult:
        try:
            child_plan = context.subworkflows[action.workflow]
        except KeyError as exc:
            raise RuntimeError(
                f"missing compiled subworkflow: {action.workflow}"
            ) from exc
        saved = state.subworkflow_states.get(action.id)
        if saved is None:
            child_state = WorkflowState.for_plan(state.run_id, child_plan)
            child_state.variables[action.child_input_variable] = state.variables[
                action.input_variable
            ]
        else:
            child_state = WorkflowState.model_validate(saved)
        completed = await self._execute_nested(
            child_plan,
            child_state,
            context,
        )
        try:
            child_output = completed.outputs[action.child_output_name]
        except KeyError as exc:
            raise RuntimeError(
                f"subworkflow {action.workflow} has no output {action.child_output_name}"
            ) from exc
        return ActionResult(
            output=child_output,
            variable_updates={action.output_variable: child_output},
            subworkflow_state_updates={
                action.id: completed.model_dump(mode="json")
            },
        )

    async def _execute_nested(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        context: RuntimeContext,
        *,
        stop_at: str | None = None,
    ) -> WorkflowState:
        actions = {action.id: action for action in plan.actions}
        event = StartWorkflow()
        while True:
            transition = self._kernel.transition(plan, state, event)
            state = transition.state
            if not transition.effects:
                return state
            effect = transition.effects[0]
            if effect.action_id == stop_at:
                state = state.model_copy(deep=True)
                state.status = WorkflowStatus.COMPLETED
                state.next_action_id = None
                return state
            action = actions[effect.action_id]
            result = await self._execute_action(plan, action, state, context)
            event = ActionSucceeded(action.id, result)

    def _emit(
        self,
        kind: str,
        state: WorkflowState,
        *,
        action_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self._events.append(
            WorkflowRuntimeEvent(
                kind=kind,
                run_id=state.run_id,
                workflow_id=state.workflow_id,
                action_id=action_id,
                error=error,
            )
        )
