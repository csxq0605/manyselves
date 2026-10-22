"""A checker's or editor's finding stands on the material it cites.

A checker or an editor can be wrong too: in one run an editor wrote that the
ledger had judged two records to be in different rooms when the ledger said
the same room, and the author followed it.  So a finding names, in
``grounds``, the ids of the material it rests on (judgment rows, source
records, values and counts).  A blocking finding none of whose grounds is in
the material its writer was given cannot be checked; it is kept as a minor
finding marked as such.  A finding that stands carries the text of what it
cites, so the author can compare before changing anything and may decline it
with a reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Ids the material carries: rows, records, photos, value/count/list slots,
# knowledge units.
GROUND_ID = re.compile(r"J-\d+(?:\.\d+)*-\d+|E-\d+|P-\d+|[VKS]-\d+|KU-\d+(?:\.\d+)*-\d+")
UNCHECKABLE = "无法核对（意见没有引用所给材料中的编号）："
GROUND_TEXT = 200


def ground_ids(values: Any) -> list[str]:
    """The ids written in a finding's grounds, in order and once each."""

    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(i for v in values or () for i in GROUND_ID.findall(str(v))))


def _cut(text: Any) -> str:
    text = str(text or "")
    return text if len(text) <= GROUND_TEXT else text[:GROUND_TEXT] + "…"


def grounded(
    findings: Mapping[str, Mapping[str, Any]], material: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Findings with their grounds checked against ``material`` (id to its text).

    A blocking finding that cites nothing from the material becomes minor;
    every finding keeps, under ``grounds``, the text of the material it cites.
    """

    checked: dict[str, dict[str, Any]] = {}
    for target, finding in findings.items():
        if not isinstance(finding, Mapping):
            continue
        cited = {i: _cut(material[i]) for i in ground_ids(finding.get("grounds")) if i in material}
        result = {key: value for key, value in finding.items() if key != "grounds"}
        if result.get("severity") == "blocking" and not cited:
            result["severity"] = "minor"
            result["problem"] = UNCHECKABLE + str(result.get("problem", ""))
        if cited:
            result["grounds"] = cited
        checked[target] = result
    return checked


__all__ = ["GROUND_ID", "UNCHECKABLE", "ground_ids", "grounded"]
