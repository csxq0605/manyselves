"""The program finds and sizes the loss items; the model's specs are computed and checked; the screening follows the preferences."""

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


def _look(key: str, specs: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "key": key,
        "triggers": [],
        "mechanisms": [
            {"mechanism": "停产夜间空压机加载", "objects": ["AC1"], "outcome": "spec", "specs": [spec["id"] for spec in specs], "note": "停产时管网没人用气"}
        ],
        "specs": specs,
        "site_questions": [],
        **extra,
    }


def test_the_lenses_find_and_size_items_on_every_meter(tmp_path: Path) -> None:
    _tools, looks = _ready(tmp_path)
    ledger = _ledger(tmp_path)
    by_id = ledger.by_id
    # The compressor is not off while the plant is stopped: 12 kW all night and 40 kW until 20:00.
    stop = by_id["L1-AC1-stop"]
    assert _levels(stop) == {"零": 3777.0, "底功率": 840.0} and stop.default == "底功率"
    assert ledger.screening["L1-AC1-stop"]["result"] == "有效"
    # Two batches a day, 3.5 h and 4.5 h at 30 kW for 100 件 each: the long batch is over the quartile.
    assert _levels(by_id["L2a-IM1"]) == {"组内最低": 300.0, "P25": 300.0, "P50": 150.0}
    # The lunch hour at 8 kW over the 1 kW floor: 70 kWh, under the 100 kWh floor of the screening (P36).
    assert _levels(by_id["L1-IM1-between"])["底功率"] == 70.0
    assert ledger.screening["L1-IM1-between"]["rule"] == "S2"
    # Every meter is examined by some lens; nothing is left unexplained on a site this regular.
    assert all(row["not_examined_kwh"] == 0 for row in ledger.coverage)
    assert ledger.residual_total() == 0
    # One look brief per system, with the system's meters only.
    briefs = {brief["key"]: brief for brief in looks["briefs"]}
    assert set(briefs) == {"look-动力站", "look-注塑车间"}
    assert [meter["meter"] for meter in briefs["look-动力站"]["meters"]] == ["AC1"]
    assert looks["summary"]["valid"] == 2


def test_the_screening_follows_the_preferences(tmp_path: Path) -> None:
    _ready(tmp_path, {"screen_min_kwh": 50, "screen_min_share": 0.2})
    screening = _ledger(tmp_path).screening
    # 70 kWh is now over the kWh floor, but 2.6 % of the moulding meter is under a 20 % share.
    assert screening["L1-IM1-between"]["result"] == "无效" and screening["L1-IM1-between"]["rule"] == "S3"
    # 840 kWh is 11 % of the compressor's energy: under a 20 % share it is not a loss worth listing.
    assert screening["L1-AC1-stop"]["rule"] == "S3"


def test_a_reference_spec_is_computed_by_the_program_and_checked(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path)
    check = tools["check-look"]({"run_id": RUN, "key": "look-动力站", "submission": _look("look-动力站", [NIGHT])})
    assert check == {"ok": True, "issues": [], "hints": []}
    settled = tools["settle-look"]({"key": "look-动力站", "first_submission": _look("look-动力站", [NIGHT]), "first_check": check})
    applied = tools["apply-looks"]({"run_id": RUN, "results": {"look-动力站": settled}, "errors": {}})
    assert [entry["key"] for entry in applied["rounds"]] == ["R1", "R2", "R3"]
    ledger = _ledger(tmp_path)
    night = ledger.by_id["M-night-AC1"]
    # 12 kW for the stopped nights, and 9 kW over the alternative's 3 kW.
    assert _levels(night) == {"规格参照": 1329.0, "保压": 996.75}
    # A reference nobody confirmed waits for a person.
    assert ledger.screening["M-night-AC1"]["rule"] == "S8"

    wrong = json.loads(json.dumps(NIGHT))
    wrong.update({"id": "wrong", "objects": ["IM1", "ZZ9"], "when": {"dates": ["2027-01-01"]}})
    wrong["basis"] = [{"source": "问卷", "ref": "Q9", "claims_confirmed": True}]
    submission = _look("look-动力站", [wrong], triggers=[{"trigger": "T-NONE", "disposition": "not_a_problem", "note": "看过"}])
    submission["mechanisms"].append({"mechanism": "管网泄漏", "objects": ["AC1"], "outcome": "covered", "covered_by": ["L9-AC1"], "note": "已覆盖"})
    issues = tools["check-look"]({"run_id": RUN, "key": "look-动力站", "submission": submission})["issues"]
    text = "\n".join(f"{issue['key']}: {issue['problem']}" for issue in issues)
    assert "ZZ9" in text and "IM1" in text  # not a meter, and not this system's
    assert "Q9" in text  # no such survey row
    assert "T-NONE" in text  # not a trigger of this brief
    assert "L9-AC1" in text  # covered by a candidate that does not exist


