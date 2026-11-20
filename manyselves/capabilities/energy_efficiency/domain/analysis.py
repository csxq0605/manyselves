"""The steps of the analysis as tools: S1 lenses, S2 items per system, S3 apply, S4 residual rounds, S5 judgments.

The analyst proposes the loss items and the program computes them: S1 runs
the lenses (the program's differences, the base and the ruler); in S2 the
analyst reads one system's differences and proposes items, each adopting
differences or bringing a reference spec, and says why every difference
large enough to matter is or is not part of an item (a large difference no
item adopts is still a row, at most pending: the analyst can only add); S3
computes every item over the whole period, with the stability checks and
the proposal's cause tests, and screens the rows; S4 asks about the largest excess no item
explains; S5 is one round of judgments on the computed rows.

Each step is deterministic around the model's turns.  The program writes a
brief, the model answers, the program checks the answer (sent back once
when something blocks), settles it and recomputes the ledger.  Briefs are
saved so the checks compare against what was asked.
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import ValidationError

from .company_model import brief_task
from .foundation import load_foundation
from .grid import PHASE_LABELS, PLANT_BY_POWER, Grid, load_grid
from .handles import handle_part
from .items import full_id, load_item_records, save_item_records
from .ledger import (
    ROUNDS_FILE,
    Ledger,
    compose,
    ledger_record,
    load_rounds,
    load_spec_records,
    save_spec_records,
    system_of,
    systems,
    write_ledger,
)
from .lenses import ANALYSIS_DIR, LENSES, run_lenses, save_lenses
from .preparation import PARAMETER_BY_KEY, ROLES
from .probes import RESULT_LABELS, TEST_TYPES, TestRequest, decompose, run_test
from .screen import INVALID, PENDING, RULES, VALID
from .specs import (
    DRIVERS,
    REFERENCE_TYPES,
    RESIDUAL_TYPES,
    DriverSpec,
    GroupingSpec,
    ReferenceSpec,
    ResidualSpec,
    basis_issues,
    spec_problems,
    unsourced_numbers,
)
from .store import SiteData, read_json, write_json
from .submissions import JudgmentSubmission, LookSubmission, ResidualSubmission

BRIEFS_DIR = f"{ANALYSIS_DIR}/briefs"
LOOKS_FILE = "looks.json"
JUDGMENTS_FILE = "judgments.json"
JUDGE_CHUNK = 25
ROUND_LIMIT = 3  # the workflow holds three residual rounds; P33 can lower it


@lru_cache(maxsize=1)
def kb_reference() -> list[dict[str, Any]]:
    """The knowledge base's step-4 items as reference text: scope, evidence and pitfalls (never a checklist)."""

    data = yaml.safe_load(Path(__file__).with_name("kb_categories.yaml").read_text(encoding="utf-8"))
    return [
        {key: entry[key] for key in ("id", "title", "scope", "evidence", "pitfalls") if key in entry}
        for entry in data["categories"]
    ]


def _save_brief(site: SiteData, brief: dict[str, Any]) -> None:
    write_json(site.path(f"{BRIEFS_DIR}/{brief['key']}.json"), brief)


def _brief(site: SiteData, key: str) -> dict[str, Any]:
    path = site.path(f"{BRIEFS_DIR}/{key}.json")
    if not path.is_file():
        raise ValueError(f"no brief {key} in run {site.run_id}")
    return read_json(path)


def _facts(site: SiteData) -> dict[str, Any]:
    return {fact.id: fact for fact in site.facts() or []}


def _fact_rows(site: SiteData) -> list[dict[str, Any]]:
    return [
        {"id": fact.id, "topic": fact.topic, "answer": fact.answer, "status": fact.status, "confirmed": fact.confirmed}
        for fact in site.facts() or []
    ]


def brief_assumptions(site: SiteData) -> list[dict[str, Any]]:
    """The inferences about what the data measures: a drafted one gives its reasoning and fact ids, not the facts' full text."""

    keys = ("id", "topic", "inference", "origin", "meter_ids", "affects")
    listed = []
    for entry in site.assumptions():
        row = {key: entry[key] for key in keys if entry.get(key)}
        if entry.get("origin") == "drafted":
            row["reasoning"] = entry.get("reasoning", "")
            row["basis_fact_ids"] = entry.get("basis_fact_ids", [])
        else:
            row["basis"] = entry["basis"]
        listed.append(row)
    return listed


def _context(site: SiteData) -> dict[str, Any]:
    """What every brief carries about the site: the survey answers, the inferences about the data, the task and the focus."""

    context: dict[str, Any] = {"facts": _fact_rows(site), "assumptions": brief_assumptions(site)}
    task = brief_task(site)
    if task:
        context["task"] = task
    focus = (site.task_scope() or {}).get("focus")
    if focus:
        context["focus"] = focus
    return context


def _axes(grid: Grid) -> dict[str, Any]:
    return {
        "period": {"first_day": grid.day_keys[0] if grid.day_keys else None, "last_day": grid.day_keys[-1] if grid.day_keys else None},
        "step_hours": grid.step_h,
        "plant": PLANT_BY_POWER,
        "registered": grid.plant_states,
        "lines": grid.lines,
        "bands": ["OFF", "LOW", "RUN"],
        "phases": PHASE_LABELS,
        "tariff": grid.tariff_names,
        "weekdays": "1 是周一，7 是周日",
        "clock": "HH:MM-HH:MM，可跨零点",
        "day_status": ["OPERATING", "REST"],
        "reference_types": REFERENCE_TYPES,
        "residual_types": RESIDUAL_TYPES,
        "drivers": {**DRIVERS, "<表编号>": "另一块表的日电量"},
    }


