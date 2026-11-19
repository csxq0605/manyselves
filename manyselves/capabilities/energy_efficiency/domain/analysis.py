"""The steps of the analysis as tools: S1 lenses, S2 looks per system, S3 apply, S4 residual rounds, S5 judgments.

Each step is deterministic around the model's turns.  The program writes a
brief, the model answers with specs and opinions, the program checks the
answer (sent back once when something blocks), settles it and recomputes
the ledger.  Briefs are saved so the checks compare against what was asked.
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
from .probes import RESULT_LABELS, TEST_TYPES, decompose, run_test
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
    candidate = ledger.by_id[candidate_id]
    screen = ledger.screening[candidate_id]
    entry: dict[str, Any] = {
        "id": candidate.id,
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
            "details": candidate.details,
        }
    )
    return entry


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
    briefs = []
    for system, meters in sorted(systems(grid).items()):
        ids = [candidate.id for candidate in ledger.candidates if candidate.meter in meters and candidate.excess > 0]
        ids.sort(key=lambda item: -ledger.by_id[item].excess)
        residual = {
            meter_id: round(float(ledger.residual[grid.row(meter_id)].sum()), 1) for meter_id in meters
        }
        briefs.append(
            {
                "key": f"look-{handle_part(system)}",
                "run_id": site.run_id,
                "kind": "system",
                "system": system,
                "meters": [_meter_overview(grid, meter_id, ledger.lens_run.driven.get(meter_id), pws.get(meter_id)) for meter_id in meters],
                "lenses": {lens: LENSES[lens][1] for lens in LENSES},
                "candidates": [_candidate_brief(ledger, item, full=False) for item in ids[:20]],
                "candidates_left": max(0, len(ids) - 20),
                "triggers": [trigger for trigger in ledger.triggers if trigger["system"] == system or set(trigger["meters"]) & set(meters)],
                "residual_kwh": residual,
                "slices": [entry for entry in ledger.slices if entry["meter"] in meters],
                **context,
                "kb": kb_reference(),
                "axes": _axes(grid),
                "preferences": _preferences(ledger, ("plant_stop_fraction", "idle_floor_quantile", "elasticity_trigger", "trigger_min_share", "screen_min_kwh")),
            }
        )
    return briefs


# Checks --------------------------------------------------------------------------


def _spec_index(submission: Any) -> dict[str, Any]:
    return {spec.id: spec for spec in submission.specs}


def _common_issues(site: SiteData, ledger: Ledger, submission: Any, meters: set[str] | None) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, list[str]]]:
    facts = _facts(site)
    issues: list[dict[str, str]] = []
    hints: list[dict[str, str]] = []
    problems: dict[str, list[str]] = {}
    seen: set[str] = set()
    foundation = load_foundation(site)
    groups = {row["group"] for row in foundation.rows("GRP")} if "GRP" in foundation.tables else set()
    known = {candidate.id for candidate in ledger.candidates}
    for spec in submission.specs:
        if spec.id in seen:
            issues.append({"key": f"spec {spec.id}", "problem": "规格编号重复"})
        seen.add(spec.id)
        found = spec_problems(ledger.grid, spec, groups, known, meters)
        problems[spec.id] = found
        issues += [{"key": f"spec {spec.id}", "problem": text} for text in found]
        issues += basis_issues(getattr(spec, "basis", []), facts, f"spec {spec.id}")
        for text in unsourced_numbers(spec.why, [c.excess for c in ledger.candidates]):
            hints.append({"key": f"spec {spec.id}", "problem": f"“{text}”不是程序算的数；数由程序按规格算，why 里不用写"})
    return issues, hints, problems


def _disposition_issues(kind_needed: dict[str, tuple[type, ...]], answers: list[Any], field: str, specs: dict[str, Any], expected: list[str], questions: list[Any]) -> list[dict[str, str]]:
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
        needed = kind_needed.get(answer.disposition)
        if needed and not any(isinstance(specs.get(spec_id), needed) for spec_id in answer.specs):
            names = "、".join({ReferenceSpec: "参照", ResidualSpec: "剩余解释", GroupingSpec: "可比分组", DriverSpec: "驱动假设"}[kind] for kind in needed)
            issues.append({"key": f"{field} {target}", "problem": f"处置 {answer.disposition} 要在 specs 里交{names}规格并写进本条的 specs"})
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
    "possible_waste": (ReferenceSpec,),
    "comparable_group": (GroupingSpec,),
    "driver": (DriverSpec, ResidualSpec),
}
SLICE_NEEDS = {"explained": (ResidualSpec,), "new_reference": (ReferenceSpec,)}


def check_look(workspace: Path, run_id: str, key: str, submission: dict[str, Any]) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    brief = _brief(site, key)
    ledger = compose(site)
    if brief["kind"] == "system":
        parsed: Any = LookSubmission.model_validate(submission)
        meters = {entry["meter"] for entry in brief["meters"]}
    else:
        parsed = ResidualSubmission.model_validate(submission)
        meters = None
    issues, hints, _problems = _common_issues(site, ledger, parsed, meters)
    if parsed.key != key:
        issues.append({"key": "key", "problem": f"key 要原样写 {key}"})
    specs = _spec_index(parsed)
    if brief["kind"] == "system":
        expected = [trigger["id"] for trigger in brief["triggers"]]
        issues += _disposition_issues(TRIGGER_NEEDS, parsed.triggers, "trigger", specs, expected, parsed.site_questions)
        if not parsed.mechanisms:
            issues.append({"key": "mechanisms", "problem": "每个系统都要做一次领域先验和问卷检查：列出本系统该看的机理，各写结果"})
        facts = _facts(site)
        known = {candidate.id for candidate in ledger.candidates}
        for number, mechanism in enumerate(parsed.mechanisms, start=1):
            label = f"mechanism {number}"
            if mechanism.outcome == "spec" and not [spec_id for spec_id in mechanism.specs if spec_id in specs]:
                issues.append({"key": label, "problem": "结果写 spec 要在 specs 里交规格并写进本条的 specs"})
            if mechanism.outcome == "covered":
                missing = [item for item in mechanism.covered_by if item not in known]
                if not mechanism.covered_by or missing:
                    issues.append({"key": label, "problem": f"结果写 covered 要在 covered_by 里写已有候选的编号（{missing or '没写'} 不对）"})
            issues += basis_issues(mechanism.basis, facts, label)
    else:
        expected = [entry["id"] for entry in brief["slices"]]
        issues += _disposition_issues(SLICE_NEEDS, parsed.slices, "slice", specs, expected, parsed.site_questions)
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
        notes.append("核对仍有问题，有问题的规格不算数：" + "；".join(f"{issue['key']}：{issue['problem']}" for issue in check["issues"][:10]))
    return {"submission": submission, "check": check, "corrected": used, "notes": notes}


def _accepted_records(site: SiteData, ledger: Ledger, submission: Any, origin: str, meters: set[str] | None) -> list[dict[str, Any]]:
    _issues, _hints, problems = _common_issues(site, ledger, submission, meters)
    facts = _facts(site)
    records = []
    for spec in submission.specs:
        found = problems.get(spec.id, []) + [issue["problem"] for issue in basis_issues(getattr(spec, "basis", []), facts, spec.id)]
        records.append({"origin": origin, "spec": spec.model_dump(mode="json", exclude_none=True), "accepted": not found, "problems": found})
    return records


# S3 ------------------------------------------------------------------------------


def apply_looks(workspace: Path, run_id: str, results: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    """S3: the systems' specs into the ledger; one item per residual round the preferences allow."""

    site = SiteData(workspace, run_id)
    ledger = compose(site)
    kept = [record for record in load_spec_records(site) if not record["origin"].startswith("S2")]
    looks = {}
    for key, result in sorted(results.items()):
        brief = _brief(site, key)
        try:
            submission = LookSubmission.model_validate(result["submission"])
        except ValidationError as exc:
            looks[key] = {"system": brief["system"], "error": f"答复读不了：{exc.errors()[0]['msg']}"}
            continue
        meters = {entry["meter"] for entry in brief["meters"]}
        kept += _accepted_records(site, ledger, submission, f"S2 {brief['system']}", meters)
        looks[key] = {"system": brief["system"], **result}
    for key, error in errors.items():
        looks[key] = {"system": _brief(site, key)["system"] if site.path(f"{BRIEFS_DIR}/{key}.json").is_file() else key, "error": str(error)[:500]}
    save_spec_records(site, kept)
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
    others = [record for record in ledger.specs]
    brief = {
        "key": f"residual-R{number}",
        "run_id": run_id,
        "kind": "residual",
        "round": number,
        "residual_open_kwh": round(ledger.residual_total(), 1),
        "model_explained_kwh": round(_model_explained(ledger), 1),
        "found_kwh": round(ledger.found_total(), 1),
        "slices": open_slices,
        "candidates": [_candidate_brief(ledger, candidate.id, full=False) for candidate in ledger.candidates if candidate.excess > 0],
        "specs_so_far": [
            {"origin": record["origin"], "kind": record["spec"]["kind"], "id": record["spec"]["id"], "objects": record["spec"].get("objects"), "accepted": record["accepted"], "why": record["spec"].get("why", "")[:120]}
            for record in others
        ],
        "rounds_so_far": load_rounds(site),
        **_context(site),
        "kb": [{"id": entry["id"], "title": entry["title"]} for entry in kb_reference()],
        "axes": _axes(grid),
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
    ledger = compose(site)
    answers: dict[str, Any] = {}
    for result in results.values():
        try:
            submission = ResidualSubmission.model_validate(result["submission"])
        except ValidationError:
            continue
        records += _accepted_records(site, ledger, submission, origin, None)
        answers = {"submission": result["submission"], "check": result["check"], "notes": result.get("notes", [])}
    save_spec_records(site, records)
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


def judge_briefs(workspace: Path, run_id: str) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    ledger = compose(site)
    write_ledger(site, ledger)
    grid = ledger.grid
    looks = looks_of(site)
    context = _context(site)
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
            brief = {
                "key": f"judge-{handle_part(system)}" + (f"-{number + 1}" if len(chunks) > 1 else ""),
                "run_id": run_id,
                "kind": "judge",
                "system": system,
                "judge": [_candidate_brief(ledger, item, full=True) for item in ids],
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


def _probe_context(site: SiteData) -> dict[str, Any]:
    foundation = load_foundation(site)
    return {row["meter"]: row for row in foundation.rows("PWS")} if "PWS" in foundation.tables else {}


def run_probes(workspace: Path, run_id: str, key: str, submission: dict[str, Any]) -> dict[str, Any]:
    site = SiteData(workspace, run_id)
    ledger = compose(site)
    parsed = JudgmentSubmission.model_validate(submission)
    return {"key": key, "results": probe_results(site, ledger, parsed)}


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
    tests = {(entry["candidate"], entry["cause"]): entry["result"] for entry in probe_results(site, ledger, parsed)}
    known = set(ledger.by_id)
    for judgment in parsed.judgments:
        label = f"candidate {judgment.candidate}"
        counts[judgment.candidate] += 1
        candidate = ledger.by_id.get(judgment.candidate)
        if candidate is None:
            issues.append({"key": label, "problem": "没有这个候选"})
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
            issues.append({"key": label, "problem": f"related 里的 {unknown} 不是候选编号"})
        numbers = [level["excess_kwh"] for level in candidate.levels] + [candidate.excess]
        texts = [judgment.reference_reason, judgment.note, *[cause.cause for cause in judgment.causes], *[measure.action for measure in judgment.measures]]
        for text in texts:
            for found in unsourced_numbers(text, numbers):
                hints.append({"key": label, "problem": f"“{found}”和程序算的数对不上；数以台账为准"})
    for item in expected:
        if counts[item] == 0:
            issues.append({"key": f"candidate {item}", "problem": "没有判断：judge 里每个候选都要写一条"})
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
    "residual_brief",
    "run_probes",
    "save_judgments",
    "settle",
    "system_of",
]
