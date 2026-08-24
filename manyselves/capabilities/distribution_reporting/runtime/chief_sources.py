"""Materialize approved Chapter 2 prose for Chief source access."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def materialize_chief_module_sources(
    store: ReportingStore,
    state: Mapping[str, Any],
) -> list[str]:
    """Persist the current approved Chapter 2 bodies as readable Markdown refs."""

    run_id = str(state["run_id"])
    raw_modules = state.get("module_submissions", {})
    modules = raw_modules if isinstance(raw_modules, Mapping) else {}
    edited = state.get("edited_report")
    if isinstance(edited, Mapping):
        edited_modules = edited.get("module_narratives", {})
    else:
        edited_modules = getattr(edited, "module_narratives", {})
    narratives = edited_modules if isinstance(edited_modules, Mapping) else {}

    refs: list[str] = []
    for module_id in REPORT_MODULE_IDS:
        if module_id in modules:
            content = ModuleSubmission.model_validate(modules[module_id]).markdown
        else:
            content = str(narratives.get(module_id, ""))
        if not content.strip():
            continue
        ref = f"Work/runs/{run_id}/context/chief-source-modules/{module_id}.md"
        store.write_text(ref, content.rstrip() + "\n")
        refs.append(ref)
    return refs


__all__ = ["materialize_chief_module_sources"]
