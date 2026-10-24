"""Shared fixtures for the report capability tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TemplateSkillBoundaryManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.template_skills import skill_order


def write_role_skill_fixture(workspace: Path) -> None:
    """Publish one fact-free Skill for every declared role, as a project would."""

    root = workspace / "Inputs/report-template-role-skills"
    boundary = TemplateSkillBoundaryManifest(
        transferred_categories=[
            "analysis_method",
            "synthesis_method",
            "visual_method",
            "quality_check",
        ],
        excluded_categories=[
            "domain_knowledge",
            "domain_standard_or_threshold",
            "project_fact_or_number",
            "customer_identity",
            "project_finding_or_risk",
            "project_conclusion_or_recommendation",
            "evidence_or_claim_identifier",
        ],
        boundary_statement=(
            "Only reusable reporting methods and fact-free structural examples may be "
            "transferred; every project fact, conclusion, threshold, and evidence identity "
            "must remain scoped to the current run inputs."
        ),
    )
    for skill_id in skill_order():
        path = root / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{skill_id}: preserve typed evidence boundaries.\n", encoding="utf-8")
    (root / "boundary.json").write_text(boundary.model_dump_json(), encoding="utf-8")


def write_facts(workspace: Path, items: list[EvidenceItem], run_id: str = "facts") -> dict[str, Any]:
    """Write a prepared run whose evidence file holds ``items``."""

    write_role_skill_fixture(workspace)
    store = ReportingStore(workspace)
    refs = {
        "coverage": f"Work/runs/{run_id}/preparation/coverage.json",
        "evidence": f"Work/runs/{run_id}/preparation/evidence.jsonl",
        "manifest": f"Work/runs/{run_id}/preparation/manifest.json",
    }
    for name, ref in refs.items():
        store.write_text(ref, f"{name}-{run_id}\n")
    store.write_text(f"Work/runs/{run_id}/preparation/completion.json", "completion\n")
    (workspace / refs["evidence"]).write_text(
        "\n".join(item.model_dump_json() for item in items) + "\n", encoding="utf-8"
    )
    return {
        "run_id": run_id,
        "resume": False,
        "request": ReportRequest(instruction="完成报告"),
        "preparation_refs": refs,
        "preparation_completion_ref": f"Work/runs/{run_id}/preparation/completion.json",
        "evidence_index_ref": f"Work/runs/{run_id}/preparation/evidence-index.json",
    }
