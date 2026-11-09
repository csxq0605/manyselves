"""Check a partition submission against the run's data, and settle the partition.

The submission's own structure was checked by its contract.  Here the
program checks what needs the data: the meters exist and belong to the
partition, the cited evidence exists and is about the item's meters and
times, a data-based cause cites the item's evidence, the windows hold data,
and the finding method can run on these meters.  Problems are returned to
the model once; whatever remains is kept with the item and shown to the
person, never used to drop the item.

``settle_partition`` turns the first submission and the correction round
into the partition's result.  An item the model dropped while correcting is
put back as first submitted, even when the model renumbered the rest, and a
failed correction round leaves the first submission in place: the model does
not decide what leaves the list.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .evidence import has_evidence, load_evidence
from .items import INPUT_KINDS, KB_ITEMS, PLANT, PartitionSubmission
from .methods import MENU, NO_METHOD, readiness, run_method
from .prepare import meter_totals, method_context
from .store import SiteData, read_json

EVIDENCE_IN_TEXT = re.compile(r"EV-[0-9a-f]{10}")


def _spans(record: dict[str, Any]) -> list[tuple[datetime, datetime]] | None:
    """The times a piece of evidence is about; None when it covers the whole period."""

    arguments = record["arguments"]
    if record["tool"] == "energy-series":
        return [(pd.Timestamp(arguments["start"]), pd.Timestamp(arguments["end"]))]
    if record["tool"] == "compare-windows":
        return [(pd.Timestamp(item["start"]), pd.Timestamp(item["end"])) for item in arguments["windows"]]
    if record["tool"] == "production-records" and arguments.get("start") and arguments.get("end"):
        return [(pd.Timestamp(arguments["start"]), pd.Timestamp(arguments["end"]))]
    return None


def _touches(spans: list[tuple[Any, Any]], windows: list[tuple[Any, Any]]) -> bool:
    return any(start < finish and begin < end for start, end in spans for begin, finish in windows)


def _item_issues(
    site: SiteData,
    partition_id: str,
    meters: list[str],
    known: set[str],
    period: tuple[Any, Any],
    ctx: Any,
    totals: dict[str, float | None],
    item: Any,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(problem: str) -> None:
        issues.append({"key": item.key, "problem": problem})

    missing = [meter_id for meter_id in item.meter_ids if meter_id not in known]
    if missing:
        add(f"表 {missing} 不存在（本分区的表：{meters}）")
    outside = [meter_id for meter_id in item.meter_ids if meter_id in known and meter_id not in meters]
    if outside:
        add(f"表 {outside} 不在本分区（本分区：{meters}）")
    # The other checks still run, on the meters that are right, so the one
    # correction round sees every problem at once.
    valid = [meter_id for meter_id in item.meter_ids if meter_id in known and meter_id in meters]
    windows = [(window.start, window.end) for window in item.windows]
    for evidence_id in item.evidence_ids:
        if not has_evidence(site, evidence_id):
            add(f"证据 {evidence_id} 不存在；只能引用工具返回的 evidence_id")
            continue
        record = load_evidence(site, evidence_id)
        if partition_id != PLANT and record["partition_id"] not in (partition_id, None):
            add(f"证据 {evidence_id} 来自别的分区")
        elif not set(record["meter_ids"]) & set(item.meter_ids):
            add(f"证据 {evidence_id} 讲的是表 {record['meter_ids']}，不是本项的表")
        else:
            spans = _spans(record)
            if spans is not None and not _touches(spans, windows):
                add(f"证据 {evidence_id} 查的时段和本项的时间窗没有交集；请引用查这些时间窗的证据")
    assumption_ids = {entry["id"] for entry in site.assumptions()}
    known_facts = {fact.id for fact in site.facts() or []} | assumption_ids
    stray = sorted(set(item.depends_on_assumptions) - assumption_ids)
    if stray:
        add(f"depends_on_assumptions 里的 {stray} 不是分区说明里 assumptions 的编号")
    for cause in item.root_cause_candidates:
        if cause.basis == "site_fact":
            if not any(fact_id in cause.basis_detail for fact_id in known_facts):
                add(f"候选原因“{cause.cause}”依据是 site_fact，basis_detail 要写分区说明里 site_facts 或 assumptions 的编号")
            continue
        if cause.basis != "data":
            continue
        cited = EVIDENCE_IN_TEXT.findall(cause.basis_detail)
        if not cited:
            add(f"候选原因“{cause.cause}”依据是 data，basis_detail 要写本项的证据编号")
        elif not set(cited) <= set(item.evidence_ids):
            stray = sorted(set(cited) - set(item.evidence_ids))
            add(f"候选原因“{cause.cause}”引用的证据 {stray} 不在本项的 evidence_ids 里")
    start, end = period
    for window in item.windows:
        if window.end <= start or window.start >= end:
            add(f"时间窗 {window.start} 至 {window.end} 完全在数据期之外")
            continue
        empty = [
            meter_id
            for meter_id in valid
            if site.series(meter_id).measure(window.start, window.end).records == 0
        ]
        if empty:
            add(f"时间窗 {window.start} 至 {window.end} 里表 {empty} 没有完整的记录")
    method_id = item.finding_method.method_id
    if method_id != NO_METHOD:
        for meter_id in valid:
            run = run_method(method_id, item.finding_method.params, site.series(meter_id), ctx, totals.get(meter_id))
            if run.status != "ok":
                add(f"方法 {method_id} 在表 {meter_id} 上不能用：{run.reason}")
            elif not run.summary.get("with_data"):
                add(f"方法 {method_id} 按这些参数在表 {meter_id} 上回放，没有一次出现有数据")
    return issues


def check_submission(workspace: Path, scope: str, submission: dict[str, Any]) -> dict[str, Any]:
    run_id, _separator, partition_id = scope.partition("#")
    site = SiteData(workspace, run_id)
    parsed = PartitionSubmission.model_validate(submission)
    meters = site.scope_meters(partition_id)
    issues: list[dict[str, str]] = []
    if meters is None:
        return {"ok": False, "issues": [{"key": "", "problem": f"unknown partition {partition_id}"}]}
    if parsed.partition_id != partition_id:
        issues.append(
            {"key": "", "problem": f"partition_id 应为 {partition_id}，提交的是 {parsed.partition_id}"}
        )
    known = set(site.meter_ids())
    period = site.period()
    ctx = method_context(site, meters)
    totals = meter_totals(site)
    for item in parsed.items:
        try:
            issues += _item_issues(site, partition_id, meters, known, period, ctx, totals, item)
        except Exception as exc:  # noqa: BLE001 - a check that breaks must not lose the partition
            issues.append(
                {
                    "key": item.key,
                    "problem": f"程序核对这一项时出错（{type(exc).__name__}: {exc}）；请检查本项的表、时间窗和引用的证据",
                }
            )
    rule_keys = {
        item["key"]
        for item in read_json(site.path("prechecks.json"))["items"]
        if set(item["meter_ids"]) & set(meters)
    }
    for entry in parsed.coverage:
        outside = [meter_id for meter_id in entry.meter_ids if meter_id not in meters]
        if outside:
            issues.append({"key": "", "problem": f"覆盖说明里的表 {outside} 不在本分区"})
        if entry.method_id not in MENU:
            issues.append({"key": "", "problem": f"覆盖说明里的方法 {entry.method_id} 不在菜单里"})
        stray = sorted(set(entry.rule_keys) - rule_keys)
        if stray:
            issues.append({"key": "", "problem": f"覆盖说明里的规则项 {stray} 不属于本分区"})
        spec = MENU.get(entry.method_id)
        if spec is None:
            continue
        states = {
            meter_id: readiness(spec, meter_id, site.spec(meter_id).has_energy, ctx, len(site.series(meter_id)))
            for meter_id in entry.meter_ids
            if meter_id in meters
        }
        if entry.outcome == "found" and states and all(state != "ready" for state, _reason in states.values()):
            reasons = "；".join(f"{meter_id}：{reason}" for meter_id, (_state, reason) in states.items())
            issues.append(
                {
                    "key": "",
                    "problem": f"覆盖说明写方法 {entry.method_id} 找到了项，但程序判定这些表都用不了这个方法（{reasons}）",
                }
            )
        ready = [meter_id for meter_id, (state, _reason) in states.items() if state == "ready"]
        if entry.outcome == "not_applicable" and ready:
            issues.append(
                {
                    "key": "",
                    "problem": f"覆盖说明写方法 {entry.method_id} 不适用，但程序判定表 {ready} 资料齐全、可以用；"
                    "查过没找到写 none_found，缺人给的参数或资料写 needs_input 并写明缺什么",
                }
            )
    for note in parsed.rule_item_notes:
        if note.rule_key not in rule_keys:
            issues.append({"key": "", "problem": f"规则检出项 {note.rule_key} 不属于本分区"})
    issues += category_issues(site, partition_id, meters, parsed)
    if partition_id == PLANT:
        issues += link_issues(site, parsed)
    return {"ok": not issues, "issues": issues}


def _claimed_but_present(site: SiteData, meter_ids: list[str], kinds: list[str]) -> list[str]:
    """Inputs a coverage entry says are missing that the run does hold."""

    manifest = site.manifest
    production = manifest.production
    present: list[str] = []
    for kind in kinds:
        if kind == "production_records":
            mapped = [meter_id for meter_id in meter_ids if site.spec(meter_id).production_machines]
            if site.batches() is not None and mapped:
                present.append(f"生产记录（表 {mapped} 挂了机台，有批次）")
        elif kind == "quantity" and production is not None and production.quantity_basis:
            present.append("批次数量（manifest 已声明口径）")
        elif kind == "qualified_quantity" and production is not None and production.quantity_basis == "qualified":
            present.append("合格品量")
        elif kind == "product" and production is not None and production.product_column:
            present.append("批次的产品")
        elif kind == "calendar" and manifest.calendar is not None:
            present.append("经人确认的日历")
        elif kind == "tariff" and manifest.tariff is not None:
            present.append("电价")
    return present


def category_issues(site: SiteData, partition_id: str, meters: list[str], parsed: PartitionSubmission) -> list[dict[str, str]]:
    """Every one of the 17 categories is accounted for on every meter of the partition.

    A category on a meter is accounted for by an item proposing it there, or
    by a coverage entry (none found, not applicable, missing inputs).  For the
    plant-wide pass, each category needs one of the two anywhere.
    """

    issues: list[dict[str, str]] = []

    def add(problem: str) -> None:
        issues.append({"key": "", "problem": problem})

    proposed = {
        (proposal.item_id, meter_id)
        for item in parsed.items
        for proposal in item.candidate_categories
        for meter_id in item.meter_ids
    }
    written: set[tuple[str, str]] = set()
    for entry in parsed.category_coverage:
        outside = [meter_id for meter_id in entry.meter_ids if meter_id not in meters]
        if outside:
            add(f"类别覆盖 {entry.item_id} 里的表 {outside} 不在本分区")
        for meter_id in entry.meter_ids:
            pair = (entry.item_id, meter_id)
            if pair in written:
                add(f"类别覆盖 {entry.item_id} 在表 {meter_id} 上写了不止一条")
            written.add(pair)
            if pair in proposed:
                add(
                    f"类别覆盖写 {entry.item_id} 在表 {meter_id} 上 {entry.outcome}，但本次提交里有项在这块表上提议了这一类；"
                    "有项提议的类别不用再写覆盖"
                )
        present = _claimed_but_present(site, [m for m in entry.meter_ids if m in meters], list(entry.missing_inputs))
        if present:
            names = "、".join(INPUT_KINDS[kind] for kind in entry.missing_inputs)
            add(f"类别覆盖 {entry.item_id} 写缺 {names}，但这次的资料里有：{'；'.join(present)}")
    if partition_id == PLANT:
        accounted = {item_id for item_id, _meter in proposed | written}
        left = [item_id for item_id in KB_ITEMS if item_id not in accounted]
        if left:
            add(f"类别覆盖缺这些类别的说明（跨对象看过没找到、不适用或缺资料）：{left}")
        return issues
    for item_id in KB_ITEMS:
        uncovered = [meter_id for meter_id in meters if (item_id, meter_id) not in proposed | written]
        if uncovered:
            add(f"类别 {item_id} 在表 {uncovered} 上既没有项提议，也没有类别覆盖说明")
    return issues


def link_issues(site: SiteData, parsed: PartitionSubmission) -> list[dict[str, str]]:
    """Each side of a plant-wide link is an item of this pass or a listed partition item."""

    path = site.path("plant.json")
    known = set(read_json(path)["item_refs"]) if path.is_file() else set()
    own = {item.key for item in parsed.items}
    issues = []
    for link in parsed.links:
        for side in (link.left, link.right):
            if side not in own and side not in known:
                issues.append(
                    {"key": "", "problem": f"关系 {link.left}—{link.right} 里的 {side} 既不是本次的项，也不是分区项（写成 P01:A1）"}
                )
        if link.left == link.right:
            issues.append({"key": "", "problem": f"关系 {link.left}—{link.right} 两边是同一项"})
    return issues


def _content(item: dict[str, Any]) -> str:
    """What an item says, apart from its key and its links to other items."""

    rest = {name: value for name, value in item.items() if name not in {"key", "related_keys", "relation_types"}}
    return json.dumps(rest, ensure_ascii=False, sort_keys=True)


def _fresh_key(key: str, taken: set[str]) -> str:
    """A key for a restored item; item keys are at most 20 characters."""

    for number in range(1, 1000):
        suffix = "-first" + (str(number) if number > 1 else "")
        candidate = key[: 20 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise ValueError(f"no free key for {key}")


def _identity(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        json.dumps(sorted(item["meter_ids"])),
        json.dumps(item["windows"], sort_keys=True),
        item["finding_method"]["method_id"],
    )


def _describe(item: dict[str, Any]) -> str:
    windows = "；".join(f"{window['start']}–{window['end']}" for window in item["windows"][:2])
    more = f" 等 {len(item['windows'])} 段" if len(item["windows"]) > 2 else ""
    text = item["phenomenon"] if len(item["phenomenon"]) <= 30 else f"{item['phenomenon'][:30]}…"
    meters = "、".join(item["meter_ids"])
    return f"表 {meters}，{windows}{more}，{item['finding_method']['method_id']}：“{text}”"


def _match(first: list[dict[str, Any]], final: list[dict[str, Any]]) -> dict[str, str]:
    """First key -> corrected key.

    The same content pairs first (the model may renumber).  A changed item
    then pairs, in this order, with an item of the same meters, windows and
    method; of the same key and meters and either the same windows or the
    same method; of the same key, windows and method (a meter corrected); of
    the same meters and method.  It pairs only when
    nothing else left could be that item (another first item of the same
    windows and method could be, too), and never with a copy of another
    first item, so a dropped item is put back rather than taken for someone
    else's correction.
    """

    pairs: dict[str, str] = {}
    used: set[str] = set()
    by_content: dict[str, list[str]] = {}
    for item in final:
        by_content.setdefault(_content(item), []).append(item["key"])
    for item in first:
        candidates = [key for key in by_content.get(_content(item), []) if key not in used]
        if candidates:
            chosen = item["key"] if item["key"] in candidates else candidates[0]
            pairs[item["key"]] = chosen
            used.add(chosen)
    first_contents = {_content(item) for item in first}

    def rank(old: dict[str, Any], new: dict[str, Any]) -> int | None:
        meters, windows, method = _identity(old)
        other_meters, other_windows, other_method = _identity(new)
        if (meters, windows, method) == (other_meters, other_windows, other_method):
            return 1
        if old["key"] == new["key"] and meters == other_meters and (windows == other_windows or method == other_method):
            return 2
        if old["key"] == new["key"] and (windows, method) == (other_windows, other_method):
            return 3
        if (meters, method) == (other_meters, other_method):
            return 4
        return None

    final_by_key = {item["key"]: item for item in final}

    def could_be(item: dict[str, Any], new: dict[str, Any], fit: int) -> bool:
        if fit == 1:
            return rank(item, new) == 1
        # An item whose own key still holds its own fix is that fix, not a rival.
        mine = final_by_key.get(item["key"])
        if (
            mine is not None
            and mine is not new
            and mine["key"] not in used
            and _content(mine) not in first_contents
            and rank(item, mine) in (2, 3)
        ):
            return False
        return rank(item, new) is not None or _identity(item)[1:] == _identity(new)[1:]

    for level in (1, 2, 3, 4):
        for old in first:
            if old["key"] in pairs:
                continue
            left = [item for item in first if item["key"] not in pairs]
            candidates = []
            for new in final:
                if new["key"] in used or _content(new) in first_contents:
                    continue
                fit = rank(old, new)
                if fit is None or fit > level:
                    continue
                if [item for item in left if item is old or could_be(item, new, fit)] == [old]:
                    candidates.append(new["key"])
            if candidates:
                chosen = old["key"] if old["key"] in candidates else candidates[0]
                pairs[old["key"]] = chosen
                used.add(chosen)
    return pairs


def settle_partition(
    partition_id: str,
    first_submission: dict[str, Any],
    first_check: dict[str, Any],
    corrected: dict[str, Any],
    correction_errors: dict[str, Any],
) -> dict[str, Any]:
    """The partition's result from its first submission and the correction round.

    ``corrected`` holds the correction round's submission and its recheck (at
    most one entry); ``correction_errors`` says why the round failed, if it did.

    Items are matched by content first and by key second, so an item the
    model renumbered is still recognised.  An item the round dropped is put
    back as first submitted (under a new key when its key now names another
    item), with its place in the coverage and its links; whatever changed
    without the check asking for it is flagged.
    """

    notes: list[str] = []
    first = PartitionSubmission.model_validate(first_submission).model_dump(mode="json")
    if not corrected:
        for error in correction_errors.values():
            notes.append(f"修正轮没有完成（{error}），保留第一次提交和它的核对问题")
        return {
            "partition_id": partition_id,
            "submission": first,
            "check": {"ok": not first_check["issues"], "issues": list(first_check["issues"])},
            "notes": notes,
        }
    (attempt,) = list(corrected.values())[:1]
    try:
        return _settle(partition_id, first, first_check, attempt)
    except Exception as exc:  # noqa: BLE001 - a settle bug must not lose the partition
        notes.append(f"修正轮的结果没能和第一次提交对上（{exc}），保留第一次提交和它的核对问题")
        return {
            "partition_id": partition_id,
            "submission": first,
            "check": {"ok": not first_check["issues"], "issues": list(first_check["issues"])},
            "notes": notes,
        }


def _settle(
    partition_id: str,
    first: dict[str, Any],
    first_check: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    final = PartitionSubmission.model_validate(attempt["submission"]).model_dump(mode="json")
    issues = list(attempt["check"]["issues"])
    flagged = {issue["key"] for issue in first_check["issues"]}
    pairs = _match(first["items"], final["items"])
    taken = {item["key"] for item in final["items"]}
    now = {item["key"]: item for item in final["items"]}
    key_map = dict(pairs)
    restored: list[dict[str, Any]] = []
    for item in first["items"]:
        old = item["key"]
        if old in pairs:
            new = pairs[old]
            if new != old:
                issues.append({"key": new, "problem": f"修正轮把这一项的编号从 {old} 改成了 {new}"})
            if _content(now[new]) != _content(item) and old not in flagged:
                issues.append({"key": new, "problem": "修正轮改动了这一项，但核对没有指出它的问题"})
            continue
        new = old if old not in taken else _fresh_key(old, taken)
        taken.add(new)
        key_map[old] = new
        restored.append({**item, "key": new})
        issues += [{**issue, "key": new} for issue in first_check["issues"] if issue["key"] == old]
        issues.append({"key": new, "problem": f"修正轮里模型删掉了这一项（原编号 {old}）；这里保留它第一次提交的版本"})
    matched_new = set(pairs.values())
    for item in final["items"]:
        if item["key"] not in matched_new:
            issues.append({"key": item["key"], "problem": "这一项是修正轮新增的，核对没有要求"})
    notes: list[str] = []
    for item in first["items"]:
        new = pairs.get(item["key"])
        if new is not None and _identity(item) != _identity(now[new]):
            notes.append(
                f"第一次提交的 {item['key']}（{_describe(item)}）在修正轮里对应到 {new}（{_describe(now[new])}）；"
                f"如果这不是同一件事，原来的 {item['key']} 已不在清单里"
            )
    restored_keys = {item["key"] for item in restored}
    if restored:
        final["items"] += restored
        present = {item["key"] for item in final["items"]}
        first_links = {item["key"]: item["related_keys"] for item in first["items"]}
        first_types = {item["key"]: item.get("relation_types", {}) for item in first["items"]}
        back = dict(key_map)
        reverse = {new: old for old, new in key_map.items()}
        for item in final["items"]:
            types: dict[str, str]
            if item["key"] in restored_keys:
                old = reverse[item["key"]]
                links = [back.get(key) for key in first_links.get(old, [])]
                types = {back[key]: kind for key, kind in first_types.get(old, {}).items() if key in back}
            else:
                links = list(item["related_keys"])
                types = dict(item.get("relation_types", {}))
                old = reverse.get(item["key"])
                for key in first_links.get(old, []) if old else []:
                    if back.get(key) in restored_keys:
                        links.append(back[key])
                        types[back[key]] = first_types[old][key]
            item["related_keys"] = list(dict.fromkeys(key for key in links if key in present and key != item["key"]))
            item["relation_types"] = {key: types[key] for key in item["related_keys"] if key in types}
    # Every item the first coverage listed under a method goes back there
    # when it still has that method but the round left it out.
    settled_items = {item["key"]: item for item in final["items"]}
    for entry in first["coverage"]:
        listed = {
            key
            for other in final["coverage"]
            if other["method_id"] == entry["method_id"]
            for key in other["item_keys"]
        }
        lost = [
            key_map[key]
            for key in entry["item_keys"]
            if key_map.get(key) in settled_items
            and settled_items[key_map[key]]["finding_method"]["method_id"] == entry["method_id"]
            and key_map[key] not in listed
        ]
        if not lost:
            continue
        for key in entry["item_keys"]:
            if key_map.get(key) in lost and key not in flagged and key_map[key] not in restored_keys:
                issues.append(
                    {"key": key_map[key], "problem": "修正轮把这一项从方法覆盖里拿掉了，核对没有要求；这里按第一次提交放回"}
                )
        lost_meters = {meter for key in lost for meter in settled_items[key]["meter_ids"]}
        same = [other for other in final["coverage"] if other["method_id"] == entry["method_id"]]
        exact = [other for other in same if sorted(other["meter_ids"]) == sorted(entry["meter_ids"])]
        covering = [other for other in same if lost_meters <= set(other["meter_ids"])]
        if not (exact or covering):
            final["coverage"].append(
                {
                    **entry,
                    "item_keys": lost,
                    "rule_keys": list(entry["rule_keys"]),
                    "note": f"{entry['note']}（修正轮里没有这一条；第一次提交列在这里的项仍在清单里，按第一次提交记为 found）",
                }
            )
            continue
        target = (exact or covering)[0]
        target["item_keys"] = list(dict.fromkeys(target["item_keys"] + lost))
        target["rule_keys"] = list(dict.fromkeys(target["rule_keys"] + entry["rule_keys"]))
        if target["outcome"] != "found":
            target["note"] = (
                f"{target['note']}（修正轮改成了 {target['outcome']}；第一次提交列在这里的项仍在清单里，按第一次提交记为 found）"
            )
            target["outcome"] = "found"
    partition_flagged = "" in flagged
    # Coverage of a method may change with a flagged item's fix; other methods should not.
    touched = {item["finding_method"]["method_id"] for item in first["items"] if item["key"] in flagged}
    touched |= {now[pairs[key]]["finding_method"]["method_id"] for key in flagged if key in pairs}
    first_shape = sorted(
        (
            entry["method_id"],
            sorted(entry["meter_ids"]),
            entry["outcome"],
            entry["note"],
            sorted(entry["rule_keys"]),
            sorted(key_map.get(key, key) for key in entry["item_keys"]),
        )
        for entry in first["coverage"]
        if entry["method_id"] not in touched
    )
    final_shape = sorted(
        (
            entry["method_id"],
            sorted(entry["meter_ids"]),
            entry["outcome"],
            entry["note"].split("（修正轮改成了")[0].split("（修正轮里没有这一条")[0],
            sorted(entry["rule_keys"]),
            sorted(entry["item_keys"]),
        )
        for entry in final["coverage"]
        if entry["method_id"] not in touched
    )
    if not partition_flagged and first_shape != final_shape:
        issues.append({"key": "", "problem": "修正轮改了方法覆盖说明，但核对没有指出分区层面的问题"})
    if not partition_flagged and (
        first["rule_item_notes"] != final["rule_item_notes"] or first["notes"] != final["notes"]
    ):
        issues.append({"key": "", "problem": "修正轮改了规则项说明或分区说明，但核对没有指出分区层面的问题"})
    if not partition_flagged and (
        first.get("category_coverage", []) != final.get("category_coverage", [])
        or first.get("links", []) != final.get("links", [])
    ):
        issues.append({"key": "", "problem": "修正轮改了类别覆盖说明或项之间的关系，但核对没有指出分区层面的问题"})
    # Links of items the check did not flag should not change.
    first_links = {item["key"]: item.get("relation_types", {}) for item in first["items"]}
    settled_links = {item["key"]: item.get("relation_types", {}) for item in final["items"]}
    for old, new in pairs.items():
        if old in flagged:
            continue
        expected = {key_map[key]: kind for key, kind in first_links[old].items() if key in key_map}
        if expected != settled_links.get(new, {}):
            issues.append({"key": new, "problem": "修正轮改了这一项的相关项，但核对没有指出它的问题"})
    # Two settled items that say the same thing: one of them replaced another item.
    seen: dict[str, str] = {}
    for item in final["items"]:
        content = _content(item)
        if content in seen:
            issues.append({"key": item["key"], "problem": f"这一项和 {seen[content]} 内容完全相同"})
        else:
            seen[content] = item["key"]
    settled = PartitionSubmission.model_validate(final).model_dump(mode="json")
    return {
        "partition_id": partition_id,
        "submission": settled,
        "check": {"ok": not issues, "issues": issues},
        "notes": notes,
    }


__all__ = ["category_issues", "check_submission", "link_issues", "settle_partition"]
