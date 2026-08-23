"""Public input contracts for the file-defined Reporting entrypoints.

These models describe only values a user supplies.  Run identity and the
fixed template Skill-part list are projected by the Capability boundary after
the generic application has allocated a Run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from .agentic import TEMPLATE_ROLE_SKILL_IDS
from .preparation import PreparationContext
from .reporting import REPORT_MODULE_IDS, ReportingModel, ReportRequest


class ReportingRunContext(ReportingModel):
    """Carry one public request across file-defined Reporting subworkflows."""

    run_id: str = Field(min_length=1)
    request: ReportRequest
    preparation_context: PreparationContext


class ReportingRunInitializerInput(ReportingModel):
    """Join the Host-owned Run identity with the public request."""

    run_id: str = Field(min_length=1)
    request: ReportRequest


class ReportingPreparationAttachment(ReportingModel):
    """Join the parent context with one Preparation subworkflow result."""

    context: ReportingRunContext
    preparation: PreparationContext


def _hide_public_operation(schema: dict[str, object]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("operation", None)


class _PublicReportRequest(ReportRequest):
    """ReportRequest-compatible public shape with operation fixed by its root."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_hide_public_operation,
    )

    @classmethod
    def model_json_schema(cls, by_alias: bool = True, ref_template: str = "#/$defs/{model}", **kwargs):  # type: ignore[no-untyped-def]
        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            **kwargs,
        )
        # The root Workflow ID selects the operation.  Keep this field in the
        # typed runtime value for the existing Capability tools, but do not
        # make it part of the user-facing form.
        schema.get("properties", {}).pop("operation", None)
        return schema


class PublicFullReportRequest(_PublicReportRequest):
    """User input for the complete five-module report root."""

    operation: Literal["full_report"] = "full_report"
    target_modules: list[str] = Field(
        default_factory=lambda: list(REPORT_MODULE_IDS),
        description="All five reporting modules are included by full-report.",
    )


class PublicModuleReportRequest(_PublicReportRequest):
    """User input for a selected-module report root."""

    operation: Literal["module_report"] = "module_report"
    target_modules: list[str] = Field(
        default_factory=lambda: [REPORT_MODULE_IDS[0]],
        description="One or more reporting modules to execute.",
    )


class PublicAggregateExistingRequest(_PublicReportRequest):
    """User input for aggregating the five already-produced module reports."""

    operation: Literal["aggregate_existing"] = "aggregate_existing"
    instruction: str = "Aggregate the five existing module reports."
    target_modules: list[str] = Field(
        default_factory=lambda: list(REPORT_MODULE_IDS),
        description="Aggregate-existing always consumes all five modules.",
    )


class PublicRenderExistingRequest(_PublicReportRequest):
    """User input for rendering one existing Markdown report."""

    operation: Literal["render_existing"] = "render_existing"
    instruction: str = "Render the existing Markdown report as DOCX."
    source_markdown_ref: str = Field(
        min_length=1,
        description="Project-relative Markdown report to render.",
    )


class PublicTemplateDistillationRequest(ReportingModel):
    """User input for distilling the packaged report template."""

    template_ref: str = Field(
        min_length=1,
        description="Project-relative report template to inspect.",
    )
    inspect_max_chars: int = Field(
        default=100_000,
        ge=1,
        description="Maximum characters exposed to the one template inspection.",
    )


class PublicAggregateExistingInitializerInput(ReportingModel):
    """Host-owned Run identity paired with public aggregate input."""

    run_id: str = Field(min_length=1)
    request: PublicAggregateExistingRequest


class PublicTemplateDistillationInitializerInput(ReportingModel):
    """Host-owned Run identity paired with public template input."""

    run_id: str = Field(min_length=1)
    request: PublicTemplateDistillationRequest


__all__ = [
    "ReportingPreparationAttachment",
    "ReportingRunContext",
    "ReportingRunInitializerInput",
    "PublicAggregateExistingRequest",
    "PublicFullReportRequest",
    "PublicModuleReportRequest",
    "PublicRenderExistingRequest",
    "PublicTemplateDistillationRequest",
    "PublicAggregateExistingInitializerInput",
    "PublicTemplateDistillationInitializerInput",
    "TEMPLATE_ROLE_SKILL_IDS",
]
