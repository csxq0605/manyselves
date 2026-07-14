from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pds_report.agents.base import AgentContext, AgentPort
from pds_report.domain.contracts import CARRIER_CONTRACTS
from pds_report.domain.models import RunStatus
from pds_report.workflow.config import (
    AgentDefinition,
    PhaseDefinition,
    WorkflowDefinition,
)
from pds_report.workflow.events import Event, EventType


class WorkflowExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    state: dict[str, object]
    errors: list[str] = field(default_factory=list)
    tasks: list[dict[str, object]] = field(default_factory=list)


class WorkflowRunner:
    async def run(
        self,
        workflow: WorkflowDefinition,
        definitions: Mapping[str, AgentDefinition],
        agents: Mapping[str, AgentPort],
        context: AgentContext,
    ) -> RunResult:
        ordered_phases = self._ordered_phases(workflow)
        task_ids = self._create_tasks(ordered_phases, context)
        errors: list[str] = []
        await context.bus.publish(
            Event(type=EventType.RUN_STARTED, source="workflow-runner", run_id=context.run_id)
        )

        for phase in ordered_phases:
            await context.bus.publish(
                Event(
                    type=EventType.PHASE_STARTED,
                    source=phase.id,
                    run_id=context.run_id,
                )
            )
            try:
                if phase.mode == "pipeline":
                    await self._run_pipeline(
                        phase,
                        definitions,
                        agents,
                        context,
                        task_ids,
                    )
                else:
                    await self._run_parallel(
                        phase,
                        definitions,
                        agents,
                        context,
                        task_ids,
                    )
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                errors.append(message)
                for task_id in task_ids[phase.id]:
                    context.task_board.skip_dependents(task_id)
                await context.bus.publish(
                    Event(
                        type=EventType.PHASE_FAILED,
                        source=phase.id,
                        run_id=context.run_id,
                        payload={"error": message},
                    )
                )
                await context.bus.publish(
                    Event(
                        type=EventType.RUN_FAILED,
                        source="workflow-runner",
                        run_id=context.run_id,
                        payload={"errors": errors},
                    )
                )
                return RunResult(
                    run_id=context.run_id,
                    status=RunStatus.FAILED,
                    state=dict(context.state),
                    errors=errors,
                    tasks=context.task_board.snapshot(),
                )

            await context.bus.publish(
                Event(
                    type=EventType.PHASE_COMPLETED,
                    source=phase.id,
                    run_id=context.run_id,
                )
            )

        await context.bus.publish(
            Event(type=EventType.RUN_COMPLETED, source="workflow-runner", run_id=context.run_id)
        )
        return RunResult(
            run_id=context.run_id,
            status=RunStatus.COMPLETED,
            state=dict(context.state),
            tasks=context.task_board.snapshot(),
        )

    def _ordered_phases(self, workflow: WorkflowDefinition) -> list[PhaseDefinition]:
        pending = list(workflow.phases)
        completed: set[str] = set()
        ordered: list[PhaseDefinition] = []
        while pending:
            ready = [phase for phase in pending if set(phase.needs) <= completed]
            if not ready:
                raise WorkflowExecutionError("workflow has unresolved phase dependencies")
            for phase in ready:
                ordered.append(phase)
                completed.add(phase.id)
                pending.remove(phase)
        return ordered

    def _create_tasks(
        self,
        phases: list[PhaseDefinition],
        context: AgentContext,
    ) -> dict[str, list[str]]:
        task_ids: dict[str, list[str]] = {}
        for phase in phases:
            phase_dependencies = [
                task_id for dependency in phase.needs for task_id in task_ids[dependency]
            ]
            task_ids[phase.id] = []
            previous_task: str | None = None
            for agent_id in phase.agents:
                task_id = f"{phase.id}:{agent_id}"
                needs = (
                    [previous_task]
                    if phase.mode == "pipeline" and previous_task
                    else phase_dependencies
                )
                context.task_board.create(task_id, agent_id, needs=needs)
                task_ids[phase.id].append(task_id)
                previous_task = task_id
        return task_ids

    async def _run_pipeline(
        self,
        phase: PhaseDefinition,
        definitions: Mapping[str, AgentDefinition],
        agents: Mapping[str, AgentPort],
        context: AgentContext,
        task_ids: dict[str, list[str]],
    ) -> None:
        for agent_id, task_id in zip(phase.agents, task_ids[phase.id], strict=True):
            output = await self._run_agent(
                agent_id,
                task_id,
                definitions,
                agents,
                context,
            )
            context.state.update(output)

    async def _run_parallel(
        self,
        phase: PhaseDefinition,
        definitions: Mapping[str, AgentDefinition],
        agents: Mapping[str, AgentPort],
        context: AgentContext,
        task_ids: dict[str, list[str]],
    ) -> None:
        import asyncio

        state_snapshot = dict(context.state)
        calls = [
            self._run_agent(
                agent_id,
                task_id,
                definitions,
                agents,
                context.with_state(dict(state_snapshot)),
            )
            for agent_id, task_id in zip(phase.agents, task_ids[phase.id], strict=True)
        ]
        results = await asyncio.gather(*calls, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise errors[0]

        merged: dict[str, object] = {}
        for result in results:
            if not isinstance(result, Mapping):
                raise WorkflowExecutionError("agent returned a non-mapping result")
            conflicts = set(merged) & set(result)
            if conflicts:
                names = ", ".join(sorted(conflicts))
                raise WorkflowExecutionError(f"parallel write conflict: {names}")
            merged.update(result)
        context.state.update(merged)

    async def _run_agent(
        self,
        agent_id: str,
        task_id: str,
        definitions: Mapping[str, AgentDefinition],
        agents: Mapping[str, AgentPort],
        context: AgentContext,
    ) -> Mapping[str, object]:
        definition = definitions[agent_id]
        try:
            agent = agents[agent_id]
        except KeyError as exc:
            raise WorkflowExecutionError(f"no implementation registered for {agent_id}") from exc

        missing_inputs = set(definition.reads) - set(context.state)
        if missing_inputs:
            names = ", ".join(sorted(missing_inputs))
            context.task_board.fail(task_id, f"missing input carriers: {names}")
            raise WorkflowExecutionError(f"{agent_id} missing input carriers: {names}")

        context.task_board.start(task_id)
        await context.bus.publish(
            Event(type=EventType.TASK_UPDATED, source=task_id, run_id=context.run_id)
        )
        await context.bus.publish(
            Event(type=EventType.AGENT_STARTED, source=agent_id, run_id=context.run_id)
        )
        try:
            output = await agent.run(context)
            if not isinstance(output, Mapping):
                raise WorkflowExecutionError(f"{agent_id} returned a non-mapping result")
            undeclared = set(output) - set(definition.writes)
            if undeclared:
                names = ", ".join(sorted(undeclared))
                raise WorkflowExecutionError(f"{agent_id} wrote undeclared carriers: {names}")
            incompatible = [
                name
                for name, value in output.items()
                if not CARRIER_CONTRACTS[name].accepts(value)
            ]
            if incompatible:
                names = ", ".join(sorted(incompatible))
                raise WorkflowExecutionError(f"{names} has incompatible type")
        except Exception as exc:
            context.task_board.fail(task_id, str(exc) or type(exc).__name__)
            await context.bus.publish(
                Event(
                    type=EventType.AGENT_FAILED,
                    source=agent_id,
                    run_id=context.run_id,
                    payload={"error": str(exc) or type(exc).__name__},
                )
            )
            raise

        context.task_board.complete(task_id, result_keys=list(output))
        await context.bus.publish(
            Event(type=EventType.TASK_UPDATED, source=task_id, run_id=context.run_id)
        )
        await context.bus.publish(
            Event(
                type=EventType.AGENT_COMPLETED,
                source=agent_id,
                run_id=context.run_id,
                payload={"result_keys": list(output)},
            )
        )
        return output
