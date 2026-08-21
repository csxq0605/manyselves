"""Reporting-owned declarations for the five Cross owner pipelines."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import ExecutorRegistry
from manyselves.kernel.workflow import (
    ResolvedPlan,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    retry_parallel_branches,
)

from .agentic_models import ModuleSubmission
from .models import REPORT_MODULE_IDS
from .review_lifecycle import CrossReviewCoordinator


class DeclarativeCrossOwnerPipelineOutcome(BaseModel):
    """Serializable result retained after one Cross owner branch drains."""

    model_config = ConfigDict(extra="forbid")

    owner_module_id: str
    status: Literal["completed", "failed"]
    pipeline: dict[str, Any] | None = None
    error: str | None = None


def register_cross_owner_pipeline_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register five statically bound owner-pipeline workflow definitions."""

    template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-pipeline",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-cross-owner-pipeline is not a workflow")
    workflows: dict[str, WorkflowDefinition] = {}
    for module_id in REPORT_MODULE_IDS:
        workflow_id = f"distribution-cross-owner-{module_id}-pipeline"
        registered = definitions.get(DefinitionKind.WORKFLOW, workflow_id)
        if registered is not None:
            if not isinstance(registered, WorkflowDefinition):
                raise TypeError(f"{workflow_id} is not a workflow")
            workflows[workflow_id] = registered
            continue
        workflow = specialize_workflow(
            template,
            {"owner_module_id": module_id},
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


def compile_cross_owner_workflows(
    definitions: DefinitionRegistry,
    executors: ExecutorRegistry,
) -> tuple[ResolvedPlan, dict[str, ResolvedPlan]]:
    """Compile the packaged Cross Cohort and its five owner pipelines."""

    cohort = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-cohort",
    )
    if not isinstance(cohort, WorkflowDefinition):
        raise TypeError("distribution-cross-owner-cohort is not a workflow")
    pipelines = {
        workflow_id: WorkflowCompiler(executors).compile(workflow, definitions)
        for workflow_id, workflow in register_cross_owner_pipeline_specializations(
            definitions
        ).items()
    }
    return WorkflowCompiler(executors).compile(cohort, definitions), pipelines


class DeclarativeCrossOwnerRuntime:
    """Bind file-defined Cross actions to the existing Capability lifecycle."""

    def __init__(
        self,
        runner: Any,
        state: dict[str, Any],
        workflow_id: str,
        *,
        compatibility_cross: Any | None = None,
    ) -> None:
        self.current_state = state
        self._runner = runner
        self._workflow_id = workflow_id
        self._compatibility_cross = compatibility_cross
        self._current_runner = getattr(runner, "_runner", runner)
        self._production = hasattr(self._current_runner, "service") and callable(
            getattr(self._current_runner, "_agent", None)
        )
        self._coordinator = (
            CrossReviewCoordinator(
                self._current_runner,
                state,
                workflow_id,
            )
            if self._production
            else None
        )
        self._aggregate_recovered = False
        self._compatibility_invoked = False

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        """Prepare the frozen owner inputs before the declared Parallel."""

        self.current_state = state
        _restore_modules(self.current_state)
        if self._production:
            self._coordinator = CrossReviewCoordinator(
                self._current_runner,
                self.current_state,
                self._workflow_id,
            )
        if self._coordinator is not None:
            self._aggregate_recovered = self._coordinator.prepare()
        return deepcopy(self.current_state)

    async def execute_owner(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeCrossOwnerPipelineOutcome:
        """Execute and drain one owner without mutating sibling branch state."""

        owner_module_id = str(values["owner_module_id"])
        if self._aggregate_recovered:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=owner_module_id,
                status="completed",
                pipeline={},
            )
        if self._coordinator is None:
            try:
                if (
                    owner_module_id == REPORT_MODULE_IDS[0]
                    and "cross_review_completion_ref" not in self.current_state
                ):
                    if self._compatibility_cross is None:
                        result = self._runner._cross_review(
                            self.current_state,
                            self._workflow_id,
                        )
                    else:
                        result = self._compatibility_cross(self.current_state)
                    if hasattr(result, "__await__"):
                        await result
                    self._compatibility_invoked = True
            except BaseException as exc:
                return DeclarativeCrossOwnerPipelineOutcome(
                    owner_module_id=owner_module_id,
                    status="failed",
                    error=str(exc),
                )
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=owner_module_id,
                status="completed",
                pipeline={"compatibility": True},
            )
        try:
            pipeline = await self._coordinator.run_owner(owner_module_id)
        except BaseException as exc:
            return DeclarativeCrossOwnerPipelineOutcome(
                owner_module_id=owner_module_id,
                status="failed",
                error=str(exc),
            )
        return DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=owner_module_id,
            status="completed",
            pipeline=pipeline.model_dump(mode="json"),
        )

    async def reduce(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Finalize the five drained outcomes and publish the shared state."""

        outcomes = {
            module_id: DeclarativeCrossOwnerPipelineOutcome.model_validate(outcome)
            for module_id, outcome in dict(values["outcomes"]).items()
        }
        failures = {
            module_id: outcome.error or "Cross owner pipeline failed"
            for module_id, outcome in outcomes.items()
            if outcome.status == "failed"
        }
        if self._coordinator is not None:
            reduced = {
                module_id: (
                    RuntimeError(failures[module_id])
                    if module_id in failures
                    else outcomes[module_id].pipeline
                )
                for module_id in REPORT_MODULE_IDS
            }
            await self._coordinator.finalize(reduced)
        elif failures:
            first = min(failures, key=float)
            raise RuntimeError(failures[first])
        return deepcopy(self.current_state)


def retry_failed_cross_owner_pipelines(
    plan: ResolvedPlan,
    state: WorkflowState,
) -> WorkflowState:
    """Retry only owner branches whose typed outcome failed."""

    if state.status is not WorkflowStatus.FAILED:
        return state
    branches = state.parallel_results.get("cross-owner-cohort", {})
    failed = {
        module_id
        for module_id in REPORT_MODULE_IDS
        if module_id in branches
        and DeclarativeCrossOwnerPipelineOutcome.model_validate(
            branches[module_id][f"outcome-{module_id}"]
        ).status
        == "failed"
    }
    if not failed:
        return state
    return retry_parallel_branches(
        plan,
        state,
        parallel_action_id="cross-owner-cohort",
        branch_ids=failed,
    )


def _restore_modules(state: dict[str, Any]) -> None:
    modules = state.get("module_submissions")
    if not isinstance(modules, Mapping):
        return
    state["module_submissions"] = {
        module_id: ModuleSubmission.model_validate(value) for module_id, value in modules.items()
    }


__all__ = [
    "DeclarativeCrossOwnerRuntime",
    "DeclarativeCrossOwnerPipelineOutcome",
    "compile_cross_owner_workflows",
    "register_cross_owner_pipeline_specializations",
    "retry_failed_cross_owner_pipelines",
]