def _preferences(ledger: Ledger, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {"id": PARAMETER_BY_KEY[key].id, "key": key, "value": ledger.params.value(key), "rule": PARAMETER_BY_KEY[key].rule}
        for key in keys
    ]


def _meter_overview(grid: Grid, meter_id: str, rho: float | None, pws: dict[str, Any] | None) -> dict[str, Any]:
    energy = grid.energy_of(meter_id)
    total = float(energy.sum())
    blocks = np.bincount(grid.minute // 360, weights=energy, minlength=4)
    weekdays = np.bincount(grid.weekday - 1, weights=energy, minlength=7)
    return {
        "meter": meter_id,
        "name": grid.names.get(meter_id, ""),
        "is_line": meter_id in grid.line_index,
        "energy_kwh": round(total, 1),
        "data_coverage": round(grid.coverage(meter_id), 4),
        "mean_kw": round(float(grid.power_of(meter_id)[grid.ok_of(meter_id)].mean()), 2) if grid.ok_of(meter_id).any() else None,
        "plant_stop_kwh": round(float(energy[grid.stop].sum()), 1),
        "clock_share": {label: round(float(blocks[number]) / total, 3) if total else None for number, label in enumerate(("0-6点", "6-12点", "12-18点", "18-24点"))},
        "weekday_share": {"一二三四五六日"[number]: round(float(weekdays[number]) / total, 3) if total else None for number in range(7)},
        "production_correlation": None if rho is None else round(rho, 3),
        "stop_vs_run": None if pws is None else {key: pws.get(key) for key in ("stop_kw", "run_kw", "ratio", "stop_share")},
    }


def _candidate_brief(ledger: Ledger, candidate_id: str, full: bool) -> dict[str, Any]:
    """One row of a loss item: what the program computed for it over the whole period."""

    candidate = ledger.by_id[candidate_id]
    screen = ledger.screening[candidate_id]
    entry: dict[str, Any] = {
        "id": candidate.id,
        "item": candidate.item,
        "lens": candidate.lens,
        "lens_name": LENSES.get(candidate.lens, (candidate.lens, ""))[0],
        "meter": candidate.meter,
        "title": candidate.title,
        "excess_kwh": round(candidate.excess, 1),
        "default": candidate.default,
        "screen": f"{screen['result']}（{screen['rule']} {screen['reason']}）",
    }
    if not full:
        return entry
    details = {key: value for key, value in candidate.details.items() if key != "stability"}
    entry.update(
        {
            "selection": candidate.selection,
            "levels": candidate.levels,
            "conservative": candidate.conservative,
            "money": None if candidate.money is None else round(candidate.money, 1),
            "in_totals": candidate.in_union,
            "notes": candidate.notes,
            "overlaps": ledger.overlaps.get(candidate.id, [])[:5],
            "decomposition": decompose(ledger.grid, candidate, ledger.lens_run.batches),
            "stability": candidate.details.get("stability"),
            "details": details,
        }
    )
    return entry


SMALL_DIFFERENCE = "低于电量下限（P36、P37 或 P40）：单独采用也会筛为无效，只给概要；可以和别的放进同一个参照规格"


def _difference_brief(ledger: Ledger, difference_id: str) -> dict[str, Any]:
    """One program difference as the analyst reads it before proposing items.

    A difference whose own row would be screened invalid on its size (not in
    ``must_answer``) comes without its decomposition and details.
    """

    difference = ledger.difference_by_id[difference_id]
    entry: dict[str, Any] = {
        "id": difference.id,
        "lens": difference.lens,
        "lens_name": LENSES.get(difference.lens, (difference.lens, ""))[0],
        "meter": difference.meter,
        "part": difference.part,
        "title": difference.title,
        "selection": difference.selection,
        "excess_kwh": round(difference.excess, 1),
        "levels": difference.levels,
        "default": difference.default,
        "conservative": difference.conservative,
        "in_totals": difference.in_union,
        "money": None if difference.money is None else round(difference.money, 1),
        "notes": difference.notes,
        "overlaps": ledger.difference_overlaps.get(difference.id, [])[:5],
        "adopted_by": ledger.adopted.get(difference.id),
    }
    if difference.id in ledger.unclaimed:
        entry["unclaimed"] = True  # a row of a program item of its own (D-…), at most pending; the task text says so once
    if difference.id not in ledger.must_answer:
        return {**entry, "small": SMALL_DIFFERENCE}
    return {**entry, "decomposition": decompose(ledger.grid, difference, ledger.lens_run.batches), "details": difference.details}


def _item_summary(ledger: Ledger, item_id: str) -> dict[str, Any]:
    item = ledger.items[item_id]
    proposal = item["proposal"]
    return {
        "id": item_id,
        "origin": item["origin"],
        "source": item.get("source", "model"),
        "title": proposal.get("title", ""),
        "rows": item.get("rows", []),
        "meters": sorted({ledger.by_id[row].meter for row in item.get("rows", []) if row in ledger.by_id}),
        "finding": proposal.get("finding", ""),
        "reasoning": proposal.get("reasoning", ""),
        "causes": proposal.get("causes", []),
        "missing_info": proposal.get("missing_info", ""),
        "related": proposal.get("related", []),
        "set_aside": item.get("set_aside"),
    }


# S1 ------------------------------------------------------------------------------


def lens_step(workspace: Path, run_id: str) -> dict[str, Any]:
    """S1: run every lens, compose the ledger and write one look brief per system."""

    site = SiteData(workspace, run_id)
    grid = load_grid(site)
    foundation = load_foundation(site)
    confirmations = site.confirmations()
    run = run_lenses(grid, foundation, site.parameter_set(), list(confirmations.confirmations) if confirmations else [])
    save_lenses(site.path(ANALYSIS_DIR), run)
    save_spec_records(site, [])
    save_item_records(site, [], [])
    write_json(site.path(f"{ANALYSIS_DIR}/{ROUNDS_FILE}"), {"rounds": []})
    ledger = compose(site)
    write_ledger(site, ledger)
    briefs = look_briefs(site, ledger)
    for brief in briefs:
        _save_brief(site, brief)
    return {"run_id": run_id, "summary": ledger.summary(), "briefs": briefs}


def look_briefs(site: SiteData, ledger: Ledger) -> list[dict[str, Any]]:
    grid = ledger.grid
    foundation = load_foundation(site)
    pws = {row["meter"]: row for row in foundation.rows("PWS")} if "PWS" in foundation.tables else {}
    context = _context(site)
    must = set(ledger.must_answer)
    briefs = []
    for number, (system, meters) in enumerate(sorted(systems(grid).items()), start=1):
        ids = [difference.id for difference in ledger.differences if difference.meter in meters and difference.excess > 0]
        ids.sort(key=lambda item: -ledger.difference_by_id[item].excess)
        briefs.append(
            {
                "key": f"look-{handle_part(system)}",
                "run_id": site.run_id,
                "kind": "system",
                "system": system,
                "item_prefix": f"S{number}",
                "meters": [_meter_overview(grid, meter_id, ledger.lens_run.driven.get(meter_id), pws.get(meter_id)) for meter_id in meters],
                "lenses": {lens: LENSES[lens][1] for lens in LENSES},
                "differences": [_difference_brief(ledger, item) for item in ids],
                "must_answer": [item for item in ids if item in must],
                "triggers": [trigger for trigger in ledger.triggers if trigger["system"] == system or set(trigger["meters"]) & set(meters)],
                **context,
                "kb": kb_reference(),
                "axes": _axes(grid),
                "roles": ROLES,
                "test_types": TEST_TYPES,
                "preferences": _preferences(ledger, ("plant_stop_fraction", "idle_floor_quantile", "elasticity_trigger", "trigger_min_share", "screen_min_kwh", "screen_min_share")),
            }
        )
    return briefs


# Checks --------------------------------------------------------------------------


def _spec_index(submission: Any) -> dict[str, Any]:
    return {spec.id: spec for spec in submission.specs}


def _known_numbers(ledger: Ledger) -> list[float]:
    return [c.excess for c in ledger.differences] + [c.excess for c in ledger.candidates]


def _common_issues(site: SiteData, ledger: Ledger, submission: Any, meters: set[str] | None) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, list[str]]]:
    facts = _facts(site)
    issues: list[dict[str, str]] = []
    hints: list[dict[str, str]] = []
    problems: dict[str, list[str]] = {}
    seen: set[str] = set()
    foundation = load_foundation(site)
    groups = {row["group"] for row in foundation.rows("GRP")} if "GRP" in foundation.tables else set()
    known = set(ledger.items) | set(ledger.by_id)
    taken = {(record["spec"]["id"]) for record in load_spec_records(site) if record.get("accepted") and record["spec"].get("kind") == "reference"}
    for spec in submission.specs:
        if spec.id in seen:
            issues.append({"key": f"spec {spec.id}", "problem": "规格编号重复"})
        seen.add(spec.id)
        found = spec_problems(ledger.grid, spec, groups, known, meters)
        if isinstance(spec, ReferenceSpec) and meters is None and spec.id in taken:
            found.append(f"参照规格编号 {spec.id} 已经用过，换一个")
        problems[spec.id] = found
        issues += [{"key": f"spec {spec.id}", "problem": text} for text in found]
        issues += basis_issues(getattr(spec, "basis", []), facts, f"spec {spec.id}")
        for text in unsourced_numbers(spec.why, _known_numbers(ledger)):
            hints.append({"key": f"spec {spec.id}", "problem": f"“{text}”不是程序算的数；数由程序按规格算，why 里不用写"})
    return issues, hints, problems


