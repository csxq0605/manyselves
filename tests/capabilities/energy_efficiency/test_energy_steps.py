"""The program steps on a made-up site: prepare, finder tools, check, replay, select, report."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from manyselves.capabilities.energy_efficiency.adapters.runtime import (
    start_input,
    tool_implementations,
)
from manyselves.capabilities.energy_efficiency.domain.check import settle_partition
from manyselves.capabilities.energy_efficiency.domain.items import KB_ITEMS
from manyselves.capabilities.energy_efficiency.domain.methods import MENU
from manyselves.capabilities.energy_efficiency.domain.selection import (
    SelectionError,
    load_rules,
    select,
)
from manyselves.capabilities.energy_efficiency.domain.store import read_json
from tests.capabilities.energy_efficiency.synthetic import write_site

RUN = "candidates-1"


def categories(meter_ids: list[str], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Category coverage for every category the items do not propose on a meter."""

    proposed = {
        (proposal["item_id"], meter_id)
        for item in items
        for proposal in item.get("candidate_categories", [])
        for meter_id in item["meter_ids"]
    }
    entries = []
    for item_id in KB_ITEMS:
        left = [meter_id for meter_id in meter_ids if (item_id, meter_id) not in proposed]
        if left:
            entries.append({"item_id": item_id, "meter_ids": left, "outcome": "none_found", "note": "看过"})
    return entries


