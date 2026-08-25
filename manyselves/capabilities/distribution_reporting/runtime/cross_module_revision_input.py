"""Typed projection from a Cross finding into the Module Author boundary."""

from __future__ import annotations

from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
)


def project_cross_owner_module_revision_agent_input(
    value: Any,
) -> DeclarativeModuleRuntimeLaneContext:
    """Return the prepared Module Author input owned by one Cross lane."""

    context = DeclarativeCrossOwnerRuntimeContext.model_validate(value)
    preparation = context.revision_preparation
    if (
        context.status == "revision_ready"
        and preparation is not None
        and preparation.mode == "invoke_agent"
        and preparation.prepared is not None
    ):
        prepared = preparation.prepared
        return DeclarativeModuleRuntimeLaneContext(
            module_id=prepared.module_id,
            workflow_id=prepared.workflow_id,
            reporting_state={"run_id": prepared.run_id},
            status="revision_ready",
            revision=DeclarativeModuleRevisionPreparation(prepared=prepared),
            module=prepared.subject,
        )
    if context.local_module_context is not None:
        local = context.local_module_context
        if local.status == "revision_ready" and local.revision is not None:
            return local
    raise ValueError("Cross owner module revision Agent input is not prepared")
