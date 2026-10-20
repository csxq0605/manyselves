"""Effect execution, state persistence, and event logging around the Kernel."""

import asyncio
import json
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import ToolDefinition
from manyselves.kernel.executors import ActionResult, ExecutorRegistry, RuntimeContext
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import (
    ActionFailed,
    ActionKind,
    ActionSucceeded,
    CompleteNestedExecution,
    JoinAction,
    MapAction,
    ParallelAction,
    RecordError,
    ResolvedPlan,
    StartWorkflow,
    StatelessWorkflowKernel,
    SubworkflowAction,
    WorkflowState,
    WorkflowStatus,
    map_items,
    restore_plan_definition_registry,
)

# ``asyncio.Task.cancel`` message for a user-requested Run cancel.
RUN_CANCEL_REQUESTED = "run cancelled by user"


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
                    checkpoint=self._state_store.save,
                )
            except asyncio.CancelledError as exc:
                # Only an explicit Run cancel is terminal.  An unlabelled
                # cancel (process shutdown) leaves RUNNING for Same-run resume.
                if RUN_CANCEL_REQUESTED not in exc.args:
                    raise
                failed = self._kernel.transition(
                    plan,
                    state,
                    ActionFailed(action.id, RUN_CANCEL_REQUESTED, None),
                )
                self._state_store.save(failed.state)
                self._emit(
                    "action.failed",
                    failed.state,
                    action_id=action.id,
                    error=RUN_CANCEL_REQUESTED,
                )
                self._emit("workflow.failed", failed.state, error=RUN_CANCEL_REQUESTED)
                raise
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
                elif isinstance(exc, _MapWorkflowExecutionError):
                    failure_result = ActionResult(
                        map_state_updates={action.id: exc.states}
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
        checkpoint: Callable[[WorkflowState], None],
    ) -> ActionResult:
        if isinstance(action, ParallelAction):
            return await self._execute_parallel(
                plan,
                action,
                state,
                context,
                plan_bundle=plan_bundle,
                checkpoint=checkpoint,
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
        if isinstance(action, MapAction):
            return await self._execute_map(
                action,
                state,
                context,
                plan_bundle=plan_bundle,
                checkpoint=checkpoint,
            )
        if isinstance(action, SubworkflowAction):
            return await self._execute_subworkflow(
                action,
                state,
                context,
                plan_bundle=plan_bundle,
                checkpoint=checkpoint,
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
        checkpoint: Callable[[WorkflowState], None],
    ) -> ActionResult:
        concurrency = plan.parallel_concurrency.get(
            action.id,
            action.max_concurrency or len(action.branches),
        )
        semaphore = asyncio.Semaphore(concurrency)
        existing_results = deepcopy(state.parallel_results.get(action.id, {}))
        existing_states = deepcopy(state.parallel_states.get(action.id, {}))
        checkpoint_results = deepcopy(existing_results)
        checkpoint_states = deepcopy(existing_states)
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

                def checkpoint_branch(current: WorkflowState) -> None:
                    checkpoint_states[branch_id] = current.model_dump(mode="json")
                    if current.status is WorkflowStatus.COMPLETED:
                        variable = join_action.inputs[branch_id]
                        checkpoint_results[branch_id] = {
                            variable: deepcopy(current.variables[variable])
                        }
                    parallel_state = state.model_copy(deep=True)
                    parallel_state.parallel_states[action.id] = deepcopy(
                        checkpoint_states
                    )
                    parallel_state.parallel_results[action.id] = deepcopy(
                        checkpoint_results
                    )
                    checkpoint(parallel_state)

                completed = await self._execute_nested(
                    plan,
                    branch_state,
                    context,
                    stop_at=action.join,
                    plan_bundle=plan_bundle,
                    checkpoint=checkpoint_branch,
                )
                checkpoint_branch(completed)
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
            conversation_updates=_new_conversations(
                state,
                [(f"{action.id}[{branch_id}]", branch) for branch_id, branch in completed_branches],
            ),
            parallel_result_updates={action.id: completed_results},
            parallel_state_updates={action.id: branch_states},
        )

    async def _execute_map(
        self,
        action: MapAction,
        state: WorkflowState,
        context: RuntimeContext,
        *,
        plan_bundle: dict[str, ResolvedPlan],
        checkpoint: Callable[[WorkflowState], None],
    ) -> ActionResult:
        """Run one child workflow per selected item and collect results by key.

        Each item's child state is checkpointed under ``map_states`` so a
        Same-run resume reuses completed items and continues unfinished ones.
        The in-progress states are dropped once the Map completes.
        """

        child_plan = _require_child_plan(action.workflow, plan_bundle, context)
        try:
            items = map_items(
                state.variables[action.items_variable],
                key_field=action.key_field,
                where=action.where,
                owner=action.id,
            )
        except RecordError as exc:
            raise RuntimeError(str(exc)) from exc
        saved_states = deepcopy(state.map_states.get(action.id, {}))
        item_states: dict[str, dict[str, Any]] = deepcopy(saved_states)
        semaphore = asyncio.Semaphore(action.max_concurrency or max(len(items), 1))

        def checkpoint_item(key: str, current: WorkflowState) -> None:
            item_states[key] = current.model_dump(mode="json")
            parent_state = state.model_copy(deep=True)
            parent_state.map_states[action.id] = deepcopy(item_states)
            checkpoint(parent_state)

        async def execute_item(key: str, item: Any) -> WorkflowState:
            saved = saved_states.get(key)
            if saved is not None and saved.get("status") == WorkflowStatus.COMPLETED:
                return WorkflowState.model_validate(saved)
            async with semaphore:
                if saved is not None:
                    child_state = WorkflowState.model_validate(saved)
                else:
                    child_state = WorkflowState.for_plan(state.run_id, child_plan)
                    child_state.conversations = deepcopy(state.conversations)
                    child_state.variables.update(
                        {
                            child_variable: deepcopy(state.variables[parent_variable])
                            for child_variable, parent_variable in (
                                action.input_variables.items()
                            )
                        }
                    )
                    child_state.variables[action.item_input_variable] = item
                    if action.key_input_variable is not None:
                        child_state.variables[action.key_input_variable] = key
                try:
                    completed = await self._execute_nested(
                        child_plan,
                        child_state,
                        context,
                        plan_bundle=plan_bundle,
                        checkpoint=lambda current: checkpoint_item(key, current),
                    )
                except _NestedWorkflowExecutionError as exc:
                    checkpoint_item(key, exc.state)
                    raise
                if completed.status is WorkflowStatus.WAITING:
                    raise RuntimeError(
                        f"map {action.id} item {key} waits for input; "
                        "map item workflows cannot request input"
                    )
                if action.child_output_name not in completed.outputs:
                    raise RuntimeError(
                        f"map {action.id} item {key}: workflow {action.workflow} "
                        f"has no output {action.child_output_name}"
                    )
                snapshot = _completed_nested_state_snapshot(completed)
                item_states[key] = snapshot
                return WorkflowState.model_validate(snapshot)

        outcomes = await asyncio.gather(
            *(execute_item(key, item) for key, item in items),
            return_exceptions=True,
        )
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        completed_items: list[tuple[str, WorkflowState]] = []
        for (key, _item), outcome in zip(items, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                errors[key] = str(outcome)
                self._emit(
                    "map.item.failed",
                    state,
                    action_id=action.id,
                    error=str(outcome),
                    data={"key": key},
                )
                continue
            results[key] = deepcopy(outcome.outputs[action.child_output_name])
            completed_items.append((f"{action.id}[{key}]", outcome))
            self._emit(
                "map.item.completed",
                state,
                action_id=action.id,
                data={"key": key},
            )
        if errors and action.errors_variable is None:
            first_key = next(iter(errors))
            raise _MapWorkflowExecutionError(
                f"map {action.id} item {first_key} failed: {errors[first_key]}",
                states=item_states,
            )
        variable_updates: dict[str, Any] = {action.output_variable: results}
        if action.errors_variable is not None:
            variable_updates[action.errors_variable] = errors
        return ActionResult(
            output={"completed": list(results), "failed": list(errors)},
            variable_updates=variable_updates,
            conversation_updates=_new_conversations(state, completed_items),
            map_state_updates={action.id: {}},
        )

    async def _execute_subworkflow(
        self,
        action: SubworkflowAction,
        state: WorkflowState,
        context: RuntimeContext,
        *,
        plan_bundle: dict[str, ResolvedPlan],
        checkpoint: Callable[[WorkflowState], None],
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
            child_state.conversations = deepcopy(state.conversations)
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
        def checkpoint_child(current: WorkflowState) -> None:
            parent_state = state.model_copy(deep=True)
            parent_state.subworkflow_states[action.id] = current.model_dump(
                mode="json"
            )
            checkpoint(parent_state)

        completed = await self._execute_nested(
            child_plan,
            child_state,
            context,
            emit_workflow_events=True,
            plan_bundle=plan_bundle,
            checkpoint=checkpoint_child,
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
            conversation_updates=_new_conversations(state, [(action.id, completed)]),
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
        checkpoint: Callable[[WorkflowState], None],
    ) -> WorkflowState:
        actions = {action.id: action for action in plan.actions}
        if emit_workflow_events:
            self._emit("workflow.started", state)
        event = StartWorkflow()
        while True:
            transition = self._kernel.transition(plan, state, event)
            state = transition.state
            checkpoint(state)
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
                    checkpoint=checkpoint,
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
                elif isinstance(exc, _MapWorkflowExecutionError):
                    failure_result = ActionResult(
                        map_state_updates={action.id: exc.states}
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


def _new_conversations(
    parent: WorkflowState,
    children: Iterable[tuple[str, WorkflowState]],
) -> dict[str, Any]:
    """Conversations children opened, keyed ``{prefix}.{variable}`` for the parent.

    Keeping them in the parent state lets a later action resolve a child's
    conversation by key after a restart.
    """

    def identity(value: Any) -> str:
        if isinstance(value, ConversationRecord):
            return value.conversation_id
        if isinstance(value, dict) and isinstance(value.get("conversation_id"), str):
            return value["conversation_id"]
        return json.dumps(value, sort_keys=True, default=str)

    known = {identity(value) for value in parent.conversations.values()}
    found: dict[str, Any] = {}
    for prefix, child in children:
        for variable, value in child.conversations.items():
            if identity(value) not in known:
                found[f"{prefix}.{variable}"] = deepcopy(value)
    return found


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


class _MapWorkflowExecutionError(RuntimeError):
    """Carry every item state of a failed Map back to the parent action."""

    def __init__(self, message: str, *, states: dict[str, dict]) -> None:
        super().__init__(message)
        self.states = states


def _require_child_plan(
    workflow_id: str,
    plan_bundle: dict[str, ResolvedPlan],
    context: RuntimeContext,
) -> ResolvedPlan:
    try:
        return plan_bundle[workflow_id]
    except KeyError:
        try:
            return context.subworkflows[workflow_id]
        except KeyError as exc:
            raise RuntimeError(f"missing compiled subworkflow: {workflow_id}") from exc


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
