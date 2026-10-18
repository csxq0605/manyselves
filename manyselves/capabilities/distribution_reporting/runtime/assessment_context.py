"""Carry author judgments through existing reporting tasks, without judging them.

Coverage here describes explicit records and exact prose anchors. It is neither
a semantic pass/fail decision nor a new workflow gate. In particular, an author
can declare a wrong judgment and the existing reviewer must still examine it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models.agentic import ModuleSubmission, ObservationAssessment, PartAssessment
from .research.project_evidence import ProjectEvidenceIndex


def part_assessment(
    workspace: Path, run_id: str, module_id: str, part_id: str, binding: dict,
) -> PartAssessment:
    index = ProjectEvidenceIndex(workspace, run_id)
    return PartAssessment(
        expected_evidence_ids=(
            [item.id for item in index.module_items(module_id, {part_id})]
            if index.path.is_file() else None
        ),
        records=binding.get("assessments"),
    )


def assessment_view(part: PartAssessment, prose: str) -> dict:
    """Expose omissions without turning citation presence into semantic coverage."""
    records = part.records or []
    assessed = {eid for record in records for eid in record.evidence_ids}
    narrative_missing: set[str] = set()
    action_missing: set[str] = set()
    unverified_rules: set[str] = set()
    for record in records:
        if not record.narrative_anchor.strip() or record.narrative_anchor not in prose:
            narrative_missing.update(record.evidence_ids)
        has_action = bool(record.action_anchor.strip() and record.action_anchor in prose)
        if not has_action and not record.no_action_reason.strip():
            action_missing.update(record.evidence_ids)
        if any(
            rule.verification != "verified" or not rule.status_source_id or not rule.checked_at
            for rule in record.rules
        ):
            unverified_rules.update(record.evidence_ids)
    return {
        **part.model_dump(mode="json"),
        "unassessed_evidence_ids": (
            sorted(set(part.expected_evidence_ids) - assessed)
            if part.expected_evidence_ids is not None else None
        ),
        "narrative_unmapped_evidence_ids": sorted(narrative_missing),
        "action_undocumented_evidence_ids": sorted(action_missing),
        "unverified_rule_evidence_ids": sorted(unverified_rules),
    }


def module_assessment_views(subject: ModuleSubmission, scope: set[str] | None = None) -> dict:
    return {
        part_id: assessment_view(subject.assessments.get(part_id, PartAssessment()), prose)
        for part_id, prose in subject.submodule_narratives.items()
        if scope is None or part_id in scope
    }


def assessment_source_context(modules: dict) -> str:
    """Shared Chief/Final handoff; no second model-generated assessment copy."""
    return json.dumps({
        str(module_id): module_assessment_views(
            module if isinstance(module, ModuleSubmission) else ModuleSubmission.model_validate(module)
        )
        for module_id, module in modules.items()
    }, ensure_ascii=False, separators=(",", ":"))


def normalize_assessments(
    value: list[dict] | None, evidence_ids: list[str] | None, known_sources: set[str],
) -> list[dict] | None:
    """Use the existing tool's schema/source correction boundary only."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("assessments must be a list")
    records = [ObservationAssessment.model_validate(item) for item in value]
    for record in records:
        if set(record.evidence_ids) - set(evidence_ids or []):
            raise ValueError("assessments.evidence_ids must be bound by this part's evidence_ids")
        for rule in record.rules:
            refs = {rule.source_id, rule.status_source_id} - {None}
            if refs - known_sources:
                raise ValueError("assessments.rules reference unregistered current-run sources")
    return [record.model_dump(mode="json", exclude_unset=True) for record in records]
