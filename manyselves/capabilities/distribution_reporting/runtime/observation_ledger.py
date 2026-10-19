"""Shared source observations and who owns them.

Mappers copy one precise source observation into several submodules, each
copy with its own E-ID.  Every subsection may judge its copy from its own
angle (load distribution, heating, equipment condition).  The ledger groups the
copies so authors and reviewers see where else the observation appears, names
one owner whose judgment decides a same-angle conflict, and lets the program
list groups whose judgments disagree for the bounded joint review.

The owner comes from ``domain/observation-ownership.yaml`` (chosen by
subsection meaning); route sets it does not list fall back to the mapper's
first route (lowest E-ID).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from .models.agentic import ModuleSubmission
from .research.project_evidence import ProjectEvidenceIndex

LEDGER_VERSION = 1
OWNERSHIP_RULES = Path(__file__).resolve().parents[1] / "domain/observation-ownership.yaml"


@cache
def ownership_rules() -> dict[frozenset[str], str]:
    data = yaml.safe_load(OWNERSHIP_RULES.read_text(encoding="utf-8")) or {}
    rules: dict[frozenset[str], str] = {}
    for rule in data.get("rules", []):
        routes = frozenset(str(item) for item in rule["routes"])
        owner = str(rule["owner"])
        if owner not in routes:
            raise ValueError(f"ownership rule owner {owner} is not one of {sorted(routes)}")
        rules[routes] = owner
    return rules


@dataclass(frozen=True)
class ObservationRoute:
    evidence_id: str
    module_id: str | None
    submodule_id: str | None

    def to_json(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "module_id": self.module_id,
            "submodule_id": self.submodule_id,
        }


@dataclass(frozen=True)
class ObservationGroup:
    observation_id: str
    source: dict
    subject: str
    observed_at: str | None
    exact_routed_copy: bool
    owner: ObservationRoute
    references: tuple[ObservationRoute, ...]

    @property
    def routes(self) -> tuple[ObservationRoute, ...]:
        return (self.owner, *self.references)

    def to_json(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "source": self.source,
            "subject": self.subject,
            "observed_at": self.observed_at,
            "exact_routed_copy": self.exact_routed_copy,
            "owner": self.owner.to_json(),
            "references": [route.to_json() for route in self.references],
        }


def _evidence_order(evidence_id: str) -> tuple[int, str]:
    digits = evidence_id.removeprefix("E-")
    return (int(digits), evidence_id) if digits.isdigit() else (1 << 62, evidence_id)


class ObservationLedger:
    def __init__(self, groups: list[ObservationGroup]) -> None:
        self.groups = groups
        self._group_by_evidence = {
            route.evidence_id: group for group in groups for route in group.routes
        }

    def group_of(self, evidence_id: str) -> ObservationGroup | None:
        return self._group_by_evidence.get(evidence_id)

    def owner_for_reference(self, evidence_id: str) -> ObservationRoute | None:
        """Return the owner route when ``evidence_id`` is a reference copy."""

        group = self._group_by_evidence.get(evidence_id)
        if group is None or group.owner.evidence_id == evidence_id:
            return None
        return group.owner

    def shared_labels(self) -> dict[str, str]:
        """Fact/review annotation for every copy: group, owner and other places."""

        labels: dict[str, str] = {}
        for group in self.groups:
            for route in group.routes:
                others = "、".join(
                    f"{other.submodule_id}/{other.evidence_id}"
                    for other in group.routes if other.evidence_id != route.evidence_id
                )
                owner = (
                    "本小节" if route == group.owner
                    else f"{group.owner.submodule_id}/{group.owner.evidence_id}"
                )
                labels[route.evidence_id] = f"{group.observation_id}；负责={owner}；另见={others}"
        return labels

    def to_json(self) -> dict:
        return {
            "version": LEDGER_VERSION,
            "owner_rule": "domain/observation-ownership.yaml; unlisted route sets: first mapper route (lowest E-ID)",
            "groups": [group.to_json() for group in self.groups],
        }


def build_observation_ledger(workspace: Path, run_id: str) -> ObservationLedger | None:
    """Group routed copies by precise source location; None without a snapshot."""

    index = ProjectEvidenceIndex(workspace, run_id)
    if not index.path.is_file():
        return None
    buckets: dict[tuple, list] = {}
    for item in index.items():
        source = item.source
        if not source.cell and not (source.row and source.column):
            continue  # A whole file/page is not a precise observation identity.
        key = (
            source.path.as_posix().replace("\\", "/"), source.sheet,
            source.cell, source.row, source.column, source.image_id,
            item.subject, item.observed_at,
        )
        buckets.setdefault(key, []).append(item)
    groups: list[ObservationGroup] = []
    for items in buckets.values():
        if len({(item.module_id, item.submodule_id) for item in items}) < 2:
            continue
        ordered = sorted(items, key=lambda item: _evidence_order(item.id))
        payloads = {
            json.dumps(
                item.model_dump(mode="json", exclude={"id", "module_id", "submodule_id"}),
                sort_keys=True,
            )
            for item in ordered
        }
        routes = [
            ObservationRoute(item.id, item.module_id, item.submodule_id) for item in ordered
        ]
        owner_submodule = ownership_rules().get(
            frozenset(str(route.submodule_id) for route in routes)
        )
        if owner_submodule is not None:
            routes.sort(key=lambda route: route.submodule_id != owner_submodule)
        groups.append(ObservationGroup(
            observation_id="",
            source=ordered[0].source.model_dump(mode="json"),
            subject=ordered[0].subject,
            observed_at=ordered[0].observed_at,
            exact_routed_copy=len(payloads) == 1,
            owner=routes[0],
            references=tuple(routes[1:]),
        ))
    groups.sort(key=lambda group: _evidence_order(group.owner.evidence_id))
    return ObservationLedger([
        ObservationGroup(
            observation_id=f"O-{number:04d}",
            source=group.source,
            subject=group.subject,
            observed_at=group.observed_at,
            exact_routed_copy=group.exact_routed_copy,
            owner=group.owner,
            references=group.references,
        )
        for number, group in enumerate(groups, start=1)
    ])


def shared_labels(workspace: Path, run_id: str) -> dict[str, str]:
    ledger = build_observation_ledger(workspace, run_id)
    return ledger.shared_labels() if ledger is not None else {}


def _records_by_evidence(modules: Mapping[str, ModuleSubmission]) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for module_id, module in modules.items():
        for part_id, part in module.assessments.items():
            for record in part.records or []:
                for evidence_id in record.evidence_ids:
                    found.setdefault(evidence_id, []).append({
                        "module_id": module_id,
                        "submodule_id": part_id,
                        "disposition": record.disposition,
                        "narrative_anchor": record.narrative_anchor,
                        "action_anchor": record.action_anchor,
                    })
    return found


def check_ledger(
    ledger: ObservationLedger, modules: Mapping[str, ModuleSubmission],
) -> dict:
    """Program checks over final module judgments; a report, never a gate."""

    records = _records_by_evidence(modules)
    unjudged: list[dict] = []
    duplicate: list[dict] = []
    conflicting: list[dict] = []
    for group in ledger.groups:
        owner_records = records.get(group.owner.evidence_id, [])
        reference_records = [
            {**record, "evidence_id": route.evidence_id}
            for route in group.references for record in records.get(route.evidence_id, [])
        ]
        if not owner_records:
            unjudged.append({
                "observation_id": group.observation_id,
                "subject": group.subject,
                "owner": group.owner.to_json(),
            })
        if reference_records:
            duplicate.append({
                "observation_id": group.observation_id,
                "subject": group.subject,
                "owner": group.owner.to_json(),
                "reference_records": reference_records,
            })
        dispositions = {
            record["disposition"]
            for record in [*owner_records, *reference_records]
        }
        if len(dispositions) > 1:
            conflicting.append({
                "observation_id": group.observation_id,
                "subject": group.subject,
                "dispositions": sorted(dispositions),
                "owner": group.owner.to_json(),
            })
    return {
        "shared_observations": len(ledger.groups),
        "owner_unjudged": unjudged,
        "judged_in_several_places": duplicate,
        "conflicting_dispositions": conflicting,
        "limit": (
            "Program checks compare explicit judgment records only; several places "
            "may judge one observation from different angles, so a disposition "
            "difference is a candidate for the joint review, not a proven error."
        ),
    }


def observation_relations(
    ledger: ObservationLedger | None,
    modules: Mapping[str, ModuleSubmission],
    evidence_subjects: Mapping[str, str],
) -> list[dict]:
    """Concern/undetermined judgments grouped by observed object across modules.

    This replaces the model-written Cross synthesis as Chapter 3 input: the
    program lists which modules judged what about the same object; the Chief
    writes the relations in prose.
    """

    references: dict[str, list[dict]] = {}
    if ledger is not None:
        for group in ledger.groups:
            references[group.owner.evidence_id] = [
                route.to_json() for route in group.references
            ]
    by_subject: dict[str, list[dict]] = {}
    for module_id, module in modules.items():
        for part_id, part in module.assessments.items():
            for record in part.records or []:
                if record.disposition not in {"concern", "undetermined"}:
                    continue
                for evidence_id in record.evidence_ids:
                    subject = evidence_subjects.get(evidence_id, "")
                    entry = {
                        "evidence_id": evidence_id,
                        "module_id": module_id,
                        "submodule_id": part_id,
                        "disposition": record.disposition,
                        "narrative_anchor": record.narrative_anchor,
                    }
                    if record.action_anchor:
                        entry["action_anchor"] = record.action_anchor
                    group = ledger.group_of(evidence_id) if ledger is not None else None
                    if group is not None:
                        entry["observation_id"] = group.observation_id
                    if references.get(evidence_id):
                        entry["referenced_by"] = references[evidence_id]
                    by_subject.setdefault(subject, []).append(entry)
    relations = []
    for subject, entries in sorted(by_subject.items()):
        modules_involved = sorted({entry["module_id"] for entry in entries})
        shared = any("observation_id" in entry for entry in entries)
        if len(modules_involved) < 2 and not shared:
            continue
        relations.append({
            "subject": subject,
            "module_ids": modules_involved,
            "judgments": entries,
        })
    return relations


__all__ = [
    "ObservationGroup",
    "ObservationLedger",
    "ObservationRoute",
    "build_observation_ledger",
    "check_ledger",
    "observation_relations",
    "shared_labels",
]