def test_confirmed_rules_become_a_lens_and_explain_what_they_cover(tmp_path: Path) -> None:
    reference = {"objects": ["AC1"], "when": {"plant": "STOP", "clock": "22:00-06:00"}, "reference": {"type": "fixed_kw", "value": 3}}
    residual = {"objects": ["AC1"], "when": {"plant": "STOP", "weekdays": [1, 2, 3, 4, 5], "clock": "17:00-20:00"}, "type": "necessary_service"}
    _ready(tmp_path, {"reference_rules": [reference], "residual_rules": [residual]})
    ledger = _ledger(tmp_path)
    # A person's reference is a lens of its own and screened like any lens.
    assert _levels(ledger.by_id["R-P39-1-AC1"]) == {"规格参照": 996.75}
    assert ledger.screening["R-P39-1-AC1"]["result"] == "有效"
    # The evening wash the person explained is all of L1-AC1-stop's excess over the floor.
    assert ledger.screening["L1-AC1-stop"]["rule"] == "S1"


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
    tools["apply-looks"]({"run_id": RUN, "results": {}, "errors": {}})
    briefs = {brief["key"]: brief for brief in tools["judge-briefs"]({"run_id": RUN})["briefs"]}
    brief = briefs["judge-动力站"]
    assert [entry["id"] for entry in brief["judge"]] == ["L1-AC1-stop"]
    first = {"key": "judge-动力站", "judgments": [_judge("L1-AC1-stop")]}
    probes = tools["run-probes"]({"run_id": RUN, "key": "judge-动力站", "submission": first})["results"]
    # 15.4 kW stopped against 40 kW producing: under the P31 ratio, the data does not back "it does not drop".
    assert probes[0]["cause"] == "C1" and probes[0]["result"] == "contradicts"
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
    fixed = _judge("L1-AC1-stop")
    fixed["causes"][0]["status"] = "dropped"
    assert tools["check-judgment"]({"run_id": RUN, "key": "judge-动力站", "submission": {"key": "judge-动力站", "judgments": [fixed]}})["ok"]
    missing = tools["check-judgment"]({"run_id": RUN, "key": "judge-动力站", "submission": {"key": "judge-动力站", "judgments": []}})
    assert any("没有判断" in issue["problem"] for issue in missing["issues"])


def test_a_person_chooses_items_and_levels_and_the_report_writes_the_chosen_amount(tmp_path: Path) -> None:
    tools, _looks = _ready(tmp_path)
    tools["apply-looks"]({"run_id": RUN, "results": {}, "errors": {}})
    tools["register-candidates"]({"run_id": RUN})
    folder = tmp_path / "Work/runs" / RUN / "energy"
    register = read_json(folder / "register.json")
    items = {item["id"]: item for item in register["items"]}
    assert items["L1-AC1-stop"]["screen"]["result"] == "有效"
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
    assert decisions["L5-IM1"]["decision"] == "drop"
    markdown, page = build_report(register, selection, [])
    kept = markdown.split("## 保留的损耗项")[1].split("## 需补资料或待定的项")[0]
    assert "### L1-AC1-stop" in kept and "3,777 kWh（参照：零，人选" in kept
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
