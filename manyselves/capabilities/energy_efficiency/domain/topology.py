"""Where each meter sits: its row in the asset list and its node in the plant tree.

Matching is by code or name only: a meter's id, name or the value that picks
its rows in a long table equal to a row's code or name, or a node's id or
name, after case and spaces are set aside.  A code one character away is
listed as a near match, since lists are typed by hand.  Every match is a
candidate for a person to confirm; the program does not decide what a meter
covers.  From the tree it lists which meters sit under which, and says when
no meter sits above the others (no main meter, or a hierarchy nobody knows),
since then energies of different meters may overlap or leave gaps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .inputs import AssetRow, Manifest, MeterSpec, TreeNode, load_assets, load_tree

NEAR_LIMIT = 3
# A row's name found inside a meter's name counts only from this many characters.
CONTAINED_MIN = 3
UNMATCHED_SHOWN = 15


def _norm(value: str | None) -> str:
    return "".join((value or "").split()).casefold()


def _one_apart(left: str, right: str) -> bool:
    """Whether two codes differ in exactly one character (swapped, extra or missing)."""

    if left == right or min(len(left), len(right)) < 3 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) == 1
    short, long = sorted((left, right), key=len)
    return any(long[:index] + long[index + 1 :] == short for index in range(len(long)))


def _keys(spec: MeterSpec) -> set[str]:
    return {key for key in (_norm(spec.id), _norm(spec.name), _norm(spec.filter_value)) if key}


def _asset_matches(spec: MeterSpec, rows: list[AssetRow], names: list[str]) -> list[dict[str, Any]]:
    """Rows whose code or name equals the meter's; else one character away, or contained in its names."""

    keys = _keys(spec)
    exact = [row for row in rows if _norm(row.code) in keys or _norm(row.name) in keys]
    if exact:
        return [{**row.model_dump(), "how": "exact"} for row in exact]
    found = []
    texts = [_norm(name) for name in names if name]
    for row in rows:
        values = [value for value in (_norm(row.code), _norm(row.name)) if value]
        if any(_one_apart(value, key) for value in values for key in keys):
            found.append({**row.model_dump(), "how": "near"})
        elif any(len(value) >= CONTAINED_MIN and value in text for value in values for text in texts):
            found.append({**row.model_dump(), "how": "contained"})
    return found[:NEAR_LIMIT]


def _node_matches(spec: MeterSpec, nodes: list[TreeNode]) -> list[TreeNode]:
    keys = _keys(spec)
    return [node for node in nodes if _norm(node.id) in keys or _norm(node.name) in keys]


def _ancestors(node: TreeNode, by_id: dict[str, TreeNode]) -> set[str]:
    found: set[str] = set()
    current = node.parent
    while current is not None and current in by_id and current not in found:
        found.add(current)
        current = by_id[current].parent
    return found


def _asset_text(match: dict[str, Any]) -> str:
    label = " ".join(part for part in (match["code"], match["name"]) if part)
    head = f"第 {match['row']} 行 {label}"
    return head + {"exact": "", "near": "（编号差一个字符）", "contained": "（名称包含在表名里）"}[match["how"]]


def build_topology(folder: Path, manifest: Manifest) -> dict[str, Any] | None:
    """The candidates, the meters' tree relations and plain notes; None without a list or a tree."""

    if manifest.assets is None and manifest.plant_tree is None:
        return None
    rows = load_assets(folder, manifest.assets) if manifest.assets else None
    nodes = load_tree(folder, manifest.plant_tree) if manifest.plant_tree else None
    by_id = {node.id: node for node in nodes or []}
    meters = []
    meter_nodes: dict[str, list[TreeNode]] = {}
    used_rows: set[int] = set()
    for spec in manifest.meters:
        entry: dict[str, Any] = {"meter_id": spec.id, "name": spec.name, "notes": []}
        found = _node_matches(spec, nodes) if nodes is not None else []
        if rows is not None:
            matches = _asset_matches(spec, rows, [spec.name, *(node.name for node in found)])
            used_rows.update(match["row"] for match in matches)
            entry["asset_matches"] = matches
            entry["asset_match"] = "；".join(_asset_text(match) for match in matches) or None
            categories = sorted({match["category"] for match in matches if match["category"]})
            entry["category"] = "、".join(categories) or None
            equipment = sorted({match["equipment"] for match in matches if match["equipment"]})
            entry["equipment"] = "、".join(equipment) or None
            if not matches:
                entry["notes"].append("台账里没有对上的行")
            elif len({match["row"] for match in matches}) > 1:
                entry["notes"].append("台账里对上不止一行")
        if nodes is not None:
            meter_nodes[spec.id] = found
            entry["tree_nodes"] = [{"id": node.id, "path": node.path} for node in found]
            entry["tree_path"] = "；".join(" / ".join(node.path) for node in found) or None
            if not found:
                entry["notes"].append("工厂树里没有对上的节点")
            elif len(found) > 1:
                entry["notes"].append("工厂树里对上不止一个节点")
        meters.append(entry)
    relations = []
    for upper, upper_nodes in meter_nodes.items():
        for lower, lower_nodes in meter_nodes.items():
            if upper == lower:
                continue
            if any(node.id in _ancestors(below, by_id) for node in upper_nodes for below in lower_nodes):
                relations.append({"upper": upper, "lower": lower, "how": "工厂树"})
    for entry in meters:
        above = [relation["upper"] for relation in relations if relation["lower"] == entry["meter_id"]]
        if above:
            entry["notes"].append("工厂树里在 " + "、".join(above) + " 下面")
    notes = []
    if nodes is not None:
        placed = [meter_id for meter_id, found in meter_nodes.items() if found]
        if placed and not relations:
            notes.append(
                "工厂树里表计之间没有上下级：可能没有总表，也可能层级没有建；不同表的电量是否重叠或漏计不知道，不要相加"
            )
        elif relations:
            uppers = sorted({relation["upper"] for relation in relations})
            notes.append("工厂树里有表挂在别的表下面（上级：" + "、".join(uppers) + "）：上级的电量可能已含下级，不要相加")
        missing = [meter_id for meter_id, found in meter_nodes.items() if not found]
        if missing:
            notes.append("工厂树里没有对上的表：" + "、".join(missing))
    if rows is not None:
        unmatched = [row for row in rows if row.row not in used_rows]
        if unmatched:
            shown = "、".join(" ".join(part for part in (row.code, row.name) if part) for row in unmatched[:UNMATCHED_SHOWN])
            more = f" 等 {len(unmatched)} 行" if len(unmatched) > UNMATCHED_SHOWN else ""
            notes.append(f"台账里没有对上任何表的行：{shown}{more}（可能没接入数据，也可能编号写法不同）")
    return {"meters": meters, "relations": relations, "notes": notes}


def topology_group(topology: dict[str, Any] | None, meter_id: str) -> str | None:
    """The asset list's category of a meter, used as its group when the manifest gives none."""

    for entry in (topology or {}).get("meters", []):
        if entry["meter_id"] == meter_id:
            return entry.get("category")
    return None


__all__ = ["build_topology", "topology_group"]
