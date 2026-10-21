"""What the knowledge base itself says about input fields and knowledge units.

The knowledge base carries its own index from input-table fields to knowledge
units, and each knowledge unit names the report section it serves.  This
module reads both, using the field names declared in
``domain/knowledge-structure.yaml``, so the program routes a table field to
the sections the knowledge base names instead of a route written in code.

A field the knowledge base lists without a unit (it says the match depends on
what was actually inspected), and a field it does not list, stay unassigned:
the module authors see them and decide where they belong, and the program
checks that every one of them was judged somewhere.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ..domain.taxonomy import REPORT_TAXONOMY
from .mappers.common import evidence_id, field_name
from .models.reporting import EvidenceItem
from .rule_standards import unit_bodies

STRUCTURE_FILE = Path(__file__).resolve().parents[1] / "domain/knowledge-structure.yaml"
_SECTION_ID = re.compile(r"(?<![\d.])\d+(?:\.\d+)+(?![\d.])")


@cache
def structure() -> dict[str, Any]:
    return yaml.safe_load(STRUCTURE_FILE.read_text(encoding="utf-8")) or {}


def unit_field(body: str, name: str) -> str:
    """One field of a knowledge unit body ("名称：值" on its own line)."""

    pattern = re.compile(
        r"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?" + re.escape(name) + r"(?:\*\*)?\s*[：:]\s*(.+?)\s*$"
    )
    values = [match.group(1).strip() for match in pattern.finditer(body)]
    return "；".join(dict.fromkeys(value for value in values if value))


def _leaf_sections(text: str) -> list[str]:
    """Report subsections named in a text, kept only if the taxonomy has them."""

    leaves = {
        sub_id
        for module in REPORT_TAXONOMY.values()
        for sub_id in module.submodules
    }
    return list(dict.fromkeys(s for s in _SECTION_ID.findall(text) if s in leaves))


@dataclass(frozen=True)
class FieldEntry:
    field: str
    units: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class KnowledgeIndex:
    """Knowledge units, the section each serves, and the field index."""

    units: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unit_sections: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    fields: Mapping[str, FieldEntry] = field(default_factory=dict)

    def entry(self, label: str | None) -> FieldEntry | None:
        return self.fields.get(field_name(label or "")) if label else None

    def to_json(self) -> dict[str, Any]:
        return {
            "unit_sections": {unit: list(s) for unit, s in self.unit_sections.items()},
            "fields": {
                name: {"units": list(entry.units), "note": entry.note}
                for name, entry in self.fields.items()
            },
        }


def _field_entries(text: str) -> dict[str, FieldEntry]:
    spec = structure().get("field_index") or {}
    heading = spec.get("heading")
    if not heading:
        return {}
    match = re.search(r"(?m)^(#{1,6})\s.*" + re.escape(heading) + r".*$", text)
    if match is None:
        return {}
    level = len(match.group(1))
    body = text[match.end():]
    end = re.search(r"(?m)^#{1," + str(level) + r"}\s", body)
    body = body[: end.start()] if end else body
    unit_pattern = re.compile(structure()["unit"]["id_pattern"])
    entries: dict[str, FieldEntry] = {}
    for block in re.split(r"\n\s*\n", body):
        name = field_name(unit_field(block, spec["field_key"]))
        if not name:
            continue
        units = tuple(dict.fromkeys(unit_pattern.findall(unit_field(block, spec["units_key"]))))
        entries.setdefault(name, FieldEntry(name, units, unit_field(block, spec["note_key"])))
    return entries


def build_index(texts: Iterable[str]) -> KnowledgeIndex:
    """Read the knowledge base's units and field index; empty when it has none."""

    texts = list(texts)
    names = structure()["unit"]["fields"]
    units: dict[str, dict[str, str]] = {}
    sections: dict[str, tuple[str, ...]] = {}
    for unit_id, (title, body) in unit_bodies(texts).items():
        values = {"title": title}
        values.update({key: unit_field(body, name) for key, name in names.items()})
        units[unit_id] = values
        found = _leaf_sections(values.get("topics", "")) or _leaf_sections(
            values.get("category", "")
        )
        if found:
            sections[unit_id] = tuple(found)
    fields: dict[str, FieldEntry] = {}
    for text in texts:
        for name, entry in _field_entries(text).items():
            fields.setdefault(name, entry)
    return KnowledgeIndex(units=units, unit_sections=sections, fields=fields)


def documents_with_role(documents: Sequence[Any], role: str) -> list[Any]:
    """Knowledge documents whose file name matches the declared role patterns."""

    patterns = [p.casefold() for p in (structure().get("roles") or {}).get(role, [])]
    return [
        document
        for document in documents
        if any(
            fnmatch.fnmatch(Path(document.relative_path).name.casefold(), pattern)
            for pattern in patterns
        )
    ]


def route_evidence(
    items: Sequence[EvidenceItem], index: KnowledgeIndex
) -> list[EvidenceItem]:
    """Route each unrouted table field to the sections its knowledge units serve.

    Only records a mapper left unrouted and labelled with their header field
    are touched.  A field indexed to several units becomes one record per
    section, in the knowledge base's order; each carries the units of its
    section.  Records the knowledge base cannot place stay unassigned and
    carry what the index says about them.
    """

    routed: list[EvidenceItem] = []
    for item in items:
        if item.module_id is not None or not item.checkpoint_field:
            routed.append(item)
            continue
        entry = index.entry(item.checkpoint_field)
        by_section: dict[str, list[str]] = {}
        for unit in entry.units if entry else ():
            for section in index.unit_sections.get(unit, ()):
                by_section.setdefault(section, []).append(unit)
        if not by_section:
            routed.append(item.model_copy(update={
                "knowledge_units": list(entry.units) if entry else [],
                "knowledge_note": (entry.note or None) if entry else None,
            }))
            continue
        for section, units in by_section.items():
            module_id = next(
                module.id for module in REPORT_TAXONOMY.values() if section in module.submodules
            )
            routed.append(item.model_copy(update={
                "id": evidence_id(
                    item.source.file_id or "", item.source.sheet or "",
                    item.source.cell or "", section, item.fact,
                ),
                "module_id": module_id,
                "submodule_id": section,
                "knowledge_units": units,
            }))
    return routed


__all__ = [
    "FieldEntry",
    "KnowledgeIndex",
    "build_index",
    "documents_with_role",
    "field_name",
    "route_evidence",
    "structure",
    "unit_field",
]
