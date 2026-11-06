"""Run ``render-existing`` on the generic Runtime binding.

The Capability supplies the input projection and the two program steps
(``render_existing``); starting, resuming, projecting the Run and deciding,
on resume, whether to continue or execute the Run again in place under its
current definitions and code are the generic binding's job.  The render's
frozen source stays with the Run, so a Run executed again renders the same
Markdown.  Its result names the Word file in ``output_ref``; the outputs
list that file as an artifact.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

from .. import CAPABILITY_FILE
from .models.entrypoint import PublicRenderExistingRequest
from .render_existing import RenderExistingRequest, build_render_existing_tool_implementations

# The variables render-existing.yaml starts from.
_REQUEST = "render-request"
_RUN_ID = "run-id"
# The source the run froze stays with it when it is executed again.
FROZEN_INPUTS = ("input-snapshot.json", "frozen-project")


def render_existing_start_input(run_id: str, workflow_id: str, values: Any) -> dict[str, Any]:
    del workflow_id
    public = PublicRenderExistingRequest.model_validate(values)
    request = RenderExistingRequest.model_validate(public.model_dump(mode="python"))
    return {_REQUEST: request, _RUN_ID: run_id}


def render_existing_rerun_input(
    run_id: str,
    workflow_id: str,
    values: Any,
    previous: dict[str, Any],
    archive: Path,
) -> dict[str, Any]:
    """The same request again; a render takes no revision.

    ``previous`` is the saved start input, or the last state's variables of
    a Run started before start input was saved.
    """

    del workflow_id, archive
    if values is not None:
        raise ValueError("render-existing takes no revision input")
    request = previous.get(_REQUEST)
    if not isinstance(request, Mapping):
        raise ValueError(f"run {run_id} has no saved render request")
    return {
        _REQUEST: RenderExistingRequest.model_validate(dict(request)),
        _RUN_ID: run_id,
    }


class RenderExistingBinding(GenericCapabilityRuntimeBinding):
    """The generic binding; the render result's ``output_ref`` is its artifact."""

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        projected = super().get_outputs(run_id)
        outputs: list[dict[str, Any]] = []
        for output in projected["outputs"]:
            outputs.append(output)
            value = output.get("value")
            if output.get("kind") == "value" and isinstance(value, Mapping):
                if value.get("output_ref"):
                    outputs.append(self._artifact_output(str(value["output_ref"])))
        return {**projected, "outputs": outputs}


def build_render_existing_binding(
    workspace: Path,
    services: Any,
    *,
    execution: Any | None = None,
) -> RenderExistingBinding:
    return RenderExistingBinding(
        capability_file=CAPABILITY_FILE,
        workspace=workspace,
        services=services,
        tool_implementations=build_render_existing_tool_implementations(Path(workspace)),
        start_input=render_existing_start_input,
        execution=execution,
        rerun_input=render_existing_rerun_input,
        frozen_inputs=FROZEN_INPUTS,
    )


__all__ = [
    "FROZEN_INPUTS",
    "RenderExistingBinding",
    "build_render_existing_binding",
    "render_existing_rerun_input",
    "render_existing_start_input",
]
