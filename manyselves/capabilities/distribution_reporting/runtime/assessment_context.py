"""Carry author judgments through existing reporting tasks, without judging them.

Coverage here describes explicit records and exact prose anchors. It is neither
a semantic pass/fail decision nor a new workflow gate. In particular, an author
can declare a wrong judgment and the existing reviewer must still examine it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models.agentic import (
    EditedReportSubmission,
    ModuleSubmission,
    ObservationAssessment,
    PartAssessment,
)
from .research.project_evidence import ProjectEvidenceIndex
from .source_ledger import SourceLedger


def part_assessment(
    workspace: Path, run_id: str, module_id: str, part_id: str, binding: dict,
) -> PartAssessment:
    index = ProjectEvidenceIndex(workspace, run_id)
    part = PartAssessment(
        expected_evidence_ids=(
            [item.id for item in index.module_items(module_id, {part_id})]
            if index.path.is_file() else None
        ),
        records=binding.get("assessments"),
    )
    ledger = SourceLedger(workspace, run_id)
    source_ids = {
        source_id for record in part.records or [] for rule in record.rules
        for source_id in (rule.source_id, rule.status_source_id) if source_id
    }
    part.rule_source_refs = {
        source_id: ref.as_posix() for source_id in sorted(source_ids)
        if (ref := ledger.content_ref(source_id)) is not None
    }
    return part


def assessment_view(part: PartAssessment, prose: str) -> dict:
    """Expose omissions without turning citation presence into semantic coverage."""
    records = part.records or []
    assessed = {eid for record in records for eid in record.evidence_ids}
    narrative_missing: set[str] = set()
    action_missing: set[str] = set()
    unverified_rules: set[str] = set()
    rule_ids = {sid for record in records for rule in record.rules
                for sid in (rule.source_id, rule.status_source_id) if sid}
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
        # Omitted optional text stays omitted; the E-* records carry object/location/time.
        "records": None if part.records is None else [
            record.model_dump(mode="json", exclude_defaults=True) for record in records
        ],
        "unassessed_evidence_ids": (
            sorted(set(part.expected_evidence_ids) - assessed)
            if part.expected_evidence_ids is not None else None
        ),
        "narrative_unmapped_evidence_ids": sorted(narrative_missing),
        "action_undocumented_evidence_ids": sorted(action_missing),
        "unverified_rule_evidence_ids": sorted(unverified_rules),
        "unreadable_rule_source_ids": sorted(rule_ids - set(part.rule_source_refs)),
    }


def module_rule_source_refs(subject: ModuleSubmission, scope: set[str] | None = None) -> list[str]:
    return sorted({
        ref for part_id, part in subject.assessments.items()
        if scope is None or part_id in scope for ref in part.rule_source_refs.values()
    })


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


def source_observation_groups(workspace: Path, run_id: str) -> list[dict] | None:
    """Link routed source observations, never merge real-world objects by name.

    A shared precise source location is a comparison candidate. Only complete
    normalized payload equality (apart from routing IDs) identifies an exact
    routed copy; neither case asserts identical professional conclusions.
    """
    index = ProjectEvidenceIndex(workspace, run_id)
    if not index.path.is_file():
        return None
    groups: dict[tuple, list] = {}
    for item in index.items():
        source = item.source
        if not source.cell and not (source.row and source.column):
            continue  # A whole file/page is not a precise observation identity.
        key = (
            source.path.as_posix().replace("\\", "/"), source.sheet,
            source.cell, source.row, source.column, source.image_id,
            item.subject, item.observed_at,
        )
        groups.setdefault(key, []).append(item)
    result = []
    for items in groups.values():
        if len({(item.module_id, item.submodule_id) for item in items}) < 2:
            continue
        payloads = {
            json.dumps(item.model_dump(mode="json", exclude={"id", "module_id", "submodule_id"}), sort_keys=True)
            for item in items
        }
        result.append({
            "source": items[0].source.model_dump(mode="json"),
            "subject": items[0].subject,
            "observed_at": items[0].observed_at,
            "exact_routed_copy": len(payloads) == 1,
            "routes": [{"evidence_id": item.id, "module_id": item.module_id,
                        "submodule_id": item.submodule_id} for item in items],
        })
    return result


def observation_identities(workspace: Path, run_id: str, modules: dict[str, ModuleSubmission]) -> str:
    """Runtime-derived object/time for every judged E-ID; Final has no E-* tools."""
    index = ProjectEvidenceIndex(workspace, run_id)
    known = {item.id: item for item in index.items()} if index.path.is_file() else {}
    judged = sorted({
        eid for module in modules.values() for part in module.assessments.values()
        for record in part.records or [] for eid in record.evidence_ids if eid in known
    })
    return json.dumps({
        eid: {"subject": known[eid].subject,
              **({"observed_at": known[eid].observed_at} if known[eid].observed_at else {})}
        for eid in judged
    }, ensure_ascii=False, separators=(",", ":"))


def final_observation_context(
    modules: dict, report: EditedReportSubmission, bodies: dict[str, str],
    *, workspace: Path, run_id: str,
) -> dict[str, str]:
    """Final checks actual current passages, including after a bounded revision."""
    typed = {
        key: value if isinstance(value, ModuleSubmission) else ModuleSubmission.model_validate(value)
        for key, value in modules.items()
    }
    required_actions = {
        eid for module in typed.values() for part in module.assessments.values()
        for record in part.records or [] if record.disposition in {"concern", "undetermined"}
        for eid in record.evidence_ids
    }
    links = [link for link in report.observation_links if link.section_id in bodies]
    action_mapped = {
        eid for link in links if link.section_id == "3.2" and link.action_anchor.strip()
        and link.action_anchor in bodies[link.section_id] for eid in link.evidence_ids
    }
    return {
        "module_assessments": assessment_source_context(typed),
        "observation_identities": observation_identities(workspace, run_id, typed),
        "observation_delivery": json.dumps({
            "links": [link.model_dump(mode="json") for link in links],
            "invalid_narrative_links": [index for index, link in enumerate(links)
                if not link.narrative_anchor.strip() or link.narrative_anchor not in bodies[link.section_id]],
            "invalid_action_links": [index for index, link in enumerate(links)
                if link.action_anchor and link.action_anchor not in bodies[link.section_id]],
            "unmapped_action_evidence_ids": (
                sorted(required_actions - action_mapped) if "3.2" in bodies else None
            ),
            "coverage_limit": "Explicit author judgments only; unassessed observations remain in module_assessments. Anchors locate text, not semantic correctness.",
        }, ensure_ascii=False, separators=(",", ":")),
    }


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
