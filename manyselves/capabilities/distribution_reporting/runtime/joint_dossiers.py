"""Per-object dossiers for the joint (Cross) review.

One observed object (a cabinet, a circuit, a transformer) is often used by
several modules, each from its own angle.  Instead of handing the joint
review the full text of five modules, the program gathers, per object, the
facts and every module's judgments with a short prose excerpt.  Only objects
whose judgments could disagree are kept; the joint review reads these and
sends corrections to the named modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .models.agentic import ModuleSubmission
from .observation_ledger import build_observation_ledger
from .research.project_evidence import ProjectEvidenceIndex

EXCERPT_CHARS = 240
ATTENTION = {"concern", "undetermined"}


@dataclass
class _Dossier:
    subject: str
    facts: dict[str, dict] = field(default_factory=dict)
    judgments: list[dict] = field(default_factory=list)


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
    """Objects used by two or more modules whose judgments need a joint look.

    Kept when the judgments from different modules differ, or when two or more
    modules raise a concern or leave the object undetermined (their actions and
    explanations must fit together).  Objects are matched by the normalized
    evidence subject; the same equipment named differently in two tables is
    not merged here.
    """

    index = ProjectEvidenceIndex(workspace, run_id)
    if not index.path.is_file():
        return []
    items = {item.id: item for item in index.items()}
    ledger = build_observation_ledger(workspace, run_id)
    labels = ledger.shared_labels() if ledger is not None else {}
    dossiers: dict[str, _Dossier] = {}

    def dossier_for(evidence_id: str) -> _Dossier | None:
        item = items.get(evidence_id)
        if item is None:
            return None
        found = dossiers.setdefault(item.subject, _Dossier(item.subject))
        if evidence_id not in found.facts:
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
            if evidence_id in labels:
                fact["shared"] = labels[evidence_id]
            found.facts[evidence_id] = fact
        return found

    for item in items.values():
        if item.module_id:
            dossier_for(item.id)
    for module_id, module in modules.items():
        for part_id, part in module.assessments.items():
            narrative = module.submodule_narratives.get(part_id, "")
            for record in part.records or []:
                touched = {
                    id(found): found
                    for evidence_id in record.evidence_ids
                    if (found := dossier_for(evidence_id)) is not None
                }
                for found in touched.values():
                    judgment = {
                        "module_id": module_id,
                        "submodule_id": part_id,
                        "evidence_ids": [
                            evidence_id for evidence_id in record.evidence_ids
                            if evidence_id in found.facts
                        ],
                        "disposition": record.disposition,
                        "excerpt": _excerpt(narrative, record.narrative_anchor),
                    }
                    if record.action_anchor:
                        judgment["action_excerpt"] = _excerpt(narrative, record.action_anchor)
                    found.judgments.append(judgment)

    selected: list[dict] = []
    for subject, found in sorted(dossiers.items()):
        fact_modules = {fact["module_id"] for fact in found.facts.values() if fact["module_id"]}
        judged_modules = {judgment["module_id"] for judgment in found.judgments}
        if len(fact_modules | judged_modules) < 2 or len(judged_modules) < 2:
            continue
        reasons = []
        by_module: dict[str, set[str]] = {}
        for judgment in found.judgments:
            by_module.setdefault(judgment["module_id"], set()).add(judgment["disposition"])
        if len({frozenset(values) for values in by_module.values()}) > 1:
            reasons.append("modules_disagree")
        if sum(1 for values in by_module.values() if values & ATTENTION) >= 2:
            reasons.append("several_modules_raise_attention")
        if not reasons:
            continue
        submodules = sorted({
            (judgment["module_id"], judgment["submodule_id"]) for judgment in found.judgments
        })
        selected.append({
            "subject": subject,
            "reasons": reasons,
            "module_ids": sorted(judged_modules),
            "submodules": [{"module_id": m, "submodule_id": s} for m, s in submodules],
            "facts": sorted(found.facts.values(), key=lambda fact: fact["evidence_id"]),
            "judgments": found.judgments,
        })
    for number, dossier in enumerate(selected, start=1):
        dossier["dossier_id"] = f"D-{number:03d}"
    return selected


def dossiers_by_module(dossiers: list[dict]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for dossier in dossiers:
        for module_id in dossier["module_ids"]:
            found.setdefault(module_id, []).append(dossier["dossier_id"])
    return found


__all__ = ["build_joint_dossiers", "dossiers_by_module"]
