"""Effect execution, state persistence, and event logging around the Kernel."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from manyselves.kernel.executors import ExecutorRegistry, RuntimeContext
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import (
    ActionFailed,
    ActionKind,
    ActionSucceeded,
    ResolvedPlan,
    StartWorkflow,
    StatelessWorkflowKernel,
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
                result = await self._executors.require(action.kind).execute(
                    action,
                    state,
                    context,
                )
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