def _item_issues(site: SiteData, ledger: Ledger, submission: Any, meters: set[str] | None, spec_problems_by_id: dict[str, list[str]]) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Per proposed item: what blocks it (an item with a blocker is not computed), and hints."""

    facts = _facts(site)
    specs = _spec_index(submission)
    local = [proposal.id for proposal in submission.items]
    existing = set(ledger.items)
    blocks: dict[str, list[str]] = defaultdict(list)
    hints: list[dict[str, str]] = []
    adopted_here: dict[str, str] = {}
    spec_owner: dict[str, list[str]] = defaultdict(list)
    for proposal in submission.items:
        problems = blocks[proposal.id]
        if local.count(proposal.id) > 1:
            problems.append("项编号重复")
        if not proposal.differences and not proposal.spec:
            problems.append("每项要采用至少一条程序差额（differences），或交一条参照规格（spec）")
        for difference_id in proposal.differences:
            difference = ledger.difference_by_id.get(difference_id)
            if difference is None:
                problems.append(f"没有程序差额 {difference_id}")
                continue
            if meters is not None and difference.meter not in meters:
                problems.append(f"差额 {difference_id} 不是本系统的表（{sorted(meters)}）")
            if difference.excess <= 0:
                problems.append(f"差额 {difference_id} 没有超出")
            if difference_id in ledger.adopted:
                problems.append(f"差额 {difference_id} 已被项 {ledger.adopted[difference_id]} 采用")
            elif difference_id in adopted_here:
                problems.append(f"差额 {difference_id} 也被本次的项 {adopted_here[difference_id]} 采用：一条差额只归一项")
            else:
                adopted_here[difference_id] = proposal.id
        if proposal.spec:
            spec = specs.get(proposal.spec)
            if spec is None:
                problems.append(f"spec 写的 {proposal.spec} 不在本次提交的 specs 里")
            elif not isinstance(spec, ReferenceSpec):
                problems.append(f"spec 要写参照规格（kind reference），{proposal.spec} 是 {spec.kind}")
            else:
                spec_owner[proposal.spec].append(proposal.id)
                if spec_problems_by_id.get(proposal.spec):
                    problems.append(f"参照规格 {proposal.spec} 算不了：{'；'.join(spec_problems_by_id[proposal.spec])}")
        kinds = {cause.kind for cause in proposal.causes}
        if kinds <= {"loss"}:
            problems.append("causes 里至少要有一条不是损耗的解释（necessary、data 或 external）：程序算出的超出不一定是损耗")
        cause_ids = [cause.id for cause in proposal.causes]
        if len(set(cause_ids)) != len(cause_ids):
            problems.append("原因编号重复")
        for cause in proposal.causes:
            problems += [issue["problem"] for issue in basis_issues(cause.basis, facts, cause.id)]
        unknown = [item for item in proposal.related if item not in local and item not in existing]
        if unknown:
            problems.append(f"related 里的 {unknown} 不是本次的项，也不是已有项的完整编号")
        numbers = _known_numbers(ledger)
        for text in (proposal.title, proposal.finding, proposal.reasoning):
            for found in unsourced_numbers(text, numbers):
                hints.append({"key": f"item {proposal.id}", "problem": f"“{found}”和程序算的数对不上；数由程序在全期算，文字里不用写"})
    for spec in submission.specs:
        if isinstance(spec, ReferenceSpec):
            owners = spec_owner.get(spec.id, [])
            if len(owners) != 1:
                blocks[f"spec {spec.id}"].append("参照规格要挂在一个项上（项的 spec 写它的编号），不能没有，也不能挂在几项上" if not owners else f"参照规格挂在了几项上：{owners}")
    return {key: value for key, value in blocks.items() if value}, hints


def _answer_issues(
    answers: list[Any],
    field: str,
    expected: list[str],
    specs: dict[str, Any],
    local_items: set[str],
    questions: list[Any],
    needs_specs: dict[str, tuple[type, ...]],
    needs_items: tuple[str, ...],
) -> list[dict[str, str]]:
    issues = []
    counts: dict[str, int] = defaultdict(int)
    for answer in answers:
        target = getattr(answer, field)
        counts[target] += 1
        if target not in expected:
            issues.append({"key": f"{field} {target}", "problem": f"说明包里没有这个编号（有：{expected}）"})
            continue
        for spec_id in answer.specs:
            if spec_id not in specs:
                issues.append({"key": f"{field} {target}", "problem": f"写的规格 {spec_id} 不在本次提交的 specs 里"})
        for item_id in answer.items:
            if item_id not in local_items:
                issues.append({"key": f"{field} {target}", "problem": f"写的项 {item_id} 不在本次提交的 items 里"})
        needed = needs_specs.get(answer.disposition)
        if needed and not any(isinstance(specs.get(spec_id), needed) for spec_id in answer.specs):
            names = "、".join({ReferenceSpec: "参照", ResidualSpec: "剩余解释", GroupingSpec: "可比分组", DriverSpec: "驱动假设"}[kind] for kind in needed)
            issues.append({"key": f"{field} {target}", "problem": f"处置 {answer.disposition} 要在 specs 里交{names}规格并写进本条的 specs"})
        if answer.disposition in needs_items and not [item for item in answer.items if item in local_items]:
            issues.append({"key": f"{field} {target}", "problem": f"处置 {answer.disposition} 要提一个项（items 里写本次提交的项编号）"})
        if answer.disposition == "needs_data":
            asked = any(target in question.settles for question in questions)
            requested = any(getattr(specs.get(spec_id), "request", "") for spec_id in answer.specs)
            if not asked and not requested:
                issues.append({"key": f"{field} {target}", "problem": "要补资料的写一条现场问题（settles 写本编号），或在规格里写 request"})
    for target in expected:
        if counts[target] == 0:
            issues.append({"key": f"{field} {target}", "problem": "没有处置：说明包里每个都要写一条"})
        elif counts[target] > 1:
            issues.append({"key": f"{field} {target}", "problem": "写了不止一条"})
    return issues


TRIGGER_NEEDS = {
    "comparable_group": (GroupingSpec,),
    "driver": (DriverSpec, ResidualSpec),
}
SLICE_NEEDS = {"explained": (ResidualSpec,)}


def _set_aside_issues(brief: dict[str, Any], ledger: Ledger, parsed: LookSubmission) -> list[dict[str, str]]:
    """Every difference in must_answer is adopted by an item of this submission or set aside, once."""

    issues = []
    adopted = {difference for proposal in parsed.items for difference in proposal.differences}
    meters = {entry["meter"] for entry in brief["meters"]}
    counts: dict[str, int] = defaultdict(int)
    for entry in parsed.set_aside:
        counts[entry.difference] += 1
        difference = ledger.difference_by_id.get(entry.difference)
        if difference is None or difference.meter not in meters:
            issues.append({"key": f"set_aside {entry.difference}", "problem": "不是本系统的程序差额"})
        elif entry.difference in adopted:
            issues.append({"key": f"set_aside {entry.difference}", "problem": "这条差额已被本次的项采用，不用再写暂不立项"})
    for difference_id in brief["must_answer"]:
        if difference_id in ledger.adopted:
            continue
        if difference_id not in adopted and counts[difference_id] == 0:
            issues.append({"key": f"difference {difference_id}", "problem": "够大的程序差额都要交代：被某项采用（differences），或在 set_aside 写为什么暂不立项"})
        elif counts[difference_id] > 1:
            issues.append({"key": f"difference {difference_id}", "problem": "set_aside 里写了不止一条"})
    return issues


def check_look(workspace: Path, run_id: str, key: str, submission: dict[str, Any]) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    brief = _brief(site, key)
    ledger = compose(site)
    if brief["kind"] == "system":
        parsed: Any = LookSubmission.model_validate(submission)
        meters: set[str] | None = {entry["meter"] for entry in brief["meters"]}
    else:
        parsed = ResidualSubmission.model_validate(submission)
        meters = None
    issues, hints, spec_problems_by_id = _common_issues(site, ledger, parsed, meters)
    blocks, item_hints = _item_issues(site, ledger, parsed, meters, spec_problems_by_id)
    hints += item_hints
    for label, problems in blocks.items():
        issues += [{"key": label if label.startswith("spec ") else f"item {label}", "problem": text} for text in problems]
    if parsed.key != key:
        issues.append({"key": "key", "problem": f"key 要原样写 {key}"})
    specs = _spec_index(parsed)
    local_items = {proposal.id for proposal in parsed.items}
    if brief["kind"] == "system":
        expected = [trigger["id"] for trigger in brief["triggers"]]
        issues += _answer_issues(parsed.triggers, "trigger", expected, specs, local_items, parsed.site_questions, TRIGGER_NEEDS, ("possible_waste",))
        issues += _set_aside_issues(brief, ledger, parsed)
        if not parsed.mechanisms:
            issues.append({"key": "mechanisms", "problem": "每个系统都要做一次领域先验和问卷检查：列出本系统该看的机理，各写结果"})
        facts = _facts(site)
        for number, mechanism in enumerate(parsed.mechanisms, start=1):
            label = f"mechanism {number}"
            if mechanism.outcome == "item" and not [item for item in mechanism.items if item in local_items]:
                issues.append({"key": label, "problem": "结果写 item 要在 items 里写本次提交的项编号"})
            issues += basis_issues(mechanism.basis, facts, label)
    else:
        expected = [entry["id"] for entry in brief["slices"]]
        issues += _answer_issues(parsed.slices, "slice", expected, specs, local_items, parsed.site_questions, SLICE_NEEDS, ("new_item",))
    return {"ok": not issues, "issues": issues, "hints": hints}


def settle(first_submission: dict[str, Any], first_check: dict[str, Any], corrected: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    """Keep the corrected answer when the correction round returned one; otherwise the first."""

    attempt = next(iter(corrected.values()), None)
    notes = []
    if attempt is not None:
        submission, check, used = attempt["submission"], attempt["check"], True
    else:
        submission, check, used = first_submission, first_check, False
        if errors:
            notes.append("退回修改的那一轮失败了，保留第一次的答复：" + "；".join(str(value)[:200] for value in errors.values()))
    if check.get("issues"):
        notes.append("核对仍有问题，有问题的项和规格不算数：" + "；".join(f"{issue['key']}：{issue['problem']}" for issue in check["issues"][:10]))
    return {"submission": submission, "check": check, "corrected": used, "notes": notes}


def _accepted(site: SiteData, ledger: Ledger, submission: Any, origin: str, prefix: str, meters: set[str] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The spec records and the item records of one settled answer; an item or spec with a blocker is kept but not computed."""

    _issues, _hints, problems = _common_issues(site, ledger, submission, meters)
    blocks, _item_hints = _item_issues(site, ledger, submission, meters, problems)
    facts = _facts(site)
    specs = []
    for spec in submission.specs:
        found = problems.get(spec.id, []) + [issue["problem"] for issue in basis_issues(getattr(spec, "basis", []), facts, spec.id)] + blocks.get(f"spec {spec.id}", [])
        specs.append({"origin": origin, "spec": spec.model_dump(mode="json", exclude_none=True), "accepted": not found, "problems": found})
    rejected = {record["spec"]["id"] for record in specs if not record["accepted"]}
    local = {proposal.id: full_id(prefix, proposal.id) for proposal in submission.items}
    items = []
    for proposal in submission.items:
        data = proposal.model_dump(mode="json", exclude_none=True)
        data["local_id"] = proposal.id
        data["id"] = local[proposal.id]
        data["related"] = [local.get(item, item) for item in proposal.related]
        found = list(blocks.get(proposal.id, []))
        if proposal.spec and proposal.spec in rejected:
            found.append(f"参照规格 {proposal.spec} 没有收下")
        items.append({"id": local[proposal.id], "origin": origin, "proposal": data, "accepted": not found, "problems": found})
    return specs, items


