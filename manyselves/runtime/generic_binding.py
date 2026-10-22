"""One Run lifecycle for any file-defined Capability.

A Capability supplies its definition file and the Python implementations of
its Tools.  Everything else is generic: compile an entrypoint workflow, start
or resume the Run on the Runtime Host, invoke every Agent through the
definition-driven invoker, and project state, outputs and cost.

A Run can also be executed again in place, under the same run id: the
previous version's files move to ``versions/v<n>``, the current plan is
compiled and executed from the start, and every Agent turn whose context is
unchanged is replayed from the Run's turn memo instead of calling the model.
Resume does this on its own when the definitions changed since the Run was
compiled; a Capability can also ask for it with revised input.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from inspect import isawaitable
from pathlib import Path
from typing import Any
from uuid import UUID

from loguru import logger

from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
    load_capability,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    ResolvedPlan,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    restore_plan_definition_registry,
    resume_waiting_input,
)

from .agent_execution import AgentExecutionService, AgentSessionLoop
from .capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from .conversation_store import FileConversationStore
from .definition_agent import DefinitionAgentInvoker
from .run_lifecycle import DetachedRunTaskOwner, StartAwareFileWorkflowStateStore
from .tool_adapter import CapabilityToolAdapter, CapabilityToolAdapterFactory
from .tools.result_memory import RunToolResultIndex
from .usage_ledger import UsageLedger
from .workflow_host import FileWorkflowEventSink, WorkflowRuntimeHost

ToolImplementations = Mapping[str, Callable[[Any], Any]]
StartInput = Callable[[str, str, Any], dict[str, Any]]
# Called with the run id after an execution of a root workflow stops.
SettledListener = Callable[[str], Awaitable[None] | None]
# (run_id, workflow_id, revision values or None, previous start input,
#  archived version directory) -> initial variables of the new version.
RerunInput = Callable[[str, str, Any, dict[str, Any], Path], dict[str, Any]]

START_INPUT_FILE = "start-input.json"
# Run-directory entries that stay in place when a version is archived.
_KEPT_ON_RERUN = frozenset({"agent-memo", "versions", START_INPUT_FILE})


class GenericCapabilityRuntimeBinding:
    """Run any entrypoint of one Capability through the generic Runtime."""

    def __init__(
        self,
        *,
        capability_file: Path,
        workspace: Path,
        services: Any,
        tool_implementations: ToolImplementations,
        loop_builder: Callable[..., AgentSessionLoop] | None = None,
        start_input: StartInput | None = None,
        execution: AgentExecutionService | None = None,
        rerun_input: RerunInput | None = None,
        kept_on_rerun: tuple[str, ...] = (),
    ) -> None:
        capability, _registry = load_capability(capability_file)
        self.capability_id = capability.id
        self.entrypoints = tuple(capability.entrypoints)
        self.capability_file = Path(capability_file)
        self.workspace = Path(workspace).resolve()
        self.services = services
        self.tool_implementations = tool_implementations
        self._loop_builder = loop_builder
        self._start_input = start_input
        self._rerun_input = rerun_input
        self._settled_listeners: list[SettledListener] = []
        self._kept_on_rerun = _KEPT_ON_RERUN | set(kept_on_rerun)
        self._store = StartAwareFileWorkflowStateStore(self.workspace)
        self._executors = build_builtin_executor_registry()
        self._detached = DetachedRunTaskOwner()
        self._execution = execution or (
            AgentExecutionService(services.bus) if services is not None else None
        )

    @property
    def active(self) -> bool:
        return self._detached.active

    @property
    def execution(self) -> AgentExecutionService | None:
        return self._execution

    @property
    def state_store(self) -> StartAwareFileWorkflowStateStore:
        return self._store

    def add_settled_listener(self, listener: SettledListener) -> None:
        """Be told when an execution stops: completed, waiting or failed."""

        self._settled_listeners.append(listener)

    async def _settled(self, run_id: str) -> None:
        for listener in tuple(self._settled_listeners):
            try:
                result = listener(run_id)
                if isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - a listener never fails the Run
                logger.exception("settled listener failed for run {}", run_id)

    def run_id_for(self, command_id: UUID, workflow_id: str) -> str:
        return f"{workflow_id}-{command_id.hex}"

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        registry, plan = self._compile(workflow_id)
        contracts = _contracts(registry)
        run_id = self.run_id_for(command_id, workflow_id)
        # Resolve every Tool the plan reaches before any Run state exists.
        self._tools(plan, contracts, None)
        try:
            state = self._store.load(run_id)
        except FileNotFoundError:
            initial: dict[str, Any] = {}
            if self._start_input is not None:
                # The Capability projects its public input into initial variables.
                try:
                    initial = self._start_input(run_id, workflow_id, values)
                except ValueError as exc:
                    raise CapabilityRunInputError(str(exc)) from exc
            elif plan.input_variable is not None:
                if plan.input_contract is None:
                    raise TypeError(f"workflow {workflow_id} input has no contract")
                try:
                    initial[plan.input_variable] = contracts[plan.input_contract].validate(
                        values
                    )
                except ValueError as exc:
                    raise CapabilityRunInputError(str(exc)) from exc
            state = WorkflowState.for_plan(run_id, plan, initial_variables=initial)
            self._store.save_plan(run_id, plan)
            self._save_start_input(run_id, initial)
        await self._execute(plan, state, registry)
        return {"run_id": run_id, "task_id": None}

    async def start_detached(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        self._compile(workflow_id)
        run_id = self.run_id_for(command_id, workflow_id)
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self.start(command_id, workflow_id, values),
        )

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        del command_id
        if input_id is None:
            raise CapabilityRunInputError(f"{self.capability_id} has no unnamed input")
        state = self._load_state(run_id)
        plan = self._load_plan(run_id)
        registry = restore_plan_definition_registry(plan)
        resumed = resume_waiting_input(
            plan,
            state,
            input_id=input_id,
            values=values,
            contracts=_contracts(registry),
        )
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self._accepted(run_id, self._execute(plan, resumed, registry)),
        )

    async def resume(self, command_id: UUID, run_id: str) -> dict[str, Any]:
        """Continue an unfinished Run.

        When the definitions changed since the Run was compiled, the Run is
        executed again in place under the current plan (see ``rerun``);
        otherwise it continues from its next action.
        """

        state = self._load_state(run_id)
        if state.status is WorkflowStatus.COMPLETED or self._detached.is_active(run_id):
            return {"run_id": run_id, "task_id": None}
        plan = self._load_plan(run_id)
        _registry, current = self._compile(state.workflow_id)
        if _plan_data(current) != _plan_data(plan):
            return await self.rerun(command_id, run_id, reason="definitions changed")
        if state.status is WorkflowStatus.WAITING:
            raise CapabilityRunStateError(
                "run is waiting for input; resume it through the input endpoint"
            )
        registry = restore_plan_definition_registry(plan)
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self._accepted(run_id, self._execute(plan, state, registry)),
        )

    async def rerun(
        self,
        command_id: UUID,
        run_id: str,
        values: Any = None,
        *,
        reason: str = "rerun",
    ) -> dict[str, Any]:
        """Execute the Run again in place, as a new version under the same id.

        ``values`` carries a revision for the Capability's ``rerun_input``;
        without one the Run starts again from its saved start input.  Model
        turns whose context did not change are replayed from the turn memo.
        """

        del command_id
        state = self._load_state(run_id)
        if self._detached.is_active(run_id):
            raise CapabilityRunStateError("run is still executing")
        if values is not None and self._rerun_input is None:
            raise CapabilityRunInputError(f"{self.capability_id} takes no revision input")
        registry, plan = self._compile(state.workflow_id)
        contracts = _contracts(registry)
        self._tools(plan, contracts, None)
        previous = self._previous_start_input(run_id, state)
        archive = self._archive_version(run_id, state, reason=reason)
        try:
            if self._rerun_input is not None:
                initial = self._rerun_input(
                    run_id, state.workflow_id, values, previous, archive
                )
            else:
                initial = previous
        except ValueError as exc:
            self._restore_version(run_id, archive)
            raise CapabilityRunInputError(str(exc)) from exc
        except BaseException:
            self._restore_version(run_id, archive)
            raise
        fresh = WorkflowState.for_plan(run_id, plan, initial_variables=initial)
        self._store.save_plan(run_id, plan)
        self._save_start_input(run_id, initial)
        return await self._detached.accept_after_persisted_state(
            run_id=run_id,
            state_store=self._store,
            operation=self._accepted(run_id, self._execute(plan, fresh, registry)),
        )

    def versions(self, run_id: str) -> list[dict[str, Any]]:
        """Archived versions of one Run, oldest first."""

        self._load_state(run_id)
        root = self._run_dir(run_id) / "versions"
        rows: list[dict[str, Any]] = []
        for path in sorted(root.glob("v*/version.json"), key=_version_order):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return rows

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id:
            raise CapabilityRunNotFoundError(run_id)
        return self.workspace / "Work" / "runs" / run_id

    def _save_start_input(self, run_id: str, initial: Mapping[str, Any]) -> None:
        path = self._run_dir(run_id) / START_INPUT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(_json_value(dict(initial)), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _previous_start_input(self, run_id: str, state: WorkflowState) -> dict[str, Any]:
        path = self._run_dir(run_id) / START_INPUT_FILE
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        if self._rerun_input is None:
            raise CapabilityRunStateError(
                f"run {run_id} has no saved start input to run again from"
            )
        # A Run started before start input was saved: the Capability picks
        # its start input out of the last state's variables.
        return _json_value(state.variables)

    def _archive_version(self, run_id: str, state: WorkflowState, *, reason: str) -> Path:
        run_dir = self._run_dir(run_id)
        versions = run_dir / "versions"
        number = 1 + max(
            (_version_order(path) for path in versions.glob("v*")), default=0
        )
        target = versions / f"v{number}"
        target.mkdir(parents=True)
        for entry in sorted(run_dir.iterdir()):
            if entry.name in self._kept_on_rerun:
                continue
            shutil.move(str(entry), str(target / entry.name))
        (target / "version.json").write_text(
            json.dumps(
                {
                    "version": number,
                    "workflow_id": state.workflow_id,
                    "status": state.status.value,
                    "archived_at": datetime.now(UTC).isoformat(),
                    "reason": reason,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return target

    def _restore_version(self, run_id: str, archive: Path) -> None:
        run_dir = self._run_dir(run_id)
        for entry in sorted(archive.iterdir()):
            if entry.name == "version.json":
                continue
            destination = run_dir / entry.name
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(entry), str(destination))
        shutil.rmtree(archive)

    def cancel(self, run_id: str) -> Awaitable[Any] | None:
        self._load_state(run_id)
        return self._detached.cancel(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        waiting_input = [state.waiting_input] if state.waiting_input is not None else []
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": state.workflow_id,
                "status": state.status.value,
                "active": self._detached.is_active(run_id),
                "task_id": None,
            },
            "state": state.model_dump(mode="json"),
            "waiting_input": waiting_input,
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        outputs: list[dict[str, Any]] = []
        for output_id, stored in state.outputs.items():
            value = stored.model_dump(mode="json") if hasattr(stored, "model_dump") else stored
            outputs.append({"id": output_id, "kind": "value", "value": value})
            artifacts = value.get("output_artifacts") if isinstance(value, Mapping) else None
            for artifact in artifacts if isinstance(artifacts, list) else ():
                path = str(artifact.get("path", "")) if isinstance(artifact, Mapping) else ""
                if path:
                    outputs.append(self._artifact_output(path))
        return {"run_id": run_id, "outputs": outputs}

    def _artifact_output(self, path: str) -> dict[str, Any]:
        target = Path(path)
        target = target if target.is_absolute() else self.workspace / target
        exists = target.is_file()
        return {
            "id": path,
            "kind": "artifact",
            "path": path,
            "exists": exists,
            "size": target.stat().st_size if exists else 0,
        }

    def get_cost(self, run_id: str) -> dict[str, Any]:
        self._load_state(run_id)
        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    async def close(self) -> None:
        await self._detached.close()

    def _compile(self, workflow_id: str) -> tuple[DefinitionRegistry, ResolvedPlan]:
        if workflow_id not in self.entrypoints:
            raise ValueError(f"workflow is not runnable: {workflow_id}")
        _capability, registry = load_capability(self.capability_file)
        workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"definition is not a workflow: {workflow_id}")
        return registry, WorkflowCompiler(self._executors).compile(workflow, registry)

    async def _execute(
        self,
        plan: ResolvedPlan,
        state: WorkflowState,
        registry: DefinitionRegistry,
    ) -> WorkflowState:
        contracts = _contracts(registry)
        run_id = state.run_id
        tools = self._tools(plan, contracts, run_id)
        factory = self._tool_factory(contracts, run_id)
        agent_ids = _agent_ids(plan)
        invoker = None
        if agent_ids:
            if self.services is None or self._execution is None:
                raise CapabilityRunStateError(
                    f"{self.capability_id} needs model services to run its Agents"
                )
            invoker = DefinitionAgentInvoker(
                self.services,
                workflow_id=f"{self.capability_id}:{run_id}",
                run_id=run_id,
                definitions=registry,
                contracts=contracts,
                tool_factory=factory.build,
                execution=self._execution,
                **(
                    {"loop_builder": self._loop_builder}
                    if self._loop_builder is not None
                    else {}
                ),
            )
        try:
            return await WorkflowRuntimeHost(
                self._executors,
                self._store,
                FileWorkflowEventSink(self.workspace),
            ).execute(
                plan,
                state,
                RuntimeContext(
                    tools=tools,
                    contracts=contracts,
                    agents={agent_id: invoker for agent_id in agent_ids},
                    definitions=registry,
                    conversations=ConversationRegistry(
                        FileConversationStore(self.workspace)
                    ),
                    plan_tool_factory=lambda definition, plan_contracts: (
                        self._tool_factory(plan_contracts, run_id).build(definition)
                    ),
                ),
            )
        finally:
            if invoker is not None:
                await invoker.close()
            await self._settled(run_id)

    @staticmethod
    async def _accepted(run_id: str, execution: Awaitable[Any]) -> dict[str, Any]:
        await execution
        return {"run_id": run_id, "task_id": None}

    def _tool_factory(
        self,
        contracts: Mapping[str, ContractAdapter],
        run_id: str | None,
    ) -> CapabilityToolAdapterFactory:
        return CapabilityToolAdapterFactory(
            self.capability_id,
            self.tool_implementations,
            contracts,
            RunToolResultIndex(self.workspace, run_id) if run_id is not None else None,
        )

    def _tools(
        self,
        plan: ResolvedPlan,
        contracts: Mapping[str, ContractAdapter],
        run_id: str | None,
    ) -> dict[str, CapabilityToolAdapter]:
        """Bind exactly the Tools the frozen plan and its children reference."""

        registry = restore_plan_definition_registry(plan)
        factory = self._tool_factory(contracts, run_id)
        tools: dict[str, CapabilityToolAdapter] = {}
        for tool_id in _plan_tool_ids(plan):
            definition = registry.require(DefinitionKind.TOOL, tool_id)
            if isinstance(definition, ToolDefinition):
                tools[tool_id] = factory.build(definition)
        return tools

    def _load_state(self, run_id: str) -> WorkflowState:
        try:
            state = self._store.load(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        if state.workflow_id not in self.entrypoints:
            raise CapabilityRunNotFoundError(run_id)
        return state

    def _load_plan(self, run_id: str) -> ResolvedPlan:
        try:
            return self._store.load_plan(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc


def _contracts(registry: DefinitionRegistry) -> dict[str, ContractAdapter]:
    return {
        definition.id: build_contract_adapter(definition)
        for definition in registry.all(DefinitionKind.CONTRACT)
        if isinstance(definition, ContractDefinition)
    }


def _plan_data(plan: ResolvedPlan) -> Any:
    return plan.model_dump(mode="json")


def _version_order(path: Path) -> int:
    for name in (path.name, path.parent.name):
        if name.startswith("v") and name[1:].isdigit():
            return int(name[1:])
    return 0


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        value = {key: _json_value(item) for key, item in value.items()}
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _plan_tool_ids(plan: ResolvedPlan) -> list[str]:
    tool_ids: list[str] = []
    for candidate in (plan, *plan.subworkflow_plans.values()):
        for tool_id in candidate.tool_ids:
            if tool_id not in tool_ids:
                tool_ids.append(tool_id)
    return tool_ids


def _agent_ids(plan: ResolvedPlan) -> list[str]:
    agent_ids: list[str] = []
    pending = [plan]
    while pending:
        current = pending.pop()
        for agent_id in current.agent_ids:
            if agent_id not in agent_ids:
                agent_ids.append(agent_id)
        pending.extend(current.subworkflow_plans.values())
    return agent_ids


__all__ = [
    "GenericCapabilityRuntimeBinding",
    "RerunInput",
    "SettledListener",
    "StartInput",
    "ToolImplementations",
]
