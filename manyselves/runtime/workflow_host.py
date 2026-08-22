"""Effect execution, state persistence, and event logging around the Kernel."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import ToolDefinition
from manyselves.kernel.executors import ActionResult, ExecutorRegistry, RuntimeContext
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import (
    ActionFailed,
    ActionKind,
    ActionSucceeded,
    CompleteNestedExecution,
    JoinAction,
    ParallelAction,
    ResolvedPlan,
    StartWorkflow,
    StatelessWorkflowKernel,
    SubworkflowAction,
    WorkflowState,
    WorkflowStatus,
    restore_plan_definition_registry,
)


class WorkflowRuntimeEvent(BaseModel):
    """Stable event-log projection emitted by the Runtime Host."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    run_id: str
    workflow_id: str
    action_id: str | None = None
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


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
        try:
            plan = self._state_store.load_plan(state.run_id)
        except FileNotFoundError:
            self._state_store.save_plan(state.run_id, plan)
        if plan.definition_snapshots:
            definitions = restore_plan_definition_registry(plan)
            contracts = build_contract_catalog(definitions)
            tools = dict(context.tools)
            if context.plan_tool_factory is not None:
                for tool_id in _plan_tool_ids(plan):
                    definition: ToolDefinition = definitions.require("tool", tool_id)
                    tools[tool_id] = context.plan_tool_factory(
                        definition,
                        contracts,
                    )
            context = replace(
                context,
                definitions=definitions,
                contracts=contracts,
                tools=tools,
            )
        actions = {action.id: action for action in plan.actions}
        self._emit("workflow.started", state)
        event = StartWorkflow()
        while True:
            transition = self._kernel.transition(plan, state, event)
            state = transition.state
            self._state_store.save(state)
            if isinstance(event, ActionSucceeded):
                action = actions[event.action_id]
                self._emit_action_events(state, action.id, event.result)
                if action.kind is ActionKind.PUBLISH_RESULT:
                    self._emit("output.published", state, action_id=action.id)
                if state.status is WorkflowStatus.WAITING:
                    self._emit("action.waiting", state, action_id=action.id)
                else:
                    self._emit("action.completed", state, action_id=action.id)
            self._emit_control_events(state, transition.events)
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
                result = await self._execute_action(
                    plan,
                    action,
                    state,
                    context,
                    plan_bundle=plan.subworkflow_plans,
                )
            except Exception as exc:
                failure_result = None
                if isinstance(exc, _ParallelWorkflowExecutionError):
                    failure_result = ActionResult(
                        parallel_result_updates={action.id: exc.results},
                        parallel_state_updates={action.id: exc.states},
                    )
                elif isinstance(exc, _NestedWorkflowExecutionError):
                    failure_result = ActionResult(
                        subworkflow_state_updates={
                            action.id: exc.state.model_dump(mode="json")
                        }
                    )
                failed = self._kernel.transition(
                    plan,
                    state,
                    ActionFailed(action.id, str(exc), failure_result),
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
        *,
        plan_bundle: dict[str, ResolvedPlan],
    ) -> ActionResult:
        if isinstance(action, ParallelAction):
            return await self._execute_parallel(
                plan,
                action,
                state,
                context,
                plan_bundle=plan_bundle,
            )
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
            return await self._execute_subworkflow(
                action,
                state,
                context,
                plan_bundle=plan_bundle,
            )
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
        *,
        plan_bundle: dict[str, ResolvedPlan],
    ) -> ActionResult:
        concurrency = plan.parallel_concurrency.get(
            action.id,
            action.max_concurrency or len(action.branches),
        )
        semaphore = asyncio.Semaphore(concurrency)
        existing_results = state.parallel_results.get(action.id, {})
        existing_states = state.parallel_states.get(action.id, {})
        join_action = next(
            candidate for candidate in plan.actions if candidate.id == action.join
        )

        async def execute_branch(branch_id: str, start_action_id: str):
            if branch_id in existing_results and branch_id in existing_states:
                return branch_id, WorkflowState.model_validate(
                    existing_states[branch_id]
                )
            async with semaphore:
                saved_branch = existing_states.get(branch_id)
                if saved_branch is None:
                    branch_state = WorkflowState.for_plan(state.run_id, plan)
                    branch_state.variables = deepcopy(state.variables)
                    branch_state.conversations = deepcopy(state.conversations)
                    branch_state.next_action_index = next(
                        index
                        for index, candidate in enumerate(plan.actions)
                        if candidate.id == start_action_id
                    )
                    branch_state.next_action_id = start_action_id
                else:
                    branch_state = WorkflowState.model_validate(saved_branch)
                completed = await self._execute_nested(
                    plan,
                    branch_state,
                    context,
                    stop_at=action.join,
                    plan_bundle=plan_bundle,
                )
                return branch_id, completed

        branch_ids = tuple(action.branches)
        branch_outcomes = await asyncio.gather(
            *(
                execute_branch(branch_id, start_action_id)
                for branch_id, start_action_id in action.branches.items()
            ),
            return_exceptions=True,
        )
        completed_branches: list[tuple[str, WorkflowState]] = []
        failures: list[_NestedWorkflowExecutionError] = []
        for branch_id, outcome in zip(branch_ids, branch_outcomes, strict=True):
            if isinstance(outcome, _NestedWorkflowExecutionError):
                completed_branches.append((branch_id, outcome.state))
                failures.append(outcome)
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                completed_branches.append(outcome)
        completed_results = {
            branch_id: {
                join_action.inputs[branch_id]: deepcopy(
                    branch_state.variables[join_action.inputs[branch_id]]
                )
            }
            for branch_id, branch_state in completed_branches
            if branch_state.status is WorkflowStatus.COMPLETED
        }
        branch_states = {
            branch_id: (
                _completed_nested_state_snapshot(
                    branch_state,
                    variables=completed_results[branch_id],
                )
                if branch_state.status is WorkflowStatus.COMPLETED
                else branch_state.model_dump(mode="json")
            )
            for branch_id, branch_state in completed_branches
        }
        if failures:
            raise _ParallelWorkflowExecutionError(
                str(failures[0]),
                states=branch_states,
                results=completed_results,
            )
        waiting_branches = sorted(
            (
                (branch_id, branch_state)
                for branch_id, branch_state in completed_branches
                if branch_state.status is WorkflowStatus.WAITING
            ),
            key=lambda item: item[0],
        )
        if waiting_branches:
            branch_id, branch_state = waiting_branches[0]
            return ActionResult(
                output=sorted(action.branches),
                waiting_input=_prepend_waiting_path(
                    branch_state.waiting_input,
                    {
                        "kind": "parallel",
                        "action_id": action.id,
                        "branch_id": branch_id,
                    },
                ),
                workflow_status=WorkflowStatus.WAITING,
                parallel_result_updates={action.id: completed_results},
                parallel_state_updates={action.id: branch_states},
            )
        return ActionResult(
            output=sorted(action.branches),
            next_action_id=action.join,
            parallel_result_updates={action.id: completed_results},
            parallel_state_updates={action.id: branch_states},
        )

    async def _execute_subworkflow(
        self,
        action: SubworkflowAction,
        state: WorkflowState,
        context: RuntimeContext,
        *,
        plan_bundle: dict[str, ResolvedPlan],
    ) -> ActionResult:
        try:
            child_plan = plan_bundle[action.workflow]
        except KeyError as exc:
            try:
                child_plan = context.subworkflows[action.workflow]
            except KeyError:
                raise RuntimeError(
                    f"missing compiled subworkflow: {action.workflow}"
                ) from exc
        saved = state.subworkflow_states.get(action.id)
        child_state = (
            WorkflowState.model_validate(saved) if saved is not None else None
        )
        if child_state is None or child_state.status is WorkflowStatus.COMPLETED:
            child_state = WorkflowState.for_plan(state.run_id, child_plan)
            if action.input_variable is not None:
                child_state.variables[action.child_input_variable] = state.variables[
                    action.input_variable
                ]
            else:
                child_state.variables.update(
                    {
                        child_variable: state.variables[parent_variable]
                        for child_variable, parent_variable in action.input_variables.items()
                    }
                )
        completed = await self._execute_nested(
            child_plan,
            child_state,
            context,
            emit_workflow_events=True,
            plan_bundle=plan_bundle,
        )
        if completed.status is WorkflowStatus.WAITING:
            return ActionResult(
                waiting_input=_prepend_waiting_path(
                    completed.waiting_input,
                    {
                        "kind": "subworkflow",
                        "action_id": action.id,
                        "workflow_id": action.workflow,
                    },
                ),
                workflow_status=WorkflowStatus.WAITING,
                subworkflow_state_updates={
                    action.id: completed.model_dump(mode="json")
                },
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
                action.id: _completed_nested_state_snapshot(completed)
            },
        )

    async def _execute_nested(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        context: RuntimeContext,
        *,
        stop_at: str | None = None,
        emit_workflow_events: bool = False,
        plan_bundle: dict[str, ResolvedPlan],
    ) -> WorkflowState:
        actions = {action.id: action for action in plan.actions}
        if emit_workflow_events:
            self._emit("workflow.started", state)
        event = StartWorkflow()
        while True:
            transition = self._kernel.transition(plan, state, event)
            state = transition.state
            if isinstance(event, ActionSucceeded):
                action = actions[event.action_id]
                self._emit_action_events(state, action.id, event.result)
                if action.kind is ActionKind.PUBLISH_RESULT:
                    self._emit("output.published", state, action_id=action.id)
                if state.status is WorkflowStatus.WAITING:
                    self._emit("action.waiting", state, action_id=action.id)
                else:
                    self._emit("action.completed", state, action_id=action.id)
            self._emit_control_events(state, transition.events)
            if not transition.effects:
                if emit_workflow_events:
                    if state.status is WorkflowStatus.WAITING:
                        self._emit("workflow.waiting", state)
                    elif state.status is WorkflowStatus.COMPLETED:
                        self._emit("workflow.completed", state)
                    elif state.status is WorkflowStatus.FAILED:
                        self._emit("workflow.failed", state)
                return state
            effect = transition.effects[0]
            if effect.action_id == stop_at:
                return self._kernel.transition(
                    plan,
                    state,
                    CompleteNestedExecution(),
                ).state
            action = actions[effect.action_id]
            self._emit("action.started", state, action_id=action.id)
            try:
                result = await self._execute_action(
                    plan,
                    action,
                    state,
                    context,
                    plan_bundle=plan_bundle,
                )
            except Exception as exc:
                failure_result = None
                if isinstance(exc, _ParallelWorkflowExecutionError):
                    failure_result = ActionResult(
                        parallel_result_updates={action.id: exc.results},
                        parallel_state_updates={action.id: exc.states},
                    )
                elif isinstance(exc, _NestedWorkflowExecutionError):
                    failure_result = ActionResult(
                        subworkflow_state_updates={
                            action.id: exc.state.model_dump(mode="json")
                        }
                    )
                failed = self._kernel.transition(
                    plan,
                    state,
                    ActionFailed(action.id, str(exc), failure_result),
                )
                self._emit(
                    "action.failed",
                    failed.state,
                    action_id=action.id,
                    error=str(exc),
                )
                if emit_workflow_events:
                    self._emit("workflow.failed", failed.state, error=str(exc))
                raise _NestedWorkflowExecutionError(str(exc), failed.state) from exc
            event = ActionSucceeded(action.id, result)

    def _emit(
        self,
        kind: str,
        state: WorkflowState,
        *,
        action_id: str | None = None,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self._events.append(
            WorkflowRuntimeEvent(
                kind=kind,
                run_id=state.run_id,
                workflow_id=state.workflow_id,
                action_id=action_id,
                error=error,
                data=data or {},
            )
        )

    def _emit_action_events(
        self,
        state: WorkflowState,
        action_id: str,
        result: ActionResult,
    ) -> None:
        for event in result.events:
            self._emit(
                event.kind,
                state,
                action_id=action_id,
                data=event.data,
            )

    def _emit_control_events(
        self,
        state: WorkflowState,
        events: tuple[Any, ...],
    ) -> None:
        for event in events:
            self._emit(
                event.kind,
                state,
                action_id=event.action_id,
                data=event.data,
            )


def _completed_nested_state_snapshot(
    state: WorkflowState,
    *,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist only completed child data still owned by the parent Run."""

    return WorkflowState(
        run_id=state.run_id,
        workflow_id=state.workflow_id,
        status=WorkflowStatus.COMPLETED,
        variables=deepcopy(variables or {}),
        conversations=deepcopy(state.conversations),
        outputs=deepcopy(state.outputs),
    ).model_dump(mode="json")


def _plan_tool_ids(plan: ResolvedPlan) -> tuple[str, ...]:
    """Return the direct Tool closure frozen into a root and its child plans."""

    tool_ids: list[str] = []
    pending = [plan]
    while pending:
        current = pending.pop()
        for tool_id in current.tool_ids:
            if tool_id not in tool_ids:
                tool_ids.append(tool_id)
        pending.extend(current.subworkflow_plans.values())
    return tuple(tool_ids)


class _NestedWorkflowExecutionError(RuntimeError):
    """Carry a failed child state back to its parent Runtime effect."""

    def __init__(self, message: str, state: WorkflowState) -> None:
        super().__init__(message)
        self.state = state


class _ParallelWorkflowExecutionError(RuntimeError):
    """Carry every drained branch state back to the parent Runtime action."""

    def __init__(
        self,
        message: str,
        *,
        states: dict[str, dict],
        results: dict[str, dict[str, object]],
    ) -> None:
        super().__init__(message)
        self.states = states
        self.results = results


def _prepend_waiting_path(
    waiting_input: dict | None,
    segment: dict[str, str],
) -> dict:
    """Project one nested waiting input through its parent action."""

    waiting = deepcopy(waiting_input or {})
    waiting["path"] = [segment, *waiting.get("path", [])]
    return waiting
