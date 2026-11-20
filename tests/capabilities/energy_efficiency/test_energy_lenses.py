"""The program computes the differences; the analyst's items adopt them or bring specs; the program computes and checks the items, and the screening of their rows follows the preferences."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from manyselves.capabilities.energy_efficiency.adapters.runtime import (
    start_input,
    tool_implementations,
)
from manyselves.capabilities.energy_efficiency.domain.ledger import compose
from manyselves.capabilities.energy_efficiency.domain.report import build_report
from manyselves.capabilities.energy_efficiency.domain.selection import read_marks, select
from manyselves.capabilities.energy_efficiency.domain.store import SiteData, read_json
from tests.capabilities.energy_efficiency.synthetic import write_site

RUN = "lens-1"


def _ready(workspace: Path, preferences: dict[str, Any] | None = None):
    """The made-up site with batch quantities, parsed, with its foundation and the lenses run."""

    folder = write_site(workspace / "data/site", prechecks=False)
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["production"].update({"quantity_basis": "output", "quantity_unit": "件"})
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if preferences:
        values = {key: {"value": value, "source": "测试里人填的"} for key, value in preferences.items()}
        (folder / "preferences.yaml").write_text(yaml.safe_dump({"values": values}, allow_unicode=True), encoding="utf-8")
    start_input(workspace)(RUN, "energy-candidates", {"input_dir": "data/site"})
    tools = tool_implementations(workspace)
    for name in ("parse-energy-inputs", "profile-energy-data", "build-foundation"):
        tools[name]({"run_id": RUN})
    return tools, tools["run-lenses"]({"run_id": RUN})


def _ledger(workspace: Path):
    return compose(SiteData(workspace, RUN))


def _levels(candidate: Any) -> dict[str, float]:
    return {level["label"]: level["excess_kwh"] for level in candidate.levels}


NIGHT = {
    "kind": "reference",
    "id": "night",
    "objects": ["AC1"],
    "when": {"plant": "STOP", "clock": "22:00-06:00"},
    "reference": {"type": "zero"},
    "alternatives": [{"type": "fixed_kw", "value": 3, "label": "保压"}],
    "basis": [{"source": "领域常识"}],
    "why": "全厂停的夜里不用压缩空气",
}
NOT_A_LOSS = {"id": "C2", "cause": "夜里有设备保压要用气", "kind": "necessary"}


def _item(local: str, differences: list[str] | None = None, spec: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": local,
        "title": "停产后空压机还开着",
        "differences": differences or [],
        **({"spec": spec} if spec else {}),
        "finding": "无需求用电视角：全厂停时空压机高于底功率",
        "reasoning": "停产时管网没人用气，功率却只降到在产时的一部分",
        "causes": [{"id": "C1", "cause": "停产后没人关空压机", "kind": "loss"}, NOT_A_LOSS],
        **extra,
    }


def _look(key: str, items: list[dict[str, Any]], specs: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "key": key,
        "items": items,
        "set_aside": [],
        "triggers": [],
        "mechanisms": [
            {"mechanism": "停产夜间空压机加载", "objects": ["AC1"], "outcome": "item", "items": [item["id"] for item in items][:1], "note": "停产时管网没人用气"}
        ]
        if items
        else [{"mechanism": "照明随作息", "outcome": "no_evidence", "note": "看过，数据里没看到"}],
        "specs": specs or [],
        "site_questions": [],
        **extra,
    }


def _apply(tools: dict[str, Any], looks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    results = {}
    for key, submission in looks.items():
        check = tools["check-look"]({"run_id": RUN, "key": key, "submission": submission})
        assert check["ok"], check["issues"]
        results[key] = tools["settle-look"]({"key": key, "first_submission": submission, "first_check": check})
    return tools["apply-looks"]({"run_id": RUN, "results": results, "errors": {}})


PLAIN = {
    "look-动力站": _look("look-动力站", [_item("I1", ["L1-AC1-stop"])]),
    "look-注塑车间": _look("look-注塑车间", [_item("I1", ["L2a-IM1"], title="长批次单耗高"), _item("I2", ["L1-IM1-between"], title="午休保温")]),
}


def test_the_lenses_compute_differences_and_the_large_ones_are_rows_before_anyone_proposes(tmp_path: Path) -> None:
    _tools, looks = _ready(tmp_path)
    ledger = _ledger(tmp_path)
    by_id = ledger.difference_by_id
    # The compressor is not off while the plant is stopped: 12 kW all night and 40 kW until 20:00.
    stop = by_id["L1-AC1-stop"]
    assert _levels(stop) == {"零": 3777.0, "底功率": 840.0} and stop.default == "底功率"
    # Two batches a day, 3.5 h and 4.5 h at 30 kW for 100 件 each: the long batch is over the quartile.
    assert _levels(by_id["L2a-IM1"]) == {"组内最低": 300.0, "P25": 300.0, "P50": 150.0}
    # The lunch hour at 8 kW over the 1 kW floor: 70 kWh, under the 100 kWh floor (P36), so the analyst need not account for it.
    assert _levels(by_id["L1-IM1-between"])["底功率"] == 70.0
    assert ledger.must_answer == ["L1-AC1-stop", "L2a-IM1"]
    # No item yet: each large difference is a row of a program item of its own, pending at most (S12).
    assert {row.id: row.item for row in ledger.candidates} == {"L1-AC1-stop": "D-L1-AC1-stop", "L2a-IM1": "D-L2a-IM1"}
    assert ledger.unclaimed == ["L1-AC1-stop", "L2a-IM1"] and ledger.items["D-L1-AC1-stop"]["source"] == "program"
    assert {key: value["rule"] for key, value in ledger.screening.items()} == {"L1-AC1-stop": "S12", "L2a-IM1": "S12"}
    assert all(row["not_examined_kwh"] == 0 for row in ledger.coverage)
    # Only the small difference stays in the residual.
    assert round(ledger.differences_total(), 1) == 1210.0 and round(ledger.residual_total(), 1) == 70.0
    assert {row["meter"]: row["unclaimed_kwh"] for row in ledger.coverage} == {"AC1": 840.0, "IM1": 300.0, "LT1": 0.0}
    # One brief per system, with the system's meters and all its differences.
    briefs = {brief["key"]: brief for brief in looks["briefs"]}
    assert set(briefs) == {"look-动力站", "look-注塑车间"}
    assert [meter["meter"] for meter in briefs["look-动力站"]["meters"]] == ["AC1"]
    assert [entry["id"] for entry in briefs["look-注塑车间"]["differences"]] == ["L2a-IM1", "L1-IM1-between"]
    assert briefs["look-动力站"]["must_answer"] == ["L1-AC1-stop"] and briefs["look-动力站"]["item_prefix"] == "S1"


def test_items_adopt_differences_and_their_rows_are_screened(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path)
    applied = _apply(tools, PLAIN)
    assert [entry["key"] for entry in applied["rounds"]] == ["R1", "R2", "R3"]
    ledger = _ledger(tmp_path)
    # Each adopted difference is a row of its item, with the difference's whole-period numbers.
    assert {row.id: row.item for row in ledger.candidates} == {"L1-AC1-stop": "S1-I1", "L2a-IM1": "S2-I1", "L1-IM1-between": "S2-I2"}
    assert _levels(ledger.by_id["L1-AC1-stop"]) == {"零": 3777.0, "底功率": 840.0}
    assert ledger.screening["L1-AC1-stop"]["result"] == "有效"
    assert ledger.screening["L1-IM1-between"]["rule"] == "S2"
    # Every row is checked for stability: both halves of the period hold the excess.
    steady = ledger.by_id["L1-AC1-stop"].details["stability"]
    assert steady["first_half_kwh"] > 0 and steady["second_half_kwh"] > 0
    # What the items adopted leaves the residual.
    assert ledger.found_total() == pytest.approx(ledger.differences_total()) and ledger.residual_total() == pytest.approx(0, abs=1e-6)


def test_the_screening_follows_the_preferences(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path, {"screen_min_kwh": 50, "screen_min_share": 0.2})
    _apply(tools, {key: value for key, value in PLAIN.items()})
    screening = _ledger(tmp_path).screening
    # 70 kWh is now over the kWh floor, but 2.6 % of the moulding meter is under a 20 % share.
    assert screening["L1-IM1-between"]["result"] == "无效" and screening["L1-IM1-between"]["rule"] == "S3"
    # 840 kWh is 11 % of the compressor's energy: under a 20 % share it is not a loss worth listing.
    assert screening["L1-AC1-stop"]["rule"] == "S3"


def test_a_reference_spec_belongs_to_an_item_and_is_computed_and_checked(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path)
    night = _look("look-动力站", [_item("I1", spec="night")], [NIGHT], set_aside=[{"difference": "L1-AC1-stop", "reason": "cannot_tell", "note": "和夜间项重叠，先看夜间"}])
    _apply(tools, {"look-动力站": night})
    ledger = _ledger(tmp_path)
    row = ledger.by_id["M-night-AC1"]
    # 12 kW for the stopped nights, and 9 kW over the alternative's 3 kW, over the whole period.
    assert _levels(row) == {"规格参照": 1329.0, "保压": 996.75} and row.item == "S1-I1"
    assert row.details["stability"]["shift"]["change_share"] is not None
    # A reference nobody confirmed waits for a person.
    assert ledger.screening["M-night-AC1"]["rule"] == "S8"
    # The difference set aside is still a row, of a program item of its own: the reason is an opinion on it,
    # it does not screen the row out and it does not count as explained.
    assert ledger.set_aside[0]["difference"] == "L1-AC1-stop" and not ledger.residual_model.any()
    assert ledger.by_id["L1-AC1-stop"].item == "D-L1-AC1-stop" and ledger.screening["L1-AC1-stop"]["rule"] == "S12"
    assert ledger.items["D-L1-AC1-stop"]["set_aside"]["note"] == "和夜间项重叠，先看夜间"

    wrong = json.loads(json.dumps(NIGHT))
    wrong.update({"id": "wrong", "objects": ["IM1", "ZZ9"], "when": {"dates": ["2027-01-01"]}})
    wrong["basis"] = [{"source": "问卷", "ref": "Q9", "claims_confirmed": True}]
    orphan = {**NIGHT, "id": "orphan"}
    items = [
        _item("I1", spec="wrong"),
        _item("I2", ["L2a-IM1", "L9-AC1"]),
        _item("I3", ["L1-AC1-stop"], causes=[{"id": "C1", "cause": "没人关", "kind": "loss"}]),
        _item("I4", ["L1-AC1-stop"]),
        _item("I5"),
    ]
    submission = _look("look-动力站", items, [wrong, orphan], triggers=[{"trigger": "T-NONE", "disposition": "not_a_problem", "note": "看过"}])
    submission["mechanisms"].append({"mechanism": "管网泄漏", "objects": ["AC1"], "outcome": "item", "items": ["I9"], "note": "提了"})
    issues = tools["check-look"]({"run_id": RUN, "key": "look-动力站", "submission": submission})["issues"]
    text = "\n".join(f"{issue['key']}: {issue['problem']}" for issue in issues)
    assert "ZZ9" in text and "不是本系统的表" in text  # not a meter, and not this system's
    assert "Q9" in text  # no such survey row
    assert "T-NONE" in text  # not a trigger of this brief
    assert "L9-AC1" in text  # a difference that does not exist
    assert "item I3: causes 里至少要有一条不是损耗" in text
    assert "也被本次的项 I3 采用" in text  # one difference, two items
    assert "item I5: 每项要采用至少一条程序差额" in text
    assert "spec orphan: 参照规格要挂在一个项上" in text
    assert "mechanism 2" in text  # an item that is not in the submission
    # A large difference neither adopted nor set aside is sent back.
    silent = _look("look-动力站", [], [])
    issues = tools["check-look"]({"run_id": RUN, "key": "look-动力站", "submission": silent})["issues"]
    assert [issue["key"] for issue in issues] == ["difference L1-AC1-stop"]


def test_stability_checks_screen_rows_that_do_not_hold(tmp_path: Path) -> None:
    # The half hour below is 84 kWh: the floor comes down so that it is screened on its stability.
    tools, _looks = _ready(tmp_path, {"screen_min_kwh": 50})
    first_week = {**NIGHT, "id": "early", "when": {"plant": "STOP", "clock": "22:00-06:00", "dates": ["2026-03-02..2026-03-08"]}}
    # Half an hour right after the compressor drops from 40 kW to 12 kW at 20:00.
    edge = {**NIGHT, "id": "edge", "when": {"plant": "STOP", "clock": "20:00-20:30"}}
    items = [_item("I1", spec="night"), _item("I2", spec="early"), _item("I3", spec="edge")]
    _apply(tools, {"look-动力站": _look("look-动力站", items, [NIGHT, first_week, edge], set_aside=[{"difference": "L1-AC1-stop", "reason": "cannot_tell", "note": "看不出"}])})
    ledger = _ledger(tmp_path)
    screening = ledger.screening
    # Only in the first week: the second half has none of it.
    assert ledger.by_id["M-early-AC1"].details["stability"]["half_ratio"] == 0
    assert screening["M-early-AC1"]["rule"] == "S10"
    # One interval earlier takes in a quarter hour at 40 kW: the excess more than doubles (P45 is 0.5).
    assert ledger.by_id["M-edge-AC1"].details["stability"]["shift"]["change_share"] > 0.5
    assert screening["M-edge-AC1"]["rule"] == "S11"
    # The night is flat at 12 kW: moving the hours changes nothing, and the row is only pending for its unconfirmed reference.
    assert ledger.by_id["M-night-AC1"].details["stability"]["shift"]["change_share"] == 0
    assert screening["M-night-AC1"]["rule"] == "S8"


def test_confirmed_rules_are_items_and_explain_what_they_cover(tmp_path: Path) -> None:
    reference = {"objects": ["AC1"], "when": {"plant": "STOP", "clock": "22:00-06:00"}, "reference": {"type": "fixed_kw", "value": 3}}
    residual = {"objects": ["AC1"], "when": {"plant": "STOP", "weekdays": [1, 2, 3, 4, 5], "clock": "17:00-20:00"}, "type": "necessary_service"}
    tools, _looks = _ready(tmp_path, {"reference_rules": [reference], "residual_rules": [residual]})
    ledger = _ledger(tmp_path)
    # A person's reference is an item of its own, screened like any item.
    assert _levels(ledger.by_id["R-P39-1-AC1"]) == {"规格参照": 996.75}
    assert ledger.by_id["R-P39-1-AC1"].item == "P39-1" and ledger.items["P39-1"]["source"] == "rule"
    assert ledger.screening["R-P39-1-AC1"]["result"] == "有效"
    # The evening wash the person explained is all of L1-AC1-stop's excess over the floor: the item adopting it is invalid.
    _apply(tools, {"look-动力站": PLAIN["look-动力站"]})
    assert _ledger(tmp_path).screening["L1-AC1-stop"]["rule"] == "S1"


def _judge(candidate: str, **extra: Any) -> dict[str, Any]:
    return {
        "candidate": candidate,
        "opinion": "likely_loss",
        "reference_choice": "底功率",
        "reference_reason": "同一台空压机在本厂停产时的底功率",
        "causes": [
            {
                "id": "C1",
                "cause": "停产后没人关空压机",
                "kind": "loss",
                "test": {"type": "base_load_when_stopped"},
                "status": "kept",
            }
        ],
        "measures": [{"type": "management", "action": "停产交班时关空压机", "owner_role": "equipment"}],
        **extra,
    }


def test_judgments_are_checked_against_the_tests_the_program_ran(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path)
    tested = _item("I1", ["L1-AC1-stop"], causes=[{"id": "C1", "cause": "停产后没人关空压机", "kind": "loss", "test": {"type": "base_load_when_stopped"}}, NOT_A_LOSS])
    _apply(tools, {"look-动力站": _look("look-动力站", [tested])})
    briefs = {brief["key"]: brief for brief in tools["judge-briefs"]({"run_id": RUN})["briefs"]}
    brief = briefs["judge-动力站"]
    assert [entry["id"] for entry in brief["judge"]] == ["L1-AC1-stop"]
    assert [item["id"] for item in brief["items"]] == ["S1-I1"] and brief["items"][0]["finding"]
    # The test written with the proposal was run before the judgment: 15.4 kW stopped against 40 kW producing
    # is under the P31 ratio, so the data does not back "it does not drop".
    tests = brief["judge"][0]["tests"]
    assert tests[0]["cause"] == "C1" and tests[0]["result"] == "contradicts"
    retrofit = {"type": "retrofit", "action": "换变频空压机", "owner_role": "equipment", "depends_on": ["C1"]}
    wrong = {
        "key": "judge-动力站",
        "judgments": [_judge("L1-AC1-stop", reference_choice="中位", measures=[retrofit], related=["L9"])],
    }
    issues = tools["check-judgment"]({"run_id": RUN, "key": "judge-动力站", "submission": wrong})["issues"]
    problems = "\n".join(issue["problem"] for issue in issues)
    assert "reference_choice" in problems
    assert "不能写 kept" in problems
    assert "retrofit" in problems
    assert "L9" in problems
    fixed = _judge("L1-AC1-stop", related=["S1-I1"])
    fixed["causes"][0]["status"] = "dropped"
    assert tools["check-judgment"]({"run_id": RUN, "key": "judge-动力站", "submission": {"key": "judge-动力站", "judgments": [fixed]}})["ok"]
    missing = tools["check-judgment"]({"run_id": RUN, "key": "judge-动力站", "submission": {"key": "judge-动力站", "judgments": []}})
    assert any("没有判断" in issue["problem"] for issue in missing["issues"])


def test_a_person_chooses_rows_and_levels_and_the_report_writes_the_chosen_amount(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path)
    _apply(tools, PLAIN)
    tools["register-candidates"]({"run_id": RUN})
    folder = tmp_path / "Work/runs" / RUN / "energy"
    register = read_json(folder / "register.json")
    items = {item["id"]: item for item in register["items"]}
    assert items["L1-AC1-stop"]["screen"]["result"] == "有效" and items["L1-AC1-stop"]["item"] == "S1-I1"
    assert {entry["id"] for entry in register["loss_items"]} == {"S1-I1", "S2-I1", "S2-I2"}
    # The differences no item adopted are listed on their own page, not in the ledger.
    differences = {entry["id"]: entry for entry in register["differences"]}
    assert differences["L1-AC1-stop"]["adopted_by"] == "S1-I1" and "L5-IM1" not in items
    assert (folder / "register.xlsx").is_file() and (folder / "分析偏好.xlsx").is_file()
    marks = tmp_path / "marks.csv"
    with marks.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["编号", "核对码", "取舍", "理由", "选定参照"])
        writer.writerow(["L1-AC1-stop", items["L1-AC1-stop"]["check_code"], "保留", "夜里也该关", "零"])
        writer.writerow(["L1-IM1-between", items["L1-IM1-between"]["check_code"], "需补资料", "问午休是否保温", ""])
    codes = {item["id"]: item["check_code"] for item in register["items"]}
    selection = select(register, read_marks(marks, codes), None)
    decisions = {entry["item_id"]: entry for entry in selection["decisions"]}
    assert decisions["L1-AC1-stop"] == {"item_id": "L1-AC1-stop", "decision": "keep", "by": "person", "note": "夜里也该关", "level": "零"}
    # Without a mark the screening decides.
    assert decisions["L2a-IM1"]["decision"] == "keep" and decisions["L2a-IM1"]["by"] == "screen S0"
    markdown, page = build_report(register, selection, [])
    kept = markdown.split("## 保留的损耗项")[1].split("## 需补资料或待定的项")[0]
    assert "### L1-AC1-stop" in kept and "3,777 kWh（参照：零，人选" in kept
    assert "寻找方式（模型，未核实）：无需求用电视角" in kept
    assert "### L2a-IM1" in kept
    waiting = markdown.split("## 需补资料或待定的项")[1].split("## 人或规则不保留的项")[0]
    assert "L1-IM1-between" in waiting and "问午休是否保温" in waiting
    assert "<h1>" in page


@pytest.mark.parametrize("bad", [{"objects": ["AC1"]}, [{"objects": ["AC1"], "when": {"clock": "25:00-06:00"}, "reference": {"type": "zero"}}]])
def test_a_rule_that_does_not_read_is_listed_not_computed(tmp_path: Path, bad: Any) -> None:
    rules = bad if isinstance(bad, list) else [bad]
    _ready(tmp_path, {"reference_rules": rules})
    ledger = _ledger(tmp_path)
    assert not [candidate for candidate in ledger.candidates if candidate.lens == "R"]
    assert any("P39" in problem for problem in ledger.problems)
