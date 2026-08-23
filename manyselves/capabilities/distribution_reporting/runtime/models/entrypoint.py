"""Typed state shared by top-level Distribution Reporting workflows."""

from __future__ import annotations

from pydantic import Field

from .preparation import PreparationContext
from .reporting import ReportingModel, ReportRequest


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


__all__ = [
    "ReportingPreparationAttachment",
    "ReportingRunContext",
    "ReportingRunInitializerInput",
]
