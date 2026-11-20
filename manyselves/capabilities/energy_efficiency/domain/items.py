"""Loss items: proposed by the analyst or a person's rule, computed by the program over the whole period.

The program's lenses only give differences (actual − reference).  An item is
what someone proposes as a loss: it adopts some of those differences (its
rows then carry their whole-period numbers) and/or brings a reference spec,
which the program computes over the whole period into one row per meter.  A
person's reference rule (P39) is an item of its own.  A difference belongs to
at most one item.

Every row gets the stability checks (``probes.stability``: the two halves of
the period, the largest day, the shifted hours of a spec) before it is
screened.  Nothing here judges an item; the screening and a person do.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .grid import Grid
from .lenses import ANALYSIS_DIR, Candidate
from .probes import stability
from .specs import ReferenceSpec, reference_candidates
from .store import SiteData, read_json, write_json

ITEMS_FILE = "items.json"
RULE_ORIGIN = "偏好 P39"
SET_ASIDE_REASONS = {
    "necessary": "必要服务或工艺需要",
    "external": "外部驱动",
    "data": "数据或口径问题",
    "not_comparable": "参照不可比",
    "cannot_tell": "看不出",
}
EXPLAINING_REASONS = ("necessary", "external", "data", "not_comparable")  # listed as the model's explanation of the residual


def load_item_records(site: SiteData) -> dict[str, list[dict[str, Any]]]:
    path = site.path(f"{ANALYSIS_DIR}/{ITEMS_FILE}")
    if not path.is_file():
        return {"items": [], "set_aside": []}
    data = read_json(path)
    return {"items": list(data.get("items", [])), "set_aside": list(data.get("set_aside", []))}


def save_item_records(site: SiteData, items: list[dict[str, Any]], set_aside: list[dict[str, Any]]) -> None:
    write_json(site.path(f"{ANALYSIS_DIR}/{ITEMS_FILE}"), {"items": items, "set_aside": set_aside})


def full_id(prefix: str, local: str) -> str:
    return f"{prefix}-{local}"


@dataclass
class ItemBuild:
    rows: list[Candidate]
    items: dict[str, dict[str, Any]]  # full item id -> the record, with the ids of its rows
    adopted: dict[str, str]  # difference id -> item id
    problems: list[str] = field(default_factory=list)


def rule_item_id(spec: ReferenceSpec) -> str:
    return spec.id if spec.id.startswith("P39") else f"P39-{spec.id}"


def build_items(
    grid: Grid,
    differences: list[Candidate],
    records: list[dict[str, Any]],
    specs: dict[tuple[str, str], ReferenceSpec],
    rules: list[ReferenceSpec],
) -> ItemBuild:
    """The rows of every accepted item; a difference adopted twice or a spec that does not compute is a problem, not a row."""

    by_id = {difference.id: difference for difference in differences}
    rows: list[Candidate] = []
    items: dict[str, dict[str, Any]] = {}
    adopted: dict[str, str] = {}
    taken: set[str] = set()
    problems: list[str] = []

    def add(row: Candidate, item_id: str) -> None:
        if row.id in taken:
            problems.append(f"项 {item_id} 的行 {row.id} 和已有的行同号，没算进去")
            return
        row.item = item_id
        row.details = {**row.details, "stability": stability(grid, row)}
        taken.add(row.id)
        rows.append(row)
        items[item_id]["rows"].append(row.id)

    for spec in rules:
        item_id = rule_item_id(spec)
        items[item_id] = {
            "id": item_id,
            "origin": RULE_ORIGIN,
            "source": "rule",
            "proposal": {"id": item_id, "title": spec.why[:120], "differences": [], "spec": spec.id, "finding": "人定的参照规则（P39）", "reasoning": spec.why, "causes": [], "missing_info": "", "related": []},
            "accepted": True,
            "problems": [],
            "rows": [],
        }
        made, failed = reference_candidates(grid, spec, "rule")
        problems += [f"偏好 P39 规则 {spec.id}：{text}" for text in failed]
        for row in made:
            add(row, item_id)
    for record in records:
        if not record.get("accepted"):
            continue
        item_id = record["id"]
        if item_id in items:
            problems.append(f"项编号 {item_id} 重复，后一个没算进去")
            continue
        items[item_id] = {**record, "source": "model", "rows": []}
        proposal = record["proposal"]
        for difference_id in proposal.get("differences", []):
            difference = by_id.get(difference_id)
            if difference is None:
                problems.append(f"项 {item_id} 采用的差额 {difference_id} 不存在")
                continue
            if difference_id in adopted:
                problems.append(f"项 {item_id} 采用的差额 {difference_id} 已被项 {adopted[difference_id]} 采用，没算进去")
                continue
            adopted[difference_id] = item_id
            add(replace(difference, notes=list(difference.notes), details=dict(difference.details)), item_id)
        spec_id = proposal.get("spec")
        if spec_id:
            spec = specs.get((record["origin"], spec_id))
            if spec is None:
                problems.append(f"项 {item_id} 的参照规格 {spec_id} 没有收下，没算进去")
                continue
            made, failed = reference_candidates(grid, spec, "model")
            problems += [f"项 {item_id} 的规格 {spec_id}：{text}" for text in failed]
            for row in made:
                add(row, item_id)
    return ItemBuild(rows=rows, items=items, adopted=adopted, problems=problems)


__all__ = [
    "EXPLAINING_REASONS",
    "ITEMS_FILE",
    "RULE_ORIGIN",
    "SET_ASIDE_REASONS",
    "ItemBuild",
    "build_items",
    "full_id",
    "load_item_records",
    "rule_item_id",
    "save_item_records",
]
