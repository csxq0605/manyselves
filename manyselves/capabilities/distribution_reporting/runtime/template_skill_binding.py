"""Run the writing-method entrypoints on the generic Runtime.

``extract-template-skills`` distills role Skills from the report template;
``triage-writing-lessons`` turns a finished run's review findings into lesson
candidates.  Both only write under their own run directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

from .. import CAPABILITY_FILE
from .lesson_triage import LessonTriageTools
from .run_taxonomy import bind_run_taxonomy, with_project_taxonomy
from .template_skills import TemplateSkillTools

_INPUT_FIELDS = {
    "extract-template-skills": {"template_ref", "roles"},
    "triage-writing-lessons": {"source_run_id", "notes_ref"},
}


def template_skill_start_input(run_id: str, workflow_id: str, values: Any) -> dict[str, Any]:
    request = dict(values or {})
    unknown = set(request) - _INPUT_FIELDS.get(workflow_id, set())
    if unknown:
        raise ValueError("unknown template extraction input: " + ", ".join(sorted(unknown)))
    return {"request": request, "run-id": run_id}


def build_template_skill_binding(
    workspace: Path,
    services: Any,
    *,
    loop_builder: Any | None = None,
    execution: Any | None = None,
) -> GenericCapabilityRuntimeBinding:
    return GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=workspace,
        services=services,
        tool_implementations={
            # Skills align to the product's own sections: the project's table.
            **with_project_taxonomy(
                TemplateSkillTools(Path(workspace)).implementations(), Path(workspace)
            ),
            # Triage reads the report run it is about.
            **bind_run_taxonomy(
                LessonTriageTools(Path(workspace)).implementations(), Path(workspace)
            ),
        },
        loop_builder=loop_builder,
        start_input=template_skill_start_input,
        execution=execution,
    )


__all__ = ["build_template_skill_binding", "template_skill_start_input"]
