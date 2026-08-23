"""Pure projections from public entrypoint values to internal task inputs."""

from __future__ import annotations

from typing import Any

from .models.agentic import TEMPLATE_ROLE_SKILL_IDS
from .models.aggregate_existing import AggregateExistingPreparationInput
from .models.entrypoint import (
    PublicAggregateExistingInitializerInput,
    PublicAggregateExistingRequest,
    PublicFullReportRequest,
    PublicModuleReportRequest,
    PublicRenderExistingRequest,
    PublicTemplateDistillationInitializerInput,
    PublicTemplateDistillationRequest,
)
from .models.inputs import TemplateDistillationInput
from .models.reporting import REPORT_MODULE_IDS, ReportRequest


def load_public_entrypoint_definitions():
    """Load the five roots with their packaged definition specializations.

    Parameterized child workflows are materialized before compilation.  This
    is definition composition only; no Provider, Run state, or domain
    execution is created here.
    """

    from manyselves.kernel.definitions import (
        DefinitionKind,
        DefinitionRegistry,
        specialize_workflow,
    )

    from .. import load_distribution_reporting_capability
    from .cross_owner_definitions import register_cross_owner_pipeline_specializations
    from .entrypoint_tools import specialize_module_cohort_workflow
    from .module_lane_definitions import register_module_runtime_lane_specializations
    from .reporting_tail_runtime import register_reporting_tail_workflow_specializations

    capability, source = load_distribution_reporting_capability()
    registry = DefinitionRegistry()
    for definition in source.all():
        if (
            definition.kind == DefinitionKind.WORKFLOW
            and definition.id == "distribution-module-cohort"
        ):
            continue
        registry.register(definition)
    cohort = source.require(DefinitionKind.WORKFLOW, "distribution-module-cohort")
    cohort = specialize_module_cohort_workflow(cohort, REPORT_MODULE_IDS)
    cohort = specialize_workflow(cohort, {"max_concurrency": len(REPORT_MODULE_IDS)})
    registry.register(cohort)
    register_cross_owner_pipeline_specializations(registry)
    register_module_runtime_lane_specializations(registry)
    register_reporting_tail_workflow_specializations(registry)
    return capability, registry


def project_public_entrypoint_input(
    workflow_id: str,
    run_id: str,
    value: Any,
) -> ReportRequest | AggregateExistingPreparationInput | TemplateDistillationInput:
    """Project one user-facing input after the Host has allocated ``run_id``.

    The returned values are existing Capability task contracts.  This helper
    does not allocate state, call a Provider, or choose a workflow; it only
    adds the root operation and the fixed template task fields that are not
    user inputs.
    """

    if workflow_id == "full-report":
        public = PublicFullReportRequest.model_validate(value)
        payload = public.model_dump(mode="python")
        payload["operation"] = "full_report"
        return ReportRequest.model_validate(payload)
    if workflow_id == "module-report":
        public = PublicModuleReportRequest.model_validate(value)
        payload = public.model_dump(mode="python")
        payload["operation"] = "module_report"
        return ReportRequest.model_validate(payload)
    if workflow_id == "aggregate-existing":
        public = PublicAggregateExistingRequest.model_validate(value)
        return project_public_aggregate_existing_input(
            PublicAggregateExistingInitializerInput(
                run_id=run_id,
                request=public,
            )
        )
    if workflow_id == "render-existing":
        public = PublicRenderExistingRequest.model_validate(value)
        payload = public.model_dump(mode="python")
        payload["operation"] = "render_existing"
        return ReportRequest.model_validate(payload)
    if workflow_id == "distill-template-skill":
        public = PublicTemplateDistillationRequest.model_validate(value)
        return project_public_template_distillation_input(
            PublicTemplateDistillationInitializerInput(
                run_id=run_id,
                request=public,
            )
        )
    raise ValueError(f"unsupported public Reporting workflow: {workflow_id}")


def project_public_aggregate_existing_input(
    value: PublicAggregateExistingInitializerInput | Any,
) -> AggregateExistingPreparationInput:
    """Join Host-owned Run identity to the aggregate task input."""

    initializer = PublicAggregateExistingInitializerInput.model_validate(value)
    payload = initializer.request.model_dump(mode="python")
    payload["operation"] = "aggregate_existing"
    return AggregateExistingPreparationInput(
        run_id=initializer.run_id,
        request=ReportRequest.model_validate(payload),
    )


def project_public_template_distillation_input(
    value: PublicTemplateDistillationInitializerInput | Any,
) -> TemplateDistillationInput:
    """Join Host-owned Run identity and fixed Skill ids to the template task."""

    initializer = PublicTemplateDistillationInitializerInput.model_validate(value)
    return TemplateDistillationInput(
        run_id=initializer.run_id,
        template_ref=initializer.request.template_ref,
        inspect_max_chars=initializer.request.inspect_max_chars,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )


__all__ = [
    "load_public_entrypoint_definitions",
    "project_public_aggregate_existing_input",
    "project_public_entrypoint_input",
    "project_public_template_distillation_input",
]
