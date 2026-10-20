"""Per-object dossiers for the joint (Cross) review.

One observed object (a cabinet, a circuit, a transformer) is often used by
several modules, each from its own angle.  Instead of handing the joint
review the full text of five modules, the program gathers, per object, the
facts and every module's judgments with a short prose excerpt, and sends the
joint review only the objects that need a joint look.

Everything here follows the fixed input structure, not one run's data: the
object is the room/cabinet identity the mappers read from fixed columns
(normalized so spelling variants align), a quantity is a mapper-declared
measurement, and the keep rules carry no tuned thresholds.  Missing objects
are never merged; when in doubt a dossier is kept rather than dropped.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .models.agentic import ModuleSubmission
from .observation_ledger import build_observation_ledger
from .photo_reading import load_photo_readings, reading_label
from .research.project_evidence import ProjectEvidenceIndex

EXCERPT_CHARS = 240
ATTENTION = {"concern", "undetermined"}
UNIDENTIFIED_PREFIXES = ("未注明",)


def object_key(subject: str) -> str:
    """Normalized object identity; spelling variants of one cabinet align.

    Width, spaces and "1号"/"1#" are unified.  A subject without a stated room
    keeps its raw text, so unidentified objects are never merged together.
    """

    text = unicodedata.normalize("NFKC", subject).strip()
    if text.startswith(UNIDENTIFIED_PREFIXES):
        return f"raw:{subject}"
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(\d)号", r"\1#", text)
    return text.lower()


@dataclass
class _Dossier:
    subject: str
    facts: dict[str, dict] = field(default_factory=dict)
    judgments: list[dict] = field(default_factory=list)
    subjects: set[str] = field(default_factory=set)
    quantities: dict[tuple[str, str], dict[str, set[float]]] = field(default_factory=dict)


def _differing_quantities(found: _Dossier) -> list[dict]:
    """Same quantity read from two sheets with different values: state the basis."""

    differing = []
    for (quantity, unit), by_sheet in sorted(found.quantities.items()):
        values = {value for sheet_values in by_sheet.values() for value in sheet_values}
        if len(by_sheet) >= 2 and len(values) >= 2:
            differing.append({
                "quantity": quantity,
                "unit": unit,
                "values_by_sheet": {sheet: sorted(vals) for sheet, vals in sorted(by_sheet.items())},
            })
    return differing


def _excerpt(narrative: str, anchor: str) -> str:
    if not anchor:
        return ""
    start = narrative.find(anchor)
    if start < 0:
        return ""
    half = max(0, (EXCERPT_CHARS - len(anchor)) // 2)
    begin = max(0, start - half)
    end = min(len(narrative), start + len(anchor) + half)
    return ("…" if begin else "") + narrative[begin:end].strip() + ("…" if end < len(narrative) else "")


def build_joint_dossiers(
    workspace: Path, run_id: str, modules: Mapping[str, ModuleSubmission],
) -> list[dict]:
    """Dossiers from module submissions; see ``build_dossiers``."""

    judgments = []
    for module_id, module in modules.items():
        for part_id, part in module.assessments.items():
            narrative = module.submodule_narratives.get(part_id, "")
            for record in part.records or []:
                judgment = {
                    "module_id": module_id,
                    "submodule_id": part_id,
                    "evidence_ids": list(record.evidence_ids),
                    "disposition": record.disposition,
                    "excerpt": _excerpt(narrative, record.narrative_anchor),
                }
                if record.action_anchor:
                    judgment["action_excerpt"] = _excerpt(narrative, record.action_anchor)
                judgments.append(judgment)
    return build_dossiers(workspace, run_id, judgments)


def build_dossiers(
    workspace: Path, run_id: str, judgments: list[dict],
) -> list[dict]:
    """Objects used by two or more modules whose judgments need a joint look.

    ``judgments`` are per-judgment records with ``module_id``,
    ``submodule_id``, ``evidence_ids`` and ``disposition``; other keys (an
    excerpt, a row id) are carried into the dossier as they are.

    Kept when the judgments from different modules differ, when two or more
    modules raise a concern or leave the object undetermined (their actions and
    explanations must fit together), or when one quantity is read with
    different values from two sheets (the report must state each basis), or
    when a photo read in this run conflicts with the record it is bound to.
    """

    index = ProjectEvidenceIndex(workspace, run_id)
    if not index.path.is_file():
        return []
    items = {item.id: item for item in index.items()}
    ledger = build_observation_ledger(workspace, run_id)
    labels = ledger.shared_labels() if ledger is not None else {}
    photo_readings = load_photo_readings(workspace, run_id)
    dossiers: dict[str, _Dossier] = {}

    def dossier_for(evidence_id: str) -> _Dossier | None:
        item = items.get(evidence_id)
        if item is None:
            return None
        found = dossiers.setdefault(object_key(item.subject), _Dossier(item.subject))
        found.subjects.add(item.subject)
        if evidence_id not in found.facts:
            sheet = item.source.sheet or item.source.path.as_posix()
            for measurement in item.measurements:
                if measurement.value is not None:
                    found.quantities.setdefault(
                        (measurement.quantity, measurement.unit), {},
                    ).setdefault(sheet, set()).add(measurement.value)
            fact = {
                "evidence_id": evidence_id,
                "module_id": item.module_id,
                "submodule_id": item.submodule_id,
                "fact": item.fact,
            }
            if item.value is not None:
                fact["value"] = item.value
                fact["unit"] = item.unit
            if item.observed_at:
                fact["observed_at"] = item.observed_at
            fact["sheet"] = sheet
            if item.photo_refs:
                fact["photo_ids"] = list(item.photo_refs)
                read = [photo_readings[ref] for ref in item.photo_refs if ref in photo_readings]
                if read:
                    fact["photo_readings"] = [reading_label(reading) for reading in read]
                if any(reading.record_match == "conflicts" for reading in read):
                    fact["photo_conflicts"] = True
            if evidence_id in labels:
                fact["shared"] = labels[evidence_id]
            found.facts[evidence_id] = fact
        return found

    for item in items.values():
        if item.module_id:
            dossier_for(item.id)
    for record in judgments:
        touched = {
            id(found): found
            for evidence_id in record["evidence_ids"]
            if (found := dossier_for(evidence_id)) is not None
        }
        for found in touched.values():
            judgment = {
                **record,
                "evidence_ids": [
                    evidence_id for evidence_id in record["evidence_ids"]
                    if evidence_id in found.facts
                ],
            }
            found.judgments.append(judgment)

    selected: list[dict] = []
    for _key, found in sorted(dossiers.items()):
        fact_modules = {fact["module_id"] for fact in found.facts.values() if fact["module_id"]}
        judged_modules = {judgment["module_id"] for judgment in found.judgments}
        reasons = []
        by_module: dict[str, set[str]] = {}
        for judgment in found.judgments:
            by_module.setdefault(judgment["module_id"], set()).add(judgment["disposition"])
        if len(by_module) >= 2 and len({frozenset(values) for values in by_module.values()}) > 1:
            reasons.append("modules_disagree")
        if sum(1 for values in by_module.values() if values & ATTENTION) >= 2:
            reasons.append("several_modules_raise_attention")
        differing = _differing_quantities(found)
        if differing:
            reasons.append("same_quantity_differs")
        photo_conflicts = [fact for fact in found.facts.values() if fact.get("photo_conflicts")]
        if photo_conflicts:
            reasons.append("photo_conflicts_record")
        if not reasons:
            continue
        submodules = {
            (judgment["module_id"], judgment["submodule_id"]) for judgment in found.judgments
        }
        if differing:
            submodules |= {
                (fact["module_id"], fact["submodule_id"]) for fact in found.facts.values()
                if fact["module_id"] and fact["submodule_id"]
            }
        submodules |= {
            (fact["module_id"], fact["submodule_id"]) for fact in photo_conflicts
            if fact["module_id"] and fact["submodule_id"]
        }
        conflict_modules = {fact["module_id"] for fact in photo_conflicts if fact["module_id"]}
        dossier = {
            "subject": found.subject,
            "reasons": reasons,
            "module_ids": sorted(
                judged_modules | (fact_modules if differing else set()) | conflict_modules
            ),
            "submodules": [{"module_id": m, "submodule_id": s} for m, s in sorted(submodules)],
            "facts": sorted(found.facts.values(), key=lambda fact: fact["evidence_id"]),
            "judgments": found.judgments,
        }
        if len(found.subjects) > 1:
            dossier["subject_spellings"] = sorted(found.subjects)
        if differing:
            dossier["differing_quantities"] = differing
        selected.append(dossier)
    for number, dossier in enumerate(selected, start=1):
        dossier["dossier_id"] = f"D-{number:03d}"
    return selected


def dossiers_by_module(dossiers: list[dict]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for dossier in dossiers:
        for module_id in dossier["module_ids"]:
            found.setdefault(module_id, []).append(dossier["dossier_id"])
    return found


__all__ = ["build_dossiers", "build_joint_dossiers", "dossiers_by_module", "object_key"]