# S3 ------------------------------------------------------------------------------


def apply_looks(workspace: Path, run_id: str, results: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    """S3: the systems' items and specs into the ledger, computed over the whole period; one item per residual round the preferences allow."""

    site = SiteData(workspace, run_id)
    ledger = compose(site)
    kept = [record for record in load_spec_records(site) if not record["origin"].startswith("S2")]
    stored = load_item_records(site)
    items = [record for record in stored["items"] if not record["origin"].startswith("S2")]
    set_aside = [record for record in stored["set_aside"] if not record.get("origin", "").startswith("S2")]
    looks = {}
    for key, result in sorted(results.items()):
        brief = _brief(site, key)
        try:
            submission = LookSubmission.model_validate(result["submission"])
        except ValidationError as exc:
            looks[key] = {"system": brief["system"], "error": f"答复读不了：{exc.errors()[0]['msg']}"}
            continue
        meters = {entry["meter"] for entry in brief["meters"]}
        origin = f"S2 {brief['system']}"
        specs, proposals = _accepted(site, ledger, submission, origin, brief["item_prefix"], meters)
        kept += specs
        items += proposals
        set_aside += [{**entry.model_dump(mode="json"), "origin": origin} for entry in submission.set_aside]
        looks[key] = {"system": brief["system"], **result}
    for key, error in errors.items():
        looks[key] = {"system": _brief(site, key)["system"] if site.path(f"{BRIEFS_DIR}/{key}.json").is_file() else key, "error": str(error)[:500]}
    save_spec_records(site, kept)
    save_item_records(site, items, set_aside)
    write_json(site.path(f"{ANALYSIS_DIR}/{LOOKS_FILE}"), {"looks": looks})
    ledger = compose(site)
    write_ledger(site, ledger)
    rounds = min(int(ledger.params.value("residual_rounds")), ROUND_LIMIT)
    return {
        "run_id": run_id,
        "summary": ledger.summary(),
        "rounds": [{"key": f"R{number}", "run_id": run_id, "round": number} for number in range(1, rounds + 1)],
    }


# S4 ------------------------------------------------------------------------------


def _model_explained(ledger: Ledger) -> float:
    return float((ledger.residual * ledger.residual_model).sum())


def residual_brief(workspace: Path, run_id: str, number: int) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    ledger = compose(site)
    rounds = load_rounds(site)
    limit = min(int(ledger.params.value("residual_rounds")), ROUND_LIMIT)
    if number > limit or any(entry.get("stopped") for entry in rounds if entry["round"] < number):
        return {"run_id": run_id, "round": number, "briefs": []}
    open_slices = [entry for entry in ledger.slices if entry["kwh"] - entry["model_explained_kwh"] > 0]
    if not open_slices:
        return {"run_id": run_id, "round": number, "briefs": []}
    grid = ledger.grid
    set_aside = {entry["difference"]: entry for entry in ledger.set_aside}
    meters = {entry["meter"] for entry in open_slices}
    free = [
        difference
        for difference in ledger.differences
        if difference.meter in meters and difference.excess > 0 and difference.id not in ledger.adopted
    ]
    free.sort(key=lambda difference: -difference.excess)
    brief = {
        "key": f"residual-R{number}",
        "run_id": run_id,
        "kind": "residual",
        "round": number,
        "item_prefix": f"R{number}",
        "residual_open_kwh": round(ledger.residual_total(), 1),
        "model_explained_kwh": round(_model_explained(ledger), 1),
        "found_kwh": round(ledger.found_total(), 1),
        "slices": open_slices,
        "differences": [
            {**_difference_brief(ledger, difference.id), "set_aside": set_aside.get(difference.id)}
            for difference in free
        ],
        # The analyst's and the rules' items; the large differences nobody adopted are in ``differences`` (unclaimed).
        "items_so_far": [_item_summary(ledger, item_id) for item_id, item in ledger.items.items() if item.get("accepted") and item.get("source") != "program"],
        "specs_so_far": [
            {"origin": record["origin"], "kind": record["spec"]["kind"], "id": record["spec"]["id"], "objects": record["spec"].get("objects"), "accepted": record["accepted"], "why": record["spec"].get("why", "")[:120]}
            for record in ledger.specs
        ],
        "rounds_so_far": load_rounds(site),
        **_context(site),
        "kb": [{"id": entry["id"], "title": entry["title"]} for entry in kb_reference()],
        "axes": _axes(grid),
        "roles": ROLES,
        "test_types": TEST_TYPES,
    }
    _save_brief(site, brief)
    return {"run_id": run_id, "round": number, "briefs": [brief]}


def apply_residual(workspace: Path, run_id: str, number: int, results: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    key = f"residual-R{number}"
    path = site.path(f"{BRIEFS_DIR}/{key}.json")
    rounds = [entry for entry in load_rounds(site) if entry["round"] != number]
    if not path.is_file() or (not results and not errors):
        return {"run_id": run_id, "round": number, "ran": False}
    brief = read_json(path)
    origin = f"S4 第 {number} 轮"
    records = [record for record in load_spec_records(site) if record["origin"] != origin]
    stored = load_item_records(site)
    items = [record for record in stored["items"] if record["origin"] != origin]
    ledger = compose(site)
    answers: dict[str, Any] = {}
    for result in results.values():
        try:
            submission = ResidualSubmission.model_validate(result["submission"])
        except ValidationError:
            continue
        specs, proposals = _accepted(site, ledger, submission, origin, brief["item_prefix"], None)
        records += specs
        items += proposals
        answers = {"submission": result["submission"], "check": result["check"], "notes": result.get("notes", [])}
    save_spec_records(site, records)
    save_item_records(site, items, stored["set_aside"])
    ledger = compose(site)
    write_ledger(site, ledger)
    gain = (ledger.found_total() - brief["found_kwh"]) + (_model_explained(ledger) - brief["model_explained_kwh"])
    share = float(ledger.params.value("residual_stop_share"))
    before = brief["residual_open_kwh"] - brief["model_explained_kwh"]
    nothing = bool(answers) and all(
        entry["disposition"] in ("cannot_tell", "needs_data") for entry in answers["submission"].get("slices", [])
    )
    stopped = bool(errors) or gain < share * max(before, 0.0) or nothing
    reason = (
        "这一轮失败" if errors else "剩余前几片都标成看不出或要补资料" if nothing else f"新增 {gain:.0f} kWh，少于剩余 {before:.0f} kWh × P34 {share:g}" if stopped else ""
    )
    rounds.append(
        {
            "round": number,
            "residual_before_kwh": round(before, 1),
            "gain_kwh": round(gain, 1),
            "residual_after_kwh": round(ledger.residual_total() - _model_explained(ledger), 1),
            "stopped": stopped,
            "reason": reason,
            "answers": answers,
            "error": "；".join(str(value)[:300] for value in errors.values()) if errors else None,
        }
    )
    rounds.sort(key=lambda entry: entry["round"])
    write_json(site.path(f"{ANALYSIS_DIR}/{ROUNDS_FILE}"), {"rounds": rounds})
    return {"run_id": run_id, "round": number, "ran": True, "stopped": stopped, "gain_kwh": round(gain, 1), "summary": ledger.summary()}


# S5 ------------------------------------------------------------------------------


def _probe_context(site: SiteData) -> dict[str, Any]:
    foundation = load_foundation(site)
    return {row["meter"]: row for row in foundation.rows("PWS")} if "PWS" in foundation.tables else {}


def proposal_tests(site: SiteData, ledger: Ledger) -> dict[str, list[dict[str, Any]]]:
    """The cause tests written when the items were proposed, run on each row of the item."""

    pws = _probe_context(site)
    results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in ledger.candidates:
        item = ledger.items.get(candidate.item or "")
        if not item:
            continue
        for cause in item["proposal"].get("causes", []):
            if not cause.get("test"):
                continue
            outcome = run_test(ledger.grid, candidate, TestRequest.model_validate(cause["test"]), ledger.params, pws, ledger.lens_run.batches)
            results[candidate.id].append({"candidate": candidate.id, "cause": cause["id"], **outcome, "result_label": RESULT_LABELS[outcome["result"]]})
    return dict(results)


def judge_briefs(workspace: Path, run_id: str) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    ledger = compose(site)
    write_ledger(site, ledger)
    grid = ledger.grid
    looks = looks_of(site)
    context = _context(site)
    tests = proposal_tests(site, ledger)
    briefs = []
    for system, meters in sorted(systems(grid).items()):
        judged = [
            candidate
            for candidate in ledger.candidates
            if candidate.meter in meters and ledger.screening[candidate.id]["result"] in (VALID, PENDING) and candidate.excess > 0
        ]
        if not judged:
            continue
        invalid = [candidate for candidate in ledger.candidates if candidate.meter in meters and ledger.screening[candidate.id]["result"] == INVALID and candidate.excess > 0]
        by_meter: dict[str, list[str]] = defaultdict(list)
        for candidate in judged:
            by_meter[candidate.meter].append(candidate.id)
        chunks: list[list[str]] = [[]]
        for meter_id in meters:
            ids = by_meter.get(meter_id, [])
            if chunks[-1] and len(chunks[-1]) + len(ids) > JUDGE_CHUNK:
                chunks.append([])
            chunks[-1].extend(ids)
        look = [entry for entry in looks.values() if entry.get("system") == system]
        for number, ids in enumerate(chunk for chunk in chunks if chunk):
            item_ids = sorted({ledger.by_id[row].item for row in ids if ledger.by_id[row].item})
            brief = {
                "key": f"judge-{handle_part(system)}" + (f"-{number + 1}" if len(chunks) > 1 else ""),
                "run_id": run_id,
                "kind": "judge",
                "system": system,
                "judge": [{**_candidate_brief(ledger, row, full=True), "tests": tests.get(row, [])} for row in ids],
                "items": [_item_summary(ledger, item_id) for item_id in item_ids if item_id in ledger.items],
                "invalid": [_candidate_brief(ledger, candidate.id, full=False) for candidate in invalid],
                "screen_rules": {rule: f"{outcome}：{text}" for rule, (outcome, text) in RULES.items()},
                "system_look": [
                    {"triggers": entry.get("submission", {}).get("triggers", []), "mechanisms": entry.get("submission", {}).get("mechanisms", [])}
                    for entry in look
                    if "submission" in entry
                ],
                **context,
                "kb": kb_reference(),
                "roles": ROLES,
                "test_types": TEST_TYPES,
                "axes": _axes(grid),
            }
            _save_brief(site, brief)
            briefs.append(brief)
    return {"run_id": run_id, "briefs": briefs}


def probe_results(site: SiteData, ledger: Ledger, submission: JudgmentSubmission) -> list[dict[str, Any]]:
    pws = _probe_context(site)
    results = []
    for judgment in submission.judgments:
        candidate = ledger.by_id.get(judgment.candidate)
        if candidate is None:
            continue
        for cause in judgment.causes:
            if cause.test is None:
                continue
            outcome = run_test(ledger.grid, candidate, cause.test, ledger.params, pws, ledger.lens_run.batches)
            results.append({"candidate": candidate.id, "cause": cause.id, **outcome, "result_label": RESULT_LABELS[outcome["result"]]})
    return results


def check_judgment(workspace: Path, run_id: str, key: str, submission: dict[str, Any]) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    brief = _brief(site, key)
    ledger = compose(site)
    parsed = JudgmentSubmission.model_validate(submission)
    facts = _facts(site)
    issues: list[dict[str, str]] = []
    hints: list[dict[str, str]] = []
    if parsed.key != key:
        issues.append({"key": "key", "problem": f"key 要原样写 {key}"})
    expected = [entry["id"] for entry in brief["judge"]]
    counts: dict[str, int] = defaultdict(int)
    # The tests written with the proposal, and any new test written with the judgment (which counts over the old one).
    tests = {(entry["candidate"], entry["cause"]): entry["result"] for rows in proposal_tests(site, ledger).values() for entry in rows}
    tests.update({(entry["candidate"], entry["cause"]): entry["result"] for entry in probe_results(site, ledger, parsed)})
    known = set(ledger.by_id) | set(ledger.items)
    for judgment in parsed.judgments:
        label = f"candidate {judgment.candidate}"
        counts[judgment.candidate] += 1
        candidate = ledger.by_id.get(judgment.candidate)
        if candidate is None:
            issues.append({"key": label, "problem": "没有这一行"})
            continue
        labels = [level["label"] for level in candidate.levels]
        if judgment.reference_choice not in [*labels, "other"]:
            issues.append({"key": label, "problem": f"reference_choice 写 levels 里的 label（{labels}）或 other"})
        causes = {cause.id for cause in judgment.causes}
        if len(causes) != len(judgment.causes):
            issues.append({"key": label, "problem": "原因编号重复"})
        for cause in judgment.causes:
            issues += basis_issues(cause.basis, facts, f"{label} {cause.id}")
            result = tests.get((candidate.id, cause.id))
            if cause.status == "kept" and result == "contradicts":
                issues.append({"key": f"{label} {cause.id}", "problem": "检验结果和这个原因矛盾，不能写 kept：写 dropped，或写 open 并提一个现场问题"})
        for measure in judgment.measures:
            missing = [item for item in measure.depends_on if item not in causes]
            if missing:
                issues.append({"key": label, "problem": f"措施依赖的原因 {missing} 不在本条的 causes 里"})
            if measure.type in ("repair", "retrofit"):
                supported = [item for item in measure.depends_on if tests.get((candidate.id, item)) == "supports"]
                if not supported:
                    issues.append({"key": label, "problem": f"原因没确认时只给管理和运行调整：{measure.type} 要依赖一个检验支持的原因（depends_on）"})
        unknown = [item for item in judgment.related if item not in known]
        if unknown:
            issues.append({"key": label, "problem": f"related 里的 {unknown} 不是行或项的编号"})
        numbers = [level["excess_kwh"] for level in candidate.levels] + [candidate.excess]
        texts = [judgment.reference_reason, judgment.note, *[cause.cause for cause in judgment.causes], *[measure.action for measure in judgment.measures]]
        for text in texts:
            for found in unsourced_numbers(text, numbers):
                hints.append({"key": label, "problem": f"“{found}”和程序算的数对不上；数以台账为准"})
    for item in expected:
        if counts[item] == 0:
            issues.append({"key": f"candidate {item}", "problem": "没有判断：judge 里每一行都要写一条"})
        elif counts[item] > 1:
            issues.append({"key": f"candidate {item}", "problem": "写了不止一条"})
    return {"ok": not issues, "issues": issues, "hints": hints}


def save_judgments(site: SiteData, results: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    data = {"results": results, "errors": {key: str(value)[:500] for key, value in errors.items()}}
    write_json(site.path(f"{ANALYSIS_DIR}/{JUDGMENTS_FILE}"), data)
    return data


def looks_of(site: SiteData) -> dict[str, Any]:
    path = site.path(f"{ANALYSIS_DIR}/{LOOKS_FILE}")
    return read_json(path)["looks"] if path.is_file() else {}


__all__ = [
    "BRIEFS_DIR",
    "brief_assumptions",
    "apply_looks",
    "apply_residual",
    "check_judgment",
    "check_look",
    "judge_briefs",
    "kb_reference",
    "lens_step",
    "ledger_record",
    "looks_of",
    "probe_results",
    "proposal_tests",
    "residual_brief",
    "save_judgments",
    "settle",
    "system_of",
]
