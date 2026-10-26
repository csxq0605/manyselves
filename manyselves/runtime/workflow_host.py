"""Effect execution, state persistence, and event logging around the Kernel."""

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from loguru import logger
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

from .prefix_lead import watching_first_response

# ``asyncio.Task.cancel`` message for a user-requested Run cancel.
RUN_CANCEL_REQUESTED = "run cancelled by user"
RUN_STOPPED_BEFORE = "run stopped before step"
# After the lead's first token, how long the others still wait for the
# provider to store the prefix it read.  The provider builds that entry within
# seconds and does not say when it is done.
LEAD_CACHE_SETTLE_SECONDS = 3.0
# How often a Map saves its running items (completed or failed ones at once).
MAP_CHECKPOINT_SECONDS = 1.0


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
        # The time an event was written, for progress views; not part of the
        # event itself.
        line = {**event.model_dump(mode="json"), "ts": datetime.now(UTC).isoformat()}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False))
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
        save_sparingly: Callable[[], bool] | None = None,
    ) -> None:
        self._executors = executors
        self._state_store = state_store
        self._events = events
        self._kernel = kernel or StatelessWorkflowKernel()
        # True while steps finish at once (skipped, not executed): the state
        # is then saved every few seconds instead of after every transition.
        self._save_sparingly = save_sparingly

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
        save = _ThrottledSave(self._state_store.save, self._save_sparingly)
        self._emit("workflow.started", state)
        event = StartWorkflow()
        while True:
            transition = self._kernel.transition(plan, state, event)
            state = transition.state
            save(state, now=not transition.effects)
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
                    checkpoint=save,
                )
            except asyncio.CancelledError as exc:
                # Only an explicit Run cancel is terminal.  An unlabelled
                # cancel (process shutdown) leaves RUNNING for Same-run resume.
                if RUN_CANCEL_REQUESTED not in exc.args:
                    save.flush()
                    raise
                # Nested steps checkpoint views of the parent that carry their
                # children's progress; the local state never sees them.  Fail
                # from the newest one so completed children are kept.
                failed = self._kernel.transition(
                    plan,
                    save.latest or state,
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
        if context.stop_before is not None and action.id in context.stop_before():
            raise RuntimeError(f"{RUN_STOPPED_BEFORE} {action.id} as requested")
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
                    checkpoint(
                        _checkpoint_view(
                            state,
                            parallel_states={action.id: dict(checkpoint_states)},
                            parallel_results={action.id: dict(checkpoint_results)},
                        )
                    )

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
        pending = [
            key for key, _item in items
            if (saved_states.get(key) or {}).get("status") != WorkflowStatus.COMPLETED
        ]
        lead_key = pending[0] if action.lead_seconds and len(pending) > 1 else None
        lead_started = asyncio.Event()
        lead_done = asyncio.Event()
        event_loop = asyncio.get_running_loop()

        def lead_responded() -> None:
            # A tool may run in a worker thread; set the event on the loop.
            event_loop.call_soon_threadsafe(lead_started.set)

        # Every item step would save the whole Run; with many items starting
        # at once that blocks the loop for seconds.  Running steps are saved
        # at most once a second; an item that completes or fails at once.
        unsaved: dict[str, WorkflowState] = {}
        save_timer: list[asyncio.TimerHandle | None] = [None]
        last_save = [float("-inf")]

        def save_items() -> None:
            if save_timer[0] is not None:
                save_timer[0].cancel()
                save_timer[0] = None
            for unsaved_key, current in unsaved.items():
                item_states[unsaved_key] = current.model_dump(mode="json")
            unsaved.clear()
            last_save[0] = time.monotonic()
            checkpoint(_checkpoint_view(state, map_states={action.id: dict(item_states)}))

        def checkpoint_item(key: str, current: WorkflowState) -> None:
            unsaved[key] = current
            waited = time.monotonic() - last_save[0]
            if current.status is not WorkflowStatus.RUNNING or waited >= MAP_CHECKPOINT_SECONDS:
                save_items()
            elif save_timer[0] is None:
                save_timer[0] = event_loop.call_later(MAP_CHECKPOINT_SECONDS - waited, save_items)

        async def execute_item(key: str, item: Any) -> WorkflowState:
            saved = saved_states.get(key)
            if saved is not None and saved.get("status") == WorkflowStatus.COMPLETED:
                return WorkflowState.model_validate(saved)
            if lead_key is not None and key != lead_key:
                # The others wait until the provider has read the lead's
                # prompt (its first token is back) and a moment more for the
                # cache entry; lead_seconds caps the wait.
                waited = time.monotonic()
                try:
                    await asyncio.wait_for(lead_started.wait(), timeout=action.lead_seconds)
                except TimeoutError:
                    logger.info(
                        "map {} item {}: no response from lead {} within {}s; starting",
                        action.id, key, lead_key, action.lead_seconds,
                    )
                else:
                    if not lead_done.is_set():
                        await asyncio.sleep(LEAD_CACHE_SETTLE_SECONDS)
                    logger.debug(
                        "map {} item {}: starting {:.1f}s after the lead",
                        action.id, key, time.monotonic() - waited,
                    )
            if key != lead_key:
                return await run_item(key, item, saved)
            try:
                with watching_first_response(lead_responded):
                    return await run_item(key, item, saved)
            finally:
                lead_done.set()
                lead_started.set()

        async def run_item(key: str, item: Any, saved: dict[str, Any] | None) -> WorkflowState:
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
                unsaved.pop(key, None)
                item_states[key] = snapshot
                return WorkflowState.model_validate(snapshot)

        try:
            outcomes = await asyncio.gather(
                *(execute_item(key, item) for key, item in items),
                return_exceptions=True,
            )
        finally:
            # The Map's own result is saved by its caller; a late timed save
            # must not write the item states back after that.
            if save_timer[0] is not None:
                save_timer[0].cancel()
                save_timer[0] = None
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
            checkpoint(
                _checkpoint_view(
                    state,
                    subworkflow_states={action.id: current.model_dump(mode="json")},
                )
            )

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


def _checkpoint_view(state: WorkflowState, **children: dict[str, Any]) -> WorkflowState:
    """The parent state with one nested entry replaced, for saving only.

    A checkpoint is serialized at once, so the view shares the parent's
    variables instead of deep-copying the whole state on every nested step.
    """

    return state.model_copy(
        update={
            field: {**getattr(state, field), **entries}
            for field, entries in children.items()
        }
    )


class _ThrottledSave:
    """Save the Run's state after each transition, or every few seconds.

    The state is the whole Run, nested steps included, and a running Run
    saves it after every transition, so that a stopped process resumes at
    the step it was in.  While steps are being skipped (a rerun replaying
    unchanged steps) each finishes at once and saving costs more than the
    step; then the state is saved at most every ``interval`` seconds.  A
    state that settles (completed, failed, waiting) is always saved at once.
    If the process stops between saves, resume repeats a few skipped steps.
    """

    def __init__(
        self,
        save: Callable[[WorkflowState], None],
        sparingly: Callable[[], bool] | None = None,
        interval: float = 2.0,
    ) -> None:
        self._save = save
        self._sparingly = sparingly
        self._interval = interval
        self._last = float("-inf")
        self._pending: WorkflowState | None = None
        # The newest state handed in, saved or not.
        self.latest: WorkflowState | None = None

    def __call__(self, state: WorkflowState, *, now: bool = False) -> None:
        self.latest = state
        moment = time.monotonic()
        if (
            now
            or state.status is not WorkflowStatus.RUNNING
            or self._sparingly is None
            or not self._sparingly()
            or moment - self._last >= self._interval
        ):
            self._save(state)
            self._last = moment
            self._pending = None
        else:
            self._pending = state

    def flush(self) -> None:
        if self._pending is not None:
            self._save(self._pending)
            self._pending = None
