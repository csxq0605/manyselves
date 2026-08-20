"""Reporting-owned declarative cohort for the five module lanes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    ControlFlowWorkflowExecutor,
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvoker, WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus

from .agentic_models import ModuleSubmission
from .declarative_module_lane import (
    ModuleSubjectValidator,
    execute_declarative_module_lane,
)
from .taxonomy import REPORT_TAXONOMY


class DeclarativeModuleLaneOutcome(BaseModel):
    """Business outcome joined after every sibling branch has drained."""

    model_config = ConfigDict(extra="forbid")

    module_id: str
    status: Literal["completed", "failed"]
    module: ModuleSubmission | None = None
    error: str | None = None


class DeclarativeModuleCohortError(RuntimeError):
    """Raised after the joined cohort contains one or more failed lanes."""


def build_module_cohort_definition(
    *,
    max_concurrency: int | None,
) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition]:
    """Build the fixed five-branch Reporting workflow and its contracts."""

    registry = DefinitionRegistry()
    contracts: dict[str, ContractAdapter] = {}
    for definition in (
        ContractDefinition(
            id="module_submission",
            version="1.0.0",
            description="Current Reporting module submission",
            adapter="pydantic",
            model="manyselves.core.reporting.agentic_models:ModuleSubmission",
        ),
        ContractDefinition(
            id="declarative_module_lane_outcome",
            version="1.0.0",
            description="One drained declarative module lane outcome",
            adapter="pydantic",
            model=(
                "manyselves.core.reporting.declarative_module_cohort:"
                "DeclarativeModuleLaneOutcome"
            ),
        ),
        ContractDefinition(
            id="declarative_module_cohort_output",
            version="1.0.0",
            description="Joined module lane outcomes",
            adapter="json_schema",
            schema={"type": "object"},
        ),
    ):
        registry.register(definition)
        contracts[definition.id] = build_contract_adapter(definition)

    module_ids = tuple(REPORT_TAXONOMY)
    for module_id in module_ids:
        registry.register(
            ToolDefinition(
                id=f"execute-module-lane-{module_id}",
                version="1.0.0",
                description=f"Execute or reuse Reporting module lane {module_id}",
                implementation=f"capability:module-lane-{module_id}",
                input_contract="module_submission",
                output_contract="declarative_module_lane_outcome",
                side_effect="ordered_state",
                parallel_safe=True,
            )
        )
    registry.register(
        ToolDefinition(
            id="reduce-module-cohort",
            version="1.0.0",
            description="Publish modules only when all five joined lanes completed",
            implementation="capability:reduce-module-cohort",
            input_contract="declarative_module_cohort_output",
            output_contract="declarative_module_cohort_output",
            side_effect="pure_read",
            parallel_safe=True,
        )
    )

    branches = {
        module_id: f"execute-module-{module_id}"
        for module_id in module_ids
    }
    actions: list[dict[str, Any]] = [
        {
            "id": "module-cohort",
            "kind": "parallel",
            "branches": branches,
            "join": "join-module-cohort",
            "max_concurrency": max_concurrency,
        }
    ]
    for module_id in module_ids:
        actions.extend(
            [
                {
                    "id": f"execute-module-{module_id}",
                    "kind": "invoke_tool",
                    "tool": f"execute-module-lane-{module_id}",
                    "input_variable": f"module-{module_id}",
                    "output_variable": f"outcome-{module_id}",
                },
                {
                    "id": f"complete-module-{module_id}-branch",
                    "kind": "goto",
                    "target": "join-module-cohort",
                },
            ]
        )
    actions.extend(
        [
            {
                "id": "join-module-cohort",
                "kind": "join",
                "parallel": "module-cohort",
                "inputs": {
                    module_id: f"outcome-{module_id}"
                    for module_id in module_ids
                },
                "output_variable": "module-outcomes",
            },
            {
                "id": "reduce-module-cohort",
                "kind": "invoke_tool",
                "tool": "reduce-module-cohort",
                "input_variable": "module-outcomes",
                "output_variable": "completed-modules",
            },
            {
                "id": "finish-module-cohort",
                "kind": "end_workflow",
                "output_variable": "completed-modules",
                "output_name": "result",
            },
        ]
    )
    workflow = WorkflowDefinition(
        id="distribution-module-cohort",
        version="1.0.0",
        description="Join the five independent Reporting module lanes",
        output_contract="declarative_module_cohort_output",
        state={},
        actions=actions,
    )
    return registry, contracts, workflow


async def execute_declarative_module_cohort(
    *,
    run_id: str,
    workflow_id: str,
    modules: Mapping[str, ModuleSubmission],
    initial_scopes: Mapping[str, set[str]],
    lane_agent_invokers: Mapping[str, Mapping[str, AgentInvoker]],
    validate_subject: ModuleSubjectValidator,
    state_store: WorkflowStateStore,
    max_concurrency: int | None = None,
) -> dict[str, ModuleSubmission]:
    """Run and drain five branches, then publish only an all-complete Join."""

    definitions, contracts, workflow = build_module_cohort_definition(
        max_concurrency=max_concurrency,
    )
    module_ids = tuple(REPORT_TAXONOMY)
    workflow.state = {
        f"module-{module_id}": modules[module_id]
        for module_id in module_ids
    }
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)

    def lane_tool(module_id: str):
        async def execute(module: Any) -> DeclarativeModuleLaneOutcome:
            try:
                result = await execute_declarative_module_lane(
                    run_id=run_id,
                    workflow_id=f"{workflow_id}--module-{module_id}",
                    module=ModuleSubmission.model_validate(module),
                    initial_scope=initial_scopes[module_id],
                    lifecycle_id="initial",
                    agent_invokers=lane_agent_invokers[module_id],
                    validate_subject=validate_subject,
                    state_store=state_store,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return DeclarativeModuleLaneOutcome(
                    module_id=module_id,
                    status="failed",
                    error=str(exc),
                )
            return DeclarativeModuleLaneOutcome(
                module_id=module_id,
                status="completed",
                module=result,
            )

        return execute

    tools = {
        f"execute-module-lane-{module_id}": lane_tool(module_id)
        for module_id in module_ids
    }
    tools["reduce-module-cohort"] = _reduce_module_cohort
    completed = await ControlFlowWorkflowExecutor(executors, state_store).execute(
        plan,
        WorkflowState.for_plan(f"{run_id}--module-cohort", plan),
        RuntimeContext(tools=tools, contracts=contracts, definitions=definitions),
    )
    if completed.status is not WorkflowStatus.COMPLETED:
        raise DeclarativeModuleCohortError("declarative module cohort did not complete")

    return {
        module_id: ModuleSubmission.model_validate(value)
        for module_id, value in completed.outputs["result"].items()
    }


def _reduce_module_cohort(values: Mapping[str, Any]) -> dict[str, ModuleSubmission]:
    module_ids = tuple(REPORT_TAXONOMY)
    outcomes = {
        module_id: DeclarativeModuleLaneOutcome.model_validate(values[module_id])
        for module_id in module_ids
    }
    failures = {
        module_id: outcome.error or "module lane failed"
        for module_id, outcome in outcomes.items()
        if outcome.status == "failed"
    }
    if failures:
        raise DeclarativeModuleCohortError(
            "; ".join(
                f"{module_id}: {failures[module_id]}"
                for module_id in module_ids
                if module_id in failures
            )
        )
    return {
        module_id: ModuleSubmission.model_validate(outcomes[module_id].module)
        for module_id in module_ids
    }