def _covering(tools: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Tests that are not about category coverage get it filled in for every category left."""

    check = tools["check-partition-items"]

    def checked(values: dict[str, Any]) -> dict[str, Any]:
        submission = dict(values["submission"])
        if "category_coverage" not in submission:
            run_id, _sep, partition_id = values["scope"].partition("#")
            partitions = read_json(workspace / "Work/runs" / run_id / "energy/partitions.json")["partitions"]
            meters = next((item["meter_ids"] for item in partitions if item["partition_id"] == partition_id), [])
            submission["category_coverage"] = categories(meters, submission.get("items", []))
        return check({**values, "submission": submission})

    return {**tools, "check-partition-items": checked}


def _prepared(workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    write_site(workspace / "data/site")
    start_input(workspace)(RUN, "energy-candidates", {"input_dir": "data/site"})
    tools = _covering(tool_implementations(workspace), workspace)
    tools["parse-energy-inputs"]({"run_id": RUN})
    tools["profile-energy-data"]({"run_id": RUN})
    tools["run-energy-prechecks"]({"run_id": RUN})
    partitions = tools["partition-energy-meters"]({"run_id": RUN, "max_meters": 6})
    return tools, {brief["partition_id"]: brief for brief in partitions["partitions"]}


def _coverage(meter_ids: list[str], found: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {
            "method_id": method_id,
            "meter_ids": meter_ids,
            "outcome": "found" if method_id in found else "none_found",
            "note": "查过",
            "item_keys": found.get(method_id, []),
        }
        for method_id in MENU
    ]


def _item(key: str, meter: str, evidence: list[str], method: dict[str, Any], window: tuple[str, str]) -> dict[str, Any]:
    return {
        "key": key,
        "meter_ids": [meter],
        "windows": [{"start": window[0], "end": window[1]}],
        "finding_method": method,
        "phenomenon": "现象",
        "evidence_ids": evidence,
        "reasoning": "从画像看到",
        "root_cause_candidates": [
            {"cause": "设备未停", "kind": "loss", "basis": "data", "basis_detail": evidence[0], "how_to_check": "问值班"},
            {"cause": "夜间有必要的工艺维持", "kind": "necessary_service", "basis": "general_knowledge", "basis_detail": "保温工艺", "how_to_check": "问工艺"},
        ],
        "candidate_categories": [{"item_id": "LOSS-IDLE", "why": "非生产时段功率不降"}],
    }


NIGHT = {
    "method_id": "daily-clock-window",
    "params": {"start_time": "20:00", "end_time": "08:00", "min_avg_kw": 5},
    "description": "每天夜间",
}
GAPS = {
    "method_id": "production-gaps",
    "params": {"machine": "IM-01", "min_gap_minutes": 30, "max_gap_minutes": 120},
    "description": "批次间隙",
}


def test_prepare_reads_the_site_and_lists_rule_items(tmp_path: Path) -> None:
    _tools, briefs = _prepared(tmp_path)

    assert set(briefs) == {"P01", "P02"}
    assert [meter["meter_id"] for meter in briefs["P02"]["meters"]] == ["IM1", "LT1"]
    assert briefs["P01"]["scope"] == f"{RUN}#P01"
    assert [item["rule_key"] for item in briefs["P01"]["rule_items"]] == ["R1"]
    assert briefs["P02"]["rule_items"] == []
    assert "不做差分" in briefs["P02"]["profile_text"]
    assert "不能换算成电量" in briefs["P02"]["profile_text"]
    assert {method["id"] for method in briefs["P01"]["methods"]} == set(MENU)
    prechecks = read_json(tmp_path / "Work/runs" / RUN / "energy/prechecks.json")
    assert prechecks["outcomes"][0]["hits"] == 13


def test_finder_tools_only_read_their_own_partition(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    window = {"start": "2026-03-02 00:00", "end": "2026-03-03 00:00"}

    other = tools["energy-series"]({"scope": f"{RUN}#P01", "meter_id": "IM1", **window})
    assert other["status"] == "error" and "不在分区" in other["error"]
    bad_scope = tools["energy-series"]({"scope": RUN, "meter_id": "AC1", **window})
    assert bad_scope["status"] == "error"
    too_long = tools["energy-series"](
        {"scope": f"{RUN}#P01", "meter_id": "AC1", "start": "2026-03-02", "end": "2026-03-05", "granularity": "native"}
    )
    assert too_long["status"] == "error"
    machine = tools["production-records"]({"scope": f"{RUN}#P01", "machine": "IM-01"})
    assert machine["status"] == "error"

    hourly = tools["energy-series"]({"scope": f"{RUN}#P01", "meter_id": "AC1", **window})
    assert len(hourly["rows"]) == 24
    assert hourly["rows"][0]["energy_kwh"] == 12.0
    assert hourly["evidence_id"].startswith("EV-")
    again = tools["energy-series"]({"scope": f"{RUN}#P01", "meter_id": "AC1", **window})
    assert again["evidence_id"] == hourly["evidence_id"]


def test_check_flags_what_the_data_does_not_support(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    gaps = tools["try-method"]({"scope": f"{RUN}#P02", "meter_ids": ["IM1"], **{k: GAPS[k] for k in ("method_id", "params")}})
    assert gaps["runs"][0]["summary"]["occurrences"] == 10
    submission = {
        "partition_id": "P02",
        "items": [
            _item("B1", "IM1", [gaps["evidence_id"]], GAPS, ("2026-03-02 11:30", "2026-03-02 12:30")),
            # Cites the moulding machine's evidence for the lighting meter, and
            # a production method on a meter with no machine.
            _item("B2", "LT1", [gaps["evidence_id"], "EV-0000000000"], GAPS, ("2026-04-01 00:00", "2026-04-02 00:00")),
        ],
        "coverage": _coverage(["IM1", "LT1"], {"production-gaps": ["B1", "B2"]}),
        "rule_item_notes": [{"rule_key": "R1", "note": "不是本分区的"}],
    }
    result = tools["check-partition-items"]({"scope": f"{RUN}#P02", "submission": submission})

    assert result["ok"] is False
    assert not [issue for issue in result["issues"] if issue["key"] == "B1"]
    b2 = " ".join(issue["problem"] for issue in result["issues"] if issue["key"] == "B2")
    assert "不是本项的表" in b2
    assert "EV-0000000000 不存在" in b2
    assert "完全在数据期之外" in b2
    assert "不能用" in b2
    assert any(issue["key"] == "" and "R1 不属于本分区" in issue["problem"] for issue in result["issues"])


def _candidates(tools: dict[str, Any]) -> dict[str, Any]:
    night = tools["try-method"]({"scope": f"{RUN}#P01", "meter_ids": ["AC1"], "method_id": NIGHT["method_id"], "params": NIGHT["params"]})
    gaps = tools["try-method"]({"scope": f"{RUN}#P02", "meter_ids": ["IM1"], "method_id": GAPS["method_id"], "params": GAPS["params"]})
    p01 = {
        "partition_id": "P01",
        "items": [_item("A1", "AC1", [night["evidence_id"]], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))],
        "coverage": _coverage(["AC1"], {"daily-clock-window": ["A1"]}),
        "rule_item_notes": [{"rule_key": "R1", "note": "与 A1 是同一现象的一部分"}],
    }
    p02 = {
        "partition_id": "P02",
        "items": [_item("B1", "IM1", [gaps["evidence_id"]], GAPS, ("2026-03-02 11:30", "2026-03-02 12:30"))],
        "coverage": _coverage(["IM1", "LT1"], {"production-gaps": ["B1"]}),
    }
    results = {}
    for key, submission in (("P01", p01), ("P02", p02)):
        check = tools["check-partition-items"]({"scope": f"{RUN}#{key}", "submission": submission})
        assert check == {"ok": True, "issues": []}
        results[key] = {"partition_id": key, "submission": submission, "check": check}
    return tools["quantify-candidates"]({"run_id": RUN, "partition_results": results, "partition_errors": {}})


def test_replay_recomputes_every_item_over_the_whole_period(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    summary = _candidates(tools)

    assert summary["counts"] == {"model_items": 2, "rule_items": 1, "partitions": 2, "partitions_failed": 0, "plant_items": 0, "plant_pass": None, "assumptions_drafted": 0, "assumption_draft": None}
    register = read_json(tmp_path / "Work/runs" / RUN / "energy/register.json")
    items = {item["id"]: item for item in register["items"]}
    assert set(items) == {"L001", "L002", "R001"}
    night, gaps, rule = items["L001"], items["L002"], items["R001"]
    # 13 whole nights 20:00-08:00 at 12 kW.
    assert night["scalars"]["replay_occurrences"] == 13
    assert night["scalars"]["replay_item_kwh"] == 13 * 12 * 12
    # Ten weekday lunch gaps of one hour at 8 kW.
    assert gaps["scalars"]["replay_item_kwh"] == 80.0
    assert gaps["windows"][0]["start"] == "2026-03-02 11:30:00"
    # The rule night 22:00-06:00 lies inside the model's night: shared records.
    assert night["scalars"]["overlap_items"] == 1
    assert night["scalars"]["overlap_max_shared_kwh"] == rule["scalars"]["replay_item_kwh"]
    assert rule["rule_notes"] == ["P01：与 A1 是同一现象的一部分"]
    # Tariff 08-22 at 1.0, 22-08 at 0.4: 24 kWh at 1.0 and 120 kWh at 0.4.
    assert night["scalars"]["window_cost"] == 72.0
    coverage = {entry["method_id"]: entry for entry in register["partitions"][0]["coverage"]}
    assert coverage["daily-clock-window"]["item_ids"] == ["L001"]
    for name in ("register.json", "register.xlsx", "register.html"):
        assert (tmp_path / "Work/runs" / RUN / "energy" / name).is_file()


def test_marks_win_over_rules_and_the_report_has_no_totals(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    book = openpyxl.load_workbook(tmp_path / "Work/runs" / RUN / "energy/register.xlsx")
    sheet = book["候选清单"]
    header = [cell.value for cell in sheet[1]]
    rows = {row[0].value: row for row in sheet.iter_rows(min_row=2)}
    rows["L001"][header.index("选择")].value = "不保留"
    rows["L001"][header.index("备注")].value = "夜间要供气"
    (tmp_path / "marks").mkdir()
    book.save(tmp_path / "marks/marked.xlsx")
    (tmp_path / "marks/rules.yaml").write_text(
        "rules:\n"
        "- when:\n  - {field: replay_item_kwh, op: gte, value: 1000}\n  decision: keep\n  note: 全期超过 1000 kWh\n",
        encoding="utf-8",
    )
    values = {"source_run_id": RUN, "marks_file": "marks/marked.xlsx", "rules_file": "marks/rules.yaml"}
    initial = start_input(tmp_path)("report-1", "energy-report", values)
    selection = tools["select-energy-items"]({"run_id": "report-1", "request": initial["request"]})
    decisions = {entry["item_id"]: entry for entry in selection["decisions"]}

    assert decisions["L001"] == {"item_id": "L001", "decision": "drop", "by": "person", "note": "夜间要供气"}
    assert decisions["R001"]["decision"] == "keep" and decisions["R001"]["by"] == "rule 1"
    assert decisions["L002"]["decision"] == "unselected"

    report = tools["render-energy-report"]({"run_id": "report-1", "request": initial["request"], "selection": selection})
    text = (tmp_path / report["files"][0]).read_text(encoding="utf-8")
    kept = text.split("## 保留的候选损耗项")[1].split("## 需补资料的项")[0]
    assert "### R001" in kept and "### L001" not in kept and "### L002" not in kept
    assert "不给合计" in text and "合计：" not in text
    assert "候选原因（均未核实）" not in kept  # rule items carry no model causes


def test_rules_read_only_program_fields(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("rules:\n- when:\n  - {field: phenomenon, op: eq, value: x}\n  decision: keep\n", encoding="utf-8")
    with pytest.raises(SelectionError, match="program fields"):
        load_rules(path)


def test_report_input_needs_a_register_and_something_to_select_with(tmp_path: Path) -> None:
    project = start_input(tmp_path)
    with pytest.raises(ValueError, match="marks_file, rules_file"):
        project("report-2", "energy-report", {"source_run_id": RUN})
    with pytest.raises(ValueError, match="no candidate register"):
        project("report-2", "energy-report", {"source_run_id": RUN, "rules_file": "rules.yaml"})


def _marks_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["编号", "核对码", "选择", "备注"])
        writer.writerows(rows)


def test_marks_csv_with_unknown_ids_is_refused(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    _marks_csv(tmp_path / "marks.csv", [["L009", "C00000000", "保留", ""]])
    # Refused when the report starts, so the person can fix the file and start again.
    with pytest.raises(ValueError, match="L009"):
        start_input(tmp_path)("report-3", "energy-report", {"source_run_id": RUN, "marks_file": "marks.csv"})
    assert not (tmp_path / "Work/runs/report-3/energy-selection").exists()


def test_marks_must_carry_the_check_code_of_the_same_item(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    register = read_json(tmp_path / "Work/runs" / RUN / "energy/register.json")
    codes = {item["id"]: item["check_code"] for item in register["items"]}
    project = start_input(tmp_path)

    # Rows swapped by hand: L001's mark carries L002's code.
    _marks_csv(tmp_path / "swapped.csv", [["L001", codes["L002"], "保留", ""]])
    with pytest.raises(ValueError, match="核对码"):
        project("report-4", "energy-report", {"source_run_id": RUN, "marks_file": "swapped.csv"})

    # No code column at all.
    with (tmp_path / "bare.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([["编号", "选择"], ["L001", "保留"]])
    with pytest.raises(ValueError, match="核对码"):
        project("report-5", "energy-report", {"source_run_id": RUN, "marks_file": "bare.csv"})
    # Codes start with a letter, so a spreadsheet keeps them as text.
    assert all(code[0].isalpha() for code in codes.values())

    _marks_csv(tmp_path / "right.csv", [["L001", codes["L001"], "保留", "看过"]])
    initial = project("report-6", "energy-report", {"source_run_id": RUN, "marks_file": "right.csv"})
    selection = tools["select-energy-items"]({"run_id": "report-6", "request": initial["request"]})
    assert {entry["item_id"]: entry["decision"] for entry in selection["decisions"]}["L001"] == "keep"


def test_the_report_reads_the_register_as_it_was_when_the_report_started(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    (tmp_path / "rules.yaml").write_text(
        "rules:\n- when:\n  - {field: source, op: eq, value: rule}\n  decision: keep\n", encoding="utf-8"
    )
    initial = start_input(tmp_path)("report-7", "energy-report", {"source_run_id": RUN, "rules_file": "rules.yaml"})
    # The candidate run's register is changed after the report run started.
    source = tmp_path / "Work/runs" / RUN / "energy/register.json"
    changed = read_json(source)
    changed["items"] = []
    source.write_text(__import__("json").dumps(changed), encoding="utf-8")

    selection = tools["select-energy-items"]({"run_id": "report-7", "request": initial["request"]})
    assert selection["counts"]["keep"] == 1
    report = tools["render-energy-report"]({"run_id": "report-7", "request": initial["request"], "selection": selection})
    assert "### R001" in (tmp_path / report["files"][0]).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ("{field: meter_ids, op: in, value: [AC1]}", "cannot use in"),
        ("{field: replay_item_kwh, op: gte, value: '1000'}", "is a number"),
        ("{field: replay_item_kwh, op: gte, value: true}", "is a number"),
        ("{field: source, op: eq, value: models}", "source is one of"),
        ("{field: partition_id, op: in, value: P01}", "non-empty list"),
        ("{field: window_cost, op: is_null, value: 0}", "takes no value"),
        ("{field: method_id, op: gt, value: a}", "cannot use gt"),
        ("{field: method_id, op: eq, value: daily-clock}", "method_id is one of"),
        ("{field: candidate_categories, op: contains, value: LOSS-NIGHT}", "candidate_categories holds ids"),
        ("{field: candidate_categories, op: eq, value: LOSS-IDLE}", "cannot use eq"),
    ],
)
def test_rules_that_do_not_fit_their_field_are_refused(tmp_path: Path, condition: str, message: str) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(f"rules:\n- when:\n  - {condition}\n  decision: keep\n", encoding="utf-8")
    with pytest.raises(SelectionError, match=message):
        load_rules(path)


def test_an_empty_number_matches_only_is_null(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "- when:\n  - {field: window_cost, op: ne, value: 0}\n  decision: keep\n"
        "- when:\n  - {field: meter_ids, op: not_contains, value: AC1}\n  decision: drop\n"
        "- when:\n  - {field: window_cost, op: is_null}\n  decision: needs_data\n",
        encoding="utf-8",
    )
    register = {
        "run_id": RUN,
        "items": [
            {"id": "L001", "meter_ids": ["AC1"], "scalars": {"window_cost": None}},
            {"id": "L002", "meter_ids": ["IM1"], "scalars": {"window_cost": None}},
            {"id": "L003", "meter_ids": ["AC1"], "scalars": {"window_cost": 5.0}},
        ],
    }
    decisions = {entry["item_id"]: entry for entry in select(register, None, load_rules(path))["decisions"]}

    assert decisions["L001"]["decision"] == "needs_data" and decisions["L001"]["by"] == "rule 3"
    assert decisions["L002"]["decision"] == "drop"
    assert decisions["L003"]["decision"] == "keep"


def test_check_asks_evidence_about_the_item_and_causes_that_cite_it(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    scope = f"{RUN}#P01"
    first_day = tools["energy-series"]({"scope": scope, "meter_id": "AC1", "start": "2026-03-02 00:00", "end": "2026-03-03 00:00"})
    item = _item("A1", "AC1", [first_day["evidence_id"]], NIGHT, ("2026-03-10 20:00", "2026-03-11 08:00"))
    item["root_cause_candidates"].append(
        {"cause": "管网泄漏", "kind": "loss", "basis": "data", "basis_detail": "夜间功率不降", "how_to_check": "听漏"}
    )
    item["root_cause_candidates"].append(
        {"cause": "备用机", "kind": "loss", "basis": "data", "basis_detail": "见 EV-0123456789", "how_to_check": "看机组"}
    )
    late = _item("A2", "AC1", [first_day["evidence_id"]], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    late["windows"].append({"start": "2026-03-20 00:00", "end": "2026-03-21 00:00"})
    submission = {
        "partition_id": "P01",
        "items": [item, late],
        "coverage": _coverage(["AC1"], {"daily-clock-window": ["A1", "A2"]}),
    }
    issues = tools["check-partition-items"]({"scope": scope, "submission": submission})["issues"]
    a1 = " ".join(issue["problem"] for issue in issues if issue["key"] == "A1")
    a2 = " ".join(issue["problem"] for issue in issues if issue["key"] == "A2")

    assert "没有交集" in a1  # evidence of 2 March for a window on 10 March
    assert "basis_detail 要写本项的证据编号" in a1
    assert "EV-0123456789" in a1 and "不在本项的 evidence_ids" in a1
    assert "完全在数据期之外" in a2


def _submission(*keys: str) -> dict[str, Any]:
    items = [
        _item(key, "AC1", ["EV-0123456789"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00")) for key in keys
    ]
    return {
        "partition_id": "P01",
        "items": items,
        "coverage": _coverage(["AC1"], {"daily-clock-window": list(keys)} if keys else {}),
    }


def test_settling_puts_back_an_item_the_model_dropped_while_correcting() -> None:
    first = _submission("A1", "A2")
    first_check = {"ok": False, "issues": [{"key": "A2", "problem": "证据不对"}]}
    corrected = {"P01": {"submission": _submission("A1"), "check": {"ok": True, "issues": []}}}
    result = settle_partition("P01", first, first_check, corrected, {})

    assert [item["key"] for item in result["submission"]["items"]] == ["A1", "A2"]
    problems = [issue["problem"] for issue in result["check"]["issues"] if issue["key"] == "A2"]
    assert "证据不对" in problems
    assert any("删掉了这一项" in problem for problem in problems)


def test_settling_keeps_the_first_submission_when_correction_fails() -> None:
    first = _submission("A1")
    first_check = {"ok": False, "issues": [{"key": "A1", "problem": "证据不对"}]}
    result = settle_partition("P01", first, first_check, {}, {"P01": "model gave up"})

    assert [item["key"] for item in result["submission"]["items"]] == ["A1"]
    assert result["check"]["issues"] == first_check["issues"]
    assert "修正轮没有完成" in result["notes"][0]


def test_settling_flags_an_item_changed_without_being_asked() -> None:
    first = _submission("A1", "A2")
    changed = _submission("A1", "A2")
    changed["items"][1]["phenomenon"] = "改写过的现象"
    corrected = {"P01": {"submission": changed, "check": {"ok": True, "issues": []}}}
    result = settle_partition("P01", first, {"ok": False, "issues": [{"key": "A1", "problem": "x"}]}, corrected, {})

    assert [issue["key"] for issue in result["check"]["issues"]] == ["A2"]
    assert "核对没有指出它的问题" in result["check"]["issues"][0]["problem"]


def test_an_item_on_a_meter_the_site_does_not_have_is_kept_and_not_counted(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    submission = _submission("A1")
    submission["items"][0]["meter_ids"] = ["XX9"]
    submission["items"][0]["finding_method"] = {"method_id": "none", "params": {}, "description": "看画像"}
    check = tools["check-partition-items"]({"scope": f"{RUN}#P01", "submission": submission})
    assert "不存在" in check["issues"][0]["problem"]
    results = {
        "P01": {"partition_id": "P01", "submission": submission, "check": check},
        "P02": {"partition_id": "P02", "submission": {"partition_id": "P02", "coverage": _coverage(["IM1", "LT1"], {})}, "check": {"ok": True, "issues": []}},
    }
    tools["quantify-candidates"]({"run_id": RUN, "partition_results": results, "partition_errors": {}})
    register = read_json(tmp_path / "Work/runs" / RUN / "energy/register.json")
    item = next(item for item in register["items"] if item["source"] == "model")

    assert item["computed"]["per_meter"]["XX9"]["unknown_meter"] is True
    assert item["scalars"]["window_energy_kwh"] is None
    assert item["check_issues"]
    page = (tmp_path / "Work/runs" / RUN / "energy/register.html").read_text(encoding="utf-8")
    assert "不在资料里" in page


def test_text_that_looks_like_a_formula_stays_text_in_the_register(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    night = tools["try-method"]({"scope": f"{RUN}#P01", "meter_ids": ["AC1"], "method_id": NIGHT["method_id"], "params": NIGHT["params"]})
    submission = {
        "partition_id": "P01",
        "items": [_item("A1", "AC1", [night["evidence_id"]], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))],
        "coverage": _coverage(["AC1"], {"daily-clock-window": ["A1"]}),
    }
    submission["items"][0]["phenomenon"] = '=HYPERLINK("http://example.invalid","x")'
    results = {
        "P01": {"partition_id": "P01", "submission": submission, "check": {"ok": True, "issues": []}},
        "P02": {"partition_id": "P02", "submission": {"partition_id": "P02", "coverage": _coverage(["IM1", "LT1"], {})}, "check": {"ok": True, "issues": []}},
    }
    tools["quantify-candidates"]({"run_id": RUN, "partition_results": results, "partition_errors": {}})
    sheet = openpyxl.load_workbook(tmp_path / "Work/runs" / RUN / "energy/register.xlsx")["候选清单"]
    header = [cell.value for cell in sheet[1]]
    cell = next(row[header.index("现象")] for row in sheet.iter_rows(min_row=2) if row[0].value == "L001")

    assert cell.data_type == "s"
    assert cell.value.startswith("=HYPERLINK")


# --- review round two ------------------------------------------------------


def _quantified(tools: dict[str, Any], p01_items: list[dict[str, Any]], found: dict[str, list[str]]) -> dict[str, Any]:
    results = {
        "P01": {
            "partition_id": "P01",
            "submission": {"partition_id": "P01", "items": p01_items, "coverage": _coverage(["AC1"], found)},
            "check": {"ok": True, "issues": []},
        },
        "P02": {
            "partition_id": "P02",
            "submission": {"partition_id": "P02", "coverage": _coverage(["IM1", "LT1"], {})},
            "check": {"ok": True, "issues": []},
        },
    }
    tools["quantify-candidates"]({"run_id": RUN, "partition_results": results, "partition_errors": {}})
    register = read_json(Path(tools["__workspace__"]) / "Work/runs" / RUN / "energy/register.json")
    return {item["local_key"]: item for item in register["items"] if item["source"] == "model"}


def test_stability_numbers_compare_average_power(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    noon = {"method_id": "daily-clock-window", "params": {"start_time": "12:00", "end_time": "13:00", "days": "weekday"}, "description": "午间"}
    evidence = tools["try-method"]({"scope": f"{RUN}#P01", "meter_ids": ["AC1"], "method_id": noon["method_id"], "params": noon["params"]})
    items = _quantified(tools, [_item("A1", "AC1", [evidence["evidence_id"]], noon, ("2026-03-02 12:00", "2026-03-02 13:00"))], {"daily-clock-window": ["A1"]})

    # A flat 40 kW load: moving or narrowing the hour changes nothing, and the halves are equal.
    assert items["A1"]["scalars"]["boundary_max_change"] == 0.0
    assert items["A1"]["scalars"]["halves_ratio"] == 1.0


def test_no_replay_cost_when_the_method_could_not_run(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    # No machine is mapped to the compressor, so production gaps cannot run on it.
    item = _item("A1", "AC1", ["EV-0123456789"], GAPS, ("2026-03-02 11:30", "2026-03-02 12:30"))
    items = _quantified(tools, [item], {"production-gaps": ["A1"]})
    scalars = items["A1"]["scalars"]

    assert scalars["window_cost"] is not None
    assert scalars["replay_occurrences"] is None and scalars["replay_cost"] is None


def test_control_characters_do_not_stop_the_spreadsheet(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    item = _item("A1", "AC1", ["EV-0123456789"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    item["phenomenon"] = "夜间平均 12 kW\x0b13 个夜晚"
    _quantified(tools, [item], {"daily-clock-window": ["A1"]})
    sheet = openpyxl.load_workbook(tmp_path / "Work/runs" / RUN / "energy/register.xlsx")["候选清单"]
    header = [cell.value for cell in sheet[1]]

    assert sheet.cell(row=2, column=header.index("现象") + 1).value == "夜间平均 12 kW 13 个夜晚"


def test_number_hints_read_numbers_against_units_and_grouped_numbers() -> None:
    from manyselves.capabilities.energy_efficiency.domain.quantify import number_hints

    known = [12.0, 1872.0]
    assert number_hints(["夜间平均功率12kW，全期1,872 kWh"], known) == []
    assert len(number_hints(["夜间平均功率35kW，全期9999kWh"], known)) == 2
    assert number_hints(["见 EV-0123456789 和 L001"], known) == []


def test_data_tools_refuse_times_with_a_zone(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    result = tools["energy-series"](
        {"scope": f"{RUN}#P01", "meter_id": "AC1", "start": "2026-03-02T20:00:00+08:00", "end": "2026-03-03T08:00:00+08:00"}
    )
    assert result["status"] == "error" and "time zone" in result["error"]


def test_a_check_that_breaks_becomes_an_issue_not_a_lost_partition(tmp_path: Path, monkeypatch) -> None:
    from manyselves.capabilities.energy_efficiency.domain import check as check_module

    tools, _briefs = _prepared(tmp_path)
    night = tools["try-method"]({"scope": f"{RUN}#P01", "meter_ids": ["AC1"], "method_id": NIGHT["method_id"], "params": NIGHT["params"]})
    series = tools["energy-series"]({"scope": f"{RUN}#P01", "meter_id": "AC1", "start": "2026-03-02 20:00", "end": "2026-03-03 08:00"})

    def broken(_record):
        raise TypeError("cannot compare")

    monkeypatch.setattr(check_module, "_spans", broken)
    submission = {
        "partition_id": "P01",
        "items": [_item("A1", "AC1", [night["evidence_id"], series["evidence_id"]], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))],
        "coverage": _coverage(["AC1"], {"daily-clock-window": ["A1"]}),
    }
    result = tools["check-partition-items"]({"scope": f"{RUN}#P01", "submission": submission})

    assert result["ok"] is False
    assert "程序核对这一项时出错" in result["issues"][0]["problem"]


def test_a_wrong_meter_does_not_hide_the_item_s_other_problems(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    item = _item("A1", "AC-1", ["EV-0123456789"], {"method_id": "none", "params": {}, "description": "看画像"}, ("2026-03-02 20:00", "2026-03-03 08:00"))
    submission = {"partition_id": "P01", "items": [item], "coverage": _coverage(["AC1"], {})}
    problems = [issue["problem"] for issue in tools["check-partition-items"]({"scope": f"{RUN}#P01", "submission": submission})["issues"]]

    assert any("不存在（本分区的表" in problem for problem in problems)
    assert any("EV-0123456789 不存在" in problem for problem in problems)


def test_settling_sees_through_renumbering() -> None:
    first = _submission("A1", "A2")
    first["items"][0]["phenomenon"] = "X：空压机夜间"
    first["items"][1]["phenomenon"] = "Y：另一现象"
    first_check = {"ok": False, "issues": [{"key": "A1", "problem": "证据不对"}]}
    # The model drops A1 and renumbers A2 as A1.
    corrected_items = [dict(first["items"][1], key="A1")]
    corrected = {
        "P01": {
            "submission": {
                "partition_id": "P01",
                "items": corrected_items,
                "coverage": _coverage(["AC1"], {"daily-clock-window": ["A1"]}),
            },
            "check": {"ok": True, "issues": []},
        }
    }
    result = settle_partition("P01", first, first_check, corrected, {})
    items = {item["key"]: item["phenomenon"] for item in result["submission"]["items"]}

    assert sorted(items.values()) == ["X：空压机夜间", "Y：另一现象"]  # X is back, Y is there once
    restored = next(key for key, text in items.items() if text.startswith("X"))
    assert restored != "A1"
    problems = " ".join(issue["problem"] for issue in result["check"]["issues"])
    assert "原编号 A1" in problems and "从 A2 改成了 A1" in problems
    coverage = next(entry for entry in result["submission"]["coverage"] if entry["method_id"] == "daily-clock-window")
    assert restored in coverage["item_keys"]


def test_settling_puts_a_dropped_item_back_into_the_coverage() -> None:
    first = _submission("A1")
    first_check = {"ok": False, "issues": [{"key": "A1", "problem": "证据不对"}]}
    corrected = {
        "P01": {
            "submission": {"partition_id": "P01", "items": [], "coverage": _coverage(["AC1"], {})},
            "check": {"ok": True, "issues": []},
        }
    }
    result = settle_partition("P01", first, first_check, corrected, {})
    coverage = next(entry for entry in result["submission"]["coverage"] if entry["method_id"] == "daily-clock-window")

    assert coverage["outcome"] == "found" and coverage["item_keys"] == ["A1"]


def test_rule_values_that_name_nothing_in_the_register_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("rules:\n- when:\n  - {field: partition_id, op: eq, value: P1}\n  decision: keep\n", encoding="utf-8")
    register = {"run_id": RUN, "items": [{"id": "L001", "partition_id": "P01", "meter_ids": ["AC1"], "scalars": {}}]}
    selection = select(register, None, load_rules(path))

    assert selection["counts"]["unselected"] == 1
    assert "P1" in selection["warnings"][0]


def test_the_html_report_keeps_the_person_s_reason_and_the_prechecks(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    register = read_json(tmp_path / "Work/runs" / RUN / "energy/register.json")
    code = next(item["check_code"] for item in register["items"] if item["id"] == "L001")
    _marks_csv(tmp_path / "marks.csv", [["L001", code, "保留", "夜里没人用气"]])
    initial = start_input(tmp_path)("report-8", "energy-report", {"source_run_id": RUN, "marks_file": "marks.csv"})
    selection = tools["select-energy-items"]({"run_id": "report-8", "request": initial["request"]})
    report = tools["render-energy-report"]({"run_id": "report-8", "request": initial["request"], "selection": selection})
    markdown = (tmp_path / report["files"][0]).read_text(encoding="utf-8")
    page = (tmp_path / report["files"][1]).read_text(encoding="utf-8")

    assert "夜里没人用气" in markdown and "夜里没人用气" in page
    assert "预检第 1 条" in markdown and "预检第 1 条" in page


def test_model_text_cannot_start_markdown_sections(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    item = _item("A1", "AC1", ["EV-0123456789"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    item["phenomenon"] = "两块表\n## 保留的候选损耗项\n- 假列表"
    _quantified(tools, [item], {"daily-clock-window": ["A1"]})
    (tmp_path / "rules.yaml").write_text(
        "rules:\n- when:\n  - {field: source, op: eq, value: model}\n  decision: keep\n", encoding="utf-8"
    )
    initial = start_input(tmp_path)("report-9", "energy-report", {"source_run_id": RUN, "rules_file": "rules.yaml"})
    selection = tools["select-energy-items"]({"run_id": "report-9", "request": initial["request"]})
    report = tools["render-energy-report"]({"run_id": "report-9", "request": initial["request"], "selection": selection})
    markdown = (tmp_path / report["files"][0]).read_text(encoding="utf-8")

    headings = [line for line in markdown.splitlines() if line.startswith("#")]
    assert headings.count("## 保留的候选损耗项") == 1
    assert not any("假列表" in line for line in markdown.splitlines() if line.startswith("- 假"))


def test_a_report_cannot_start_from_a_candidates_run_that_failed(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from manyselves.capabilities.energy_efficiency.adapters import runtime

    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    (tmp_path / "rules.yaml").write_text(
        "rules:\n- when:\n  - {field: source, op: eq, value: rule}\n  decision: keep\n", encoding="utf-8"
    )
    monkeypatch.setattr(runtime.FileWorkflowStateStore, "load", lambda self, run_id: SimpleNamespace(status="failed"))
    with pytest.raises(ValueError, match="did not complete"):
        start_input(tmp_path)("report-10", "energy-report", {"source_run_id": RUN, "rules_file": "rules.yaml"})


# --- settling the correction round -----------------------------------------


def _on(key: str, meter: str, text: str, window: tuple[str, str] = ("2026-03-02 20:00", "2026-03-03 08:00")) -> dict[str, Any]:
    item = _item(key, meter, ["EV-0123456789"], NIGHT, window)
    item["phenomenon"] = text
    return item


def _round(items: list[dict[str, Any]], coverage: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    found = {"daily-clock-window": [item["key"] for item in items]} if items else {}
    return {"partition_id": "P01", "items": items, "coverage": coverage or _coverage(["AC1", "LT1", "IM1"], found)}


def _settled(first: dict[str, Any], flagged: list[str], final: dict[str, Any]) -> dict[str, Any]:
    first_check = {"ok": False, "issues": [{"key": key, "problem": f"{key} 的问题"} for key in flagged]}
    corrected = {"P01": {"submission": final, "check": {"ok": True, "issues": []}}}
    return settle_partition("P01", first, first_check, corrected, {})


def test_a_dropped_item_is_not_taken_for_another_item_s_renumbered_fix() -> None:
    first = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y"), _on("A3", "IM1", "Z")])
    # The model drops A1, fixes A2 and renumbers: A1 is now Y's fix, A2 is Z.
    final = _round([_on("A1", "LT1", "Y 改过"), _on("A2", "IM1", "Z")])
    result = _settled(first, ["A1", "A2"], final)
    items = {item["key"]: item["phenomenon"] for item in result["submission"]["items"]}

    assert sorted(items.values()) == ["X", "Y 改过", "Z"]
    problems = " ".join(issue["problem"] for issue in result["check"]["issues"])
    assert "原编号 A1" in problems and "从 A2 改成了 A1" in problems


def test_long_keys_restored_twice_still_fit() -> None:
    keys = [f"IM1-moulding-gap-{n}" for n in range(1, 5)]
    windows = [(f"2026-03-0{n + 2} 20:00", f"2026-03-0{n + 2} 22:00") for n in range(4)]
    first = _round([_on(key, "IM1", key, window) for key, window in zip(keys, windows)])
    # The model drops gap-1 and gap-2 and renames gap-3 and gap-4 into their keys.
    final = _round([dict(first["items"][2], key=keys[0]), dict(first["items"][3], key=keys[1])])
    result = _settled(first, [keys[1]], final)

    assert sorted(item["phenomenon"] for item in result["submission"]["items"]) == keys
    assert all(len(item["key"]) <= 20 for item in result["submission"]["items"])
    assert not result["notes"]


def test_a_flagged_item_turned_into_a_copy_is_put_back() -> None:
    first = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y")])
    final = _round([_on("A1", "LT1", "Y"), _on("A2", "LT1", "Y")])
    result = _settled(first, ["A1"], final)

    assert "X" in [item["phenomenon"] for item in result["submission"]["items"]]
    assert any("内容完全相同" in issue["problem"] for issue in result["check"]["issues"])


def test_a_dropped_item_goes_back_to_the_coverage_of_its_own_meter() -> None:
    def coverage(found_on_im1: list[str], rule_keys: list[str]) -> list[dict[str, Any]]:
        entries = [entry for entry in _coverage(["AC1"], {}) if entry["method_id"] != "daily-clock-window"]
        return entries + [
            {"method_id": "daily-clock-window", "meter_ids": ["AC1"], "outcome": "none_found", "note": "查过"},
            {
                "method_id": "daily-clock-window",
                "meter_ids": ["IM1"],
                "outcome": "found" if found_on_im1 or rule_keys else "none_found",
                "note": "查过",
                "item_keys": found_on_im1,
                "rule_keys": rule_keys,
            },
        ]

    first = _round([_on("A1", "LT1", "Y"), _on("A2", "IM1", "X")], coverage(["A2"], ["R1"]))
    final = _round([_on("A1", "LT1", "Y")], coverage([], []))
    result = _settled(first, ["A2"], final)
    entries = {
        tuple(entry["meter_ids"]): entry
        for entry in result["submission"]["coverage"]
        if entry["method_id"] == "daily-clock-window"
    }

    assert entries[("AC1",)]["outcome"] == "none_found" and "修正轮" not in entries[("AC1",)]["note"]
    assert entries[("IM1",)]["outcome"] == "found"
    assert entries[("IM1",)]["item_keys"] == ["A2"] and entries[("IM1",)]["rule_keys"] == ["R1"]


def test_unasked_changes_to_links_and_coverage_are_flagged() -> None:
    a1, a2, a3 = _on("A1", "AC1", "X"), _on("A2", "LT1", "Y"), _on("A3", "IM1", "Z")
    a1["related_keys"], a2["related_keys"] = ["A2"], ["A1"]
    a1["relation_types"], a2["relation_types"] = {"A2": "independent"}, {"A1": "independent"}
    first = _round([a1, a2, a3])
    final = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y"), _on("A3", "IM1", "Z 改过")])
    segments = next(entry for entry in final["coverage"] if entry["method_id"] == "threshold-segments")
    segments["meter_ids"] = ["AC1"]
    result = _settled(first, ["A3"], final)
    problems = [(issue["key"], issue["problem"]) for issue in result["check"]["issues"]]

    assert ("A1", "修正轮改了这一项的相关项，但核对没有指出它的问题") in problems
    assert any(key == "" and "方法覆盖说明" in problem for key, problem in problems)


# --- numbers, marks and the report -----------------------------------------


def test_number_hints_read_ranges_and_wan() -> None:
    from manyselves.capabilities.energy_efficiency.domain.quantify import number_hints

    known = [12.0, 15234.0]
    assert number_hints(["夜间功率 12-95 kW"], known) == ["文字里的 95 在引用的证据和程序计算里没有找到对应的数"]
    assert number_hints(["全期约 1.52 万 kWh，03-02 起"], known) == []
    assert len(number_hints(["编号 " + "7" * 5000], known)) == 1  # read, not a crash


def test_a_replay_that_ran_and_hit_nothing_costs_zero(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    never = {**NIGHT, "params": {**NIGHT["params"], "min_avg_kw": 100000}}
    items = _quantified(tools, [_item("A1", "AC1", ["EV-0123456789"], never, ("2026-03-02 20:00", "2026-03-03 08:00"))], {"daily-clock-window": ["A1"]})
    scalars = items["A1"]["scalars"]

    assert scalars["replay_hits"] == 0 and scalars["replay_item_kwh"] == 0.0
    assert scalars["replay_cost"] == 0.0


def test_a_marks_file_that_is_not_xlsx_or_csv_is_refused_at_start(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    (tmp_path / "marks.xls").write_bytes(b"\xd0\xcf\x11\xe0 old excel")
    (tmp_path / "cut.xlsx").write_bytes(b"PK\x03\x04 cut short")
    project = start_input(tmp_path)

    with pytest.raises(ValueError, match=r"\.xlsx"):
        project("report-10", "energy-report", {"source_run_id": RUN, "marks_file": "marks.xls"})
    with pytest.raises(ValueError, match="cannot be read"):
        project("report-11", "energy-report", {"source_run_id": RUN, "marks_file": "cut.xlsx"})


def test_a_marks_file_that_marks_nothing_is_refused_or_reported(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    _candidates(tools)
    _marks_csv(tmp_path / "blank.csv", [])
    project = start_input(tmp_path)
    with pytest.raises(ValueError, match="marks no item"):
        project("report-12", "energy-report", {"source_run_id": RUN, "marks_file": "blank.csv"})

    (tmp_path / "rules.yaml").write_text(
        "rules:\n- when:\n  - {field: source, op: eq, value: model}\n  decision: keep\n", encoding="utf-8"
    )
    initial = project("report-13", "energy-report", {"source_run_id": RUN, "marks_file": "blank.csv", "rules_file": "rules.yaml"})
    selection = tools["select-energy-items"]({"run_id": "report-13", "request": initial["request"]})
    assert "没有一项填了选择" in selection["warnings"][0]


def test_the_report_keeps_model_text_inline_and_shows_links(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    one = _item("A1", "AC1", ["EV-0123456789", "EV-1\n## 假标题"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    one["reasoning"] = "1. 画像显示夜间不降\n2. 查数确认"
    two = _item("A2", "AC1", ["EV-0123456789"], NIGHT, ("2026-03-03 20:00", "2026-03-04 08:00"))
    two["related_keys"] = ["A1"]
    two["relation_types"] = {"A1": "same_event_evidence"}
    _quantified(tools, [one, two], {"daily-clock-window": ["A1", "A2"]})
    (tmp_path / "rules.yaml").write_text(
        "rules:\n- when:\n  - {field: source, op: eq, value: model}\n  decision: keep\n", encoding="utf-8"
    )
    initial = start_input(tmp_path)("report-14", "energy-report", {"source_run_id": RUN, "rules_file": "rules.yaml"})
    selection = tools["select-energy-items"]({"run_id": "report-14", "request": initial["request"]})
    report = tools["render-energy-report"]({"run_id": "report-14", "request": initial["request"], "selection": selection})
    markdown = (tmp_path / report["files"][0]).read_text(encoding="utf-8")
    page = (tmp_path / report["files"][1]).read_text(encoding="utf-8")

    assert not any(line.startswith("## 假标题") for line in markdown.splitlines())
    assert "- 思考过程：1. 画像显示夜间不降 2. 查数确认" in markdown
    assert "模型标的相关项" in markdown and "模型标的相关项" in page
    assert "全期出现平均覆盖" in markdown


def test_a_dropped_item_is_not_taken_for_a_meter_fix_under_its_key() -> None:
    first = _round([_on("A1", "AC1", "X"), _on("A2", "AC-1", "Y", ("2026-03-07 20:00", "2026-03-08 08:00"))])
    # The model drops A1, corrects A2's meter to AC1 and renumbers it A1.
    final = _round([_on("A1", "AC1", "Y 改过", ("2026-03-07 20:00", "2026-03-08 08:00"))])
    result = _settled(first, ["A1", "A2"], final)

    assert "X" in [item["phenomenon"] for item in result["submission"]["items"]]


def test_items_fixed_in_place_on_one_meter_stay_one_each() -> None:
    first = _round([_on("A1", "AC1", "X"), _on("A2", "AC1", "Y", ("2026-03-09 20:00", "2026-03-10 08:00"))])
    final = _round([_on("A1", "AC1", "X 改过"), _on("A2", "AC1", "Y 改过", ("2026-03-09 20:00", "2026-03-10 08:00"))])
    result = _settled(first, ["A1", "A2"], final)

    assert [item["phenomenon"] for item in result["submission"]["items"]] == ["X 改过", "Y 改过"]
    assert result["check"]["issues"] == []


def test_a_meter_fix_the_check_asked_for_is_the_same_item() -> None:
    first = _round([_on("A1", "AC-1", "X")])
    final = _round([_on("A1", "AC1", "X")])
    result = _settled(first, ["A1"], final)

    assert [item["meter_ids"] for item in result["submission"]["items"]] == [["AC1"]]
    assert result["check"]["issues"] == []


def test_an_item_left_out_of_the_coverage_goes_back() -> None:
    first = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y")])
    # The model fixes A1 and writes the night method as found nothing.
    final = _round([_on("A1", "AC1", "X 改过"), _on("A2", "LT1", "Y")], _coverage(["AC1", "LT1", "IM1"], {}))
    result = _settled(first, ["A1"], final)
    night = next(entry for entry in result["submission"]["coverage"] if entry["method_id"] == "daily-clock-window")

    assert night["outcome"] == "found" and sorted(night["item_keys"]) == ["A1", "A2"]
    assert any(issue["key"] == "A2" and "方法覆盖里拿掉" in issue["problem"] for issue in result["check"]["issues"])


def test_number_hints_skip_codes_and_dates_and_read_shared_units() -> None:
    from manyselves.capabilities.energy_efficiency.domain.quantify import number_hints

    known = [12.8, 20.0, 0.187, 0.25, 35.2]
    for text in [
        "夜间基底功率在 12.8-20 kW 之间",
        "占比 18.7-25%",
        "表 IM1-40 夜间基底 35.2 kW",
        "2026 年 3 月、2026-03、12 月 25 日夜间",
    ]:
        assert number_hints([text], known) == [], text
    assert number_hints(["表 im1-40 夜间"], known, ["im1-40"]) == []
    assert len(number_hints(["每年多耗约 5,000 kWh"], known)) == 1


def test_a_new_finding_under_a_dropped_item_s_key_does_not_replace_it() -> None:
    gaps_item = _item("A2", "IM1", ["EV-0123456789"], GAPS, ("2026-03-05 10:00", "2026-03-05 11:30"))
    gaps_item["phenomenon"] = "批次间隙空转"
    first = _round([_on("A1", "IM1", "夜间不停"), gaps_item])
    # The model drops A2 and numbers a different finding A2.
    final = _round([_on("A1", "IM1", "夜间不停"), _on("A2", "IM1", "周日白天不停机", ("2026-03-08 08:00", "2026-03-08 18:00"))])
    result = _settled(first, ["A2"], final)

    assert "批次间隙空转" in [item["phenomenon"] for item in result["submission"]["items"]]


def test_a_pairing_that_changed_the_item_s_window_is_named() -> None:
    first = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y"), _on("A3", "AC1", "Z", ("2026-03-04 20:00", "2026-03-05 08:00"))])
    final = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y"), _on("A3", "AC1", "另一个夜晚", ("2026-03-11 20:00", "2026-03-12 08:00"))])
    result = _settled(first, ["A3"], final)

    assert any("原来的 A3" in note and "“Z”" in note for note in result["notes"])


def test_two_meter_fixes_in_place_stay_two_items() -> None:
    first = _round([_on("A1", "AC-1", "X"), _on("A2", "LT-1", "Y")])
    final = _round([_on("A1", "AC1", "X"), _on("A2", "LT1", "Y")])
    result = _settled(first, ["A1", "A2"], final)

    assert [item["meter_ids"] for item in result["submission"]["items"]] == [["AC1"], ["LT1"]]
    assert result["check"]["issues"] == []


@pytest.mark.parametrize(
    ("text", "known", "codes", "flagged"),
    [
        ("夜间功率在 10-28 kW 之间", [10.0], (), ["28"]),  # a range with a unit is not a date
        ("可降低 5-30%", [0.05], (), ["30%"]),
        ("12/13 个夜晚超过阈值", [11.0, 13.0], (), ["12"]),
        ("03-02 起；3/2（周一）；3/2 22:00；12-25 日", [], (), []),
        ("3月16-31日夜间平均 35.2 kW", [35.2], (), []),  # spans of days, weeks and hours
        ("夜间（22-6点）平均 35.2 kW，第 13-15 周", [35.2], (), []),
        ("占全表电量的 18.7 %", [0.187], (), []),
        ("约 1.2-1.5 万元", [12000.0, 15000.0], (), []),
        ("约 1.2-1.6 万元", [12000.0, 15000.0], (), ["1.6万"]),
        ("Started 2026-03-02. Night base 35.2 kW, 见 2026-03.", [35.2], (), []),
        ("表 1 夜间平均 1.87 万 kWh", [15234.0], ("1", "2"), ["1.87万"]),  # a meter id never eats into a number
        ("注塑机 12 夜间平均 35.2 kW", [35.2], ("IM12", "注塑机 12"), []),
        ("2026-03-02 22:00 至 2026-03-03 06:00 共 48 条 15 分钟记录", [48.0], (), []),
        ("冷冻水出水温度设定在 7-12℃，无功 10-28 kvar", [7.0, 10.0], (), ["12", "28"]),
        ("每月夜间电量在 8000-1.2 万 kWh 之间", [12000.0], (), ["8000"]),
        ("表 12 全期 12,500 kWh，夜间占比 12%", [12500.0], ("表 12", "12#"), ["12%"]),
        ("16 号机、机台 16 与 16# 夜间仍有约 16 kW", [], ("机台 16", "16#"), ["16"]),
        ("每日第 10 百分位功率约 12 kW，10% 分位数也是 12 kW", [12.0], (), []),  # a percentile is a label
        ("下降约 10%", [12.0], (), ["10%"]),
    ],
)
def test_number_hints_tell_dates_times_and_names_from_figures(text, known, codes, flagged) -> None:
    from manyselves.capabilities.energy_efficiency.domain.quantify import number_hints

    hints = number_hints([text], known, codes)

    assert hints == [f"文字里的 {figure} 在引用的证据和程序计算里没有找到对应的数" for figure in flagged]


def test_a_bare_number_is_a_name_only_where_it_is_written_as_one() -> None:
    from manyselves.capabilities.energy_efficiency.domain.quantify import _written_names

    assert _written_names("16", "机台") == ["机台 16", "机台16", "16#"]
    assert _written_names("IM-01", "机台") == ["IM-01"]


def test_reasoning_may_quote_the_profile_but_the_phenomenon_must_quote_evidence(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    evidence = tools["try-method"](
        {"scope": f"{RUN}#P01", "meter_ids": ["AC1"], "method_id": "daily-clock-window", "params": NIGHT["params"]}
    )
    item = _item("A1", "AC1", [evidence["evidence_id"]], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    item["phenomenon"] = "AC1 全期电量 7392 kWh。"
    item["reasoning"] = "画像里 AC1 全期电量 7392 kWh、平均功率 22 kW，周末日电量 288 kWh；另有 9999 kWh。"

    hints = _quantified(tools, [item], {"daily-clock-window": ["A1"]})["A1"]["computed"]["number_hints"]

    # 7392 is in the profile the finder read, not in the evidence it cited.
    assert hints == [
        "文字里的 7392 在引用的证据和程序计算里没有找到对应的数",
        "文字里的 9999 在引用的证据和程序计算里没有找到对应的数",
    ]


# --- knowledge-base card fields and the verification checklist --------------


def test_a_card_needs_a_non_loss_explanation_a_category_and_typed_links() -> None:
    from pydantic import ValidationError

    from manyselves.capabilities.energy_efficiency.domain.items import LossItem

    base = _item("A1", "AC1", ["EV-0123456789"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    LossItem.model_validate(base)
    only_loss = dict(base, root_cause_candidates=[dict(base["root_cause_candidates"][0])] * 2)
    with pytest.raises(ValidationError, match="not a loss"):
        LossItem.model_validate(only_loss)
    with pytest.raises(ValidationError, match="candidate_categories"):
        LossItem.model_validate({key: value for key, value in base.items() if key != "candidate_categories"})
    with pytest.raises(ValidationError):
        LossItem.model_validate(dict(base, candidate_categories=[{"item_id": "LOSS-NIGHT", "why": "夜间"}]))
    with pytest.raises(ValidationError, match="relation_types"):
        LossItem.model_validate(dict(base, related_keys=["A2"]))


def test_a_restored_item_keeps_the_kind_of_its_links() -> None:
    a1, a2 = _on("A1", "AC1", "X"), _on("A2", "LT1", "Y")
    a2["related_keys"], a2["relation_types"] = ["A1"], {"A1": "same_event_evidence"}
    first = _round([a1, a2])
    # The model drops A2 while fixing A1.
    final = _round([_on("A1", "AC1", "X 改过")])
    result = _settled(first, ["A1"], final)
    restored = next(item for item in result["submission"]["items"] if item["phenomenon"] == "Y")

    assert restored["relation_types"] == {"A1": "same_event_evidence"}


def test_the_report_carries_a_verification_checklist_with_blank_judgments(tmp_path: Path) -> None:
    tools, _briefs = _prepared(tmp_path)
    tools["__workspace__"] = tmp_path
    one = _item("A1", "AC1", ["EV-0123456789"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    one["possible_necessary_services"] = ["夜间冷却水循环"]
    two = _item("A2", "LT1", ["EV-0123456789"], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    two["related_keys"], two["relation_types"] = ["A1"], {"A1": "same_event_evidence"}
    two["candidate_categories"] = [{"item_id": "LOSS-PROCESS", "why": "试一下"}]
    items = _quantified(tools, [one, two], {"daily-clock-window": ["A1", "A2"]})

    assert items["A1"]["candidate_categories"][0]["role"] == "ENERGY"
    assert any("经人声明口径的合格品产量" in note for note in items["A2"]["computed"]["category_notes"])
    assert any("没有同一块表" in note for note in items["A2"]["computed"]["relation_notes"])
    (tmp_path / "rules.yaml").write_text(
        "rules:\n- when:\n  - {field: source, op: eq, value: model}\n  decision: keep\n", encoding="utf-8"
    )
    initial = start_input(tmp_path)("report-kb", "energy-report", {"source_run_id": RUN, "rules_file": "rules.yaml"})
    selection = tools["select-energy-items"]({"run_id": "report-kb", "request": initial["request"]})
    report = tools["render-energy-report"]({"run_id": "report-kb", "request": initial["request"], "selection": selection})
    markdown = (tmp_path / report["files"][0]).read_text(encoding="utf-8")
    checklist = markdown.split("## 人工核实清单")[1].split("## 查了什么")[0]

    assert "## 保留的候选损耗项" in markdown and "涉及电量" in markdown
    assert "必要服务（人确认）：未知" in checklist and "夜间冷却水循环" in checklist
    # The knowledge base's competing explanations for a night window, with a blank status column.
    assert "停产窗口公辅持续运行" in checklist and "| 已排除/未排除（人填） |" in checklist
    assert "| 假设待验证 |  |  |" in checklist
    assert "夜间有必要的工艺维持" in checklist


def test_rules_can_read_the_candidate_categories(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "- when:\n  - {field: candidate_categories, op: contains, value: MEASUREMENT_ISSUE}\n  decision: needs_data\n"
        "- when:\n  - {field: candidate_categories, op: not_contains, value: LOSS-IDLE}\n  decision: drop\n",
        encoding="utf-8",
    )
    register = {
        "run_id": RUN,
        "items": [
            {"id": "L001", "meter_ids": ["AC1"], "scalars": {}, "candidate_categories": [{"item_id": "LOSS-IDLE"}]},
            {"id": "L002", "meter_ids": ["AC1"], "scalars": {}, "candidate_categories": [{"item_id": "MEASUREMENT_ISSUE"}]},
            {"id": "R001", "meter_ids": ["AC1"], "scalars": {}, "candidate_categories": []},
        ],
    }
    decisions = {entry["item_id"]: entry["decision"] for entry in select(register, None, load_rules(path))["decisions"]}

    assert decisions == {"L001": "unselected", "L002": "needs_data", "R001": "drop"}


def test_survey_answers_and_inferences_reach_the_finder_and_can_be_cited(tmp_path: Path) -> None:
    import yaml

    site = write_site(tmp_path / "data/site")
    (site / "survey.csv").write_text(
        "调研表\n序号,问题摘要,状态,现场核实结果\n生产·02,污水站,已答复,持续运行\n资料·07,电价方案,部分已了解（待确认）,两个户号\n产线·13,报警,无答复,\n",
        encoding="utf-8",
    )
    manifest = yaml.safe_load((site / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["site_facts"] = {
        "file": "survey.csv",
        "header_row": 2,
        "id_column": "序号",
        "topic_column": "问题摘要",
        "answer_column": "现场核实结果",
        "status_column": "状态",
        "confirmed_states": ["已答复", "已确认"],
    }
    manifest["assumptions"] = [{"id": "A1", "topic": "AC1 计量范围", "inference": "只计空压机", "basis": "台账名称"}]
    (site / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    start_input(tmp_path)(RUN, "energy-candidates", {"input_dir": "data/site"})
    tools = _covering(tool_implementations(tmp_path), tmp_path)
    parsed = tools["parse-energy-inputs"]({"run_id": RUN})
    tools["profile-energy-data"]({"run_id": RUN})
    tools["run-energy-prechecks"]({"run_id": RUN})
    brief = tools["partition-energy-meters"]({"run_id": RUN, "max_meters": 6})["partitions"][0]

    assert parsed["site_facts"] == {"facts": 2, "confirmed": 1}
    assert [(fact["id"], fact["confirmed"]) for fact in brief["site_facts"]] == [("生产·02", True), ("资料·07", False)]
    assert brief["assumptions"][0]["id"] == "A1"

    scope = f"{RUN}#{brief['partition_id']}"
    meter = brief["meters"][0]["meter_id"]
    day = tools["energy-series"]({"scope": scope, "meter_id": meter, "start": "2026-03-02 00:00", "end": "2026-03-03 00:00"})
    item = _item("A1", meter, [day["evidence_id"]], NIGHT, ("2026-03-02 20:00", "2026-03-03 08:00"))
    item["root_cause_candidates"] += [
        {"cause": "污水站持续运行", "kind": "necessary_service", "basis": "site_fact", "basis_detail": "生产·02", "how_to_check": "问"},
        {"cause": "别的", "kind": "normal_operation", "basis": "site_fact", "basis_detail": "问卷", "how_to_check": "问"},
    ]
    submission = {
        "partition_id": brief["partition_id"],
        "items": [item],
        "coverage": _coverage([meter], {"daily-clock-window": ["A1"]}),
    }
    issues = tools["check-partition-items"]({"scope": scope, "submission": submission})["issues"]
    problems = [issue["problem"] for issue in issues if issue["key"] == "A1"]
    assert any("“别的”依据是 site_fact" in problem for problem in problems)
    assert not any("污水站持续运行" in problem for problem in problems)


def test_inferences_are_listed_in_the_report_as_waiting_for_the_site() -> None:
    from manyselves.capabilities.energy_efficiency.domain.verification import (
        assumptions_html,
        assumptions_markdown,
        checklist_markdown,
    )

    rows = [{"id": "A1", "topic": "E06 表范围", "inference": "没计到整条线", "basis": "批次电量只有报表的六成"}]
    lines = checklist_markdown([], assumptions_markdown(rows))
    text = "\n".join(lines)
    assert "### 数据口径待现场确认" in text
    assert "| A1 | E06 表范围 | 没计到整条线 | 批次电量只有报表的六成 |  | 待现场确认 |  |" in text
    assert "待现场确认" in assumptions_html(rows)
    assert assumptions_markdown([]) == []
