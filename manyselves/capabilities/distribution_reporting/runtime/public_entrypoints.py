"""Pure projections from public entrypoint values to internal task inputs."""

from __future__ import annotations

from typing import Any

from .models.entrypoint import PublicFullReportRequest, PublicRenderExistingRequest
from .models.reporting import ReportRequest


def project_public_entrypoint_input(
    workflow_id: str,
    run_id: str,
    value: Any,
) -> ReportRequest:
    """Project one user-facing input after the Host has allocated ``run_id``.

    The returned value is the Capability's typed report request.  This helper
    does not allocate state, call a Provider, or choose a workflow; it only
    adds the root operation that is not a user input.
    """

    del run_id
    if workflow_id == "full-report":
        public = PublicFullReportRequest.model_validate(value)
        payload = public.model_dump(mode="python")
        payload["operation"] = "full_report"
        return ReportRequest.model_validate(payload)
    if workflow_id == "render-existing":
        public = PublicRenderExistingRequest.model_validate(value)
        payload = public.model_dump(mode="python")
        payload["operation"] = "render_existing"
        return ReportRequest.model_validate(payload)
    raise ValueError(f"unsupported public Reporting workflow: {workflow_id}")


__all__ = ["project_public_entrypoint_input"]
