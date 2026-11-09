"""The facts about what the data measures, and the drafted inferences that cite them."""

from __future__ import annotations

from pathlib import Path

import yaml

from manyselves.capabilities.energy_efficiency.adapters.runtime import (
    start_input,
    tool_implementations,
)
from manyselves.capabilities.energy_efficiency.domain.prepare import brief_assumptions
from manyselves.capabilities.energy_efficiency.domain.store import SiteData, read_json
from tests.capabilities.energy_efficiency.synthetic import write_site

RUN = "caliber-run"


def _site(workspace: Path, assumptions: list[dict[str, str]] | None = None) -> dict:
    folder = write_site(workspace / "data/site")
    if assumptions:
        manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["assumptions"] = assumptions
        (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    start_input(workspace)(RUN, "energy-candidates", {"input_dir": "data/site"})
    tools = tool_implementations(workspace)
    tools["parse-energy-inputs"]({"run_id": RUN})
    tools["profile-energy-data"]({"run_id": RUN})
    return tools


def test_facts_measure_how_each_meter_behaves(tmp_path: Path) -> None:
    tools = _site(tmp_path)
    brief = tools["caliber-facts"]({"run_id": RUN})["briefs"][0]
    facts = {fact["id"]: fact for fact in read_json(SiteData(tmp_path, RUN).path("caliber.json"))["facts"]}

    assert brief["key"] == "CALIBER" and brief["fact_ids"] == list(facts)
    assert "[AC1-COVER]" in brief["facts_text"]
    # The moulding machine's meter rises while its own machine runs: 30 kW against 1-8 kW.
    machines = facts["IM1-MACHINES"]["values"]["machines"]
    assert machines[0]["machine"] == "IM-01" and machines[0]["mapped"] and machines[0]["lift"] > 3
    assert facts["IM1-BATCH"]["values"]["batches"] == 20
    # The compressor runs at 40 kW whenever a batch runs; a stopped plant mixes 40 kW evenings and 12 kW nights.
    assert facts["AC1-STATE"]["values"]["plant_running"]["avg_kw"] == 40.0
    assert 12.0 < facts["AC1-STATE"]["values"]["plant_stopped"]["avg_kw"] < 40.0
    # Lighting is 0 kW at night: zero readings are measured, not judged.
    assert facts["LT1-LOW"]["values"]["share_zero"] > 0.3
    assert "PLANT-MACHINES" in facts and "PLANT-PROD" in facts


def test_drafted_inferences_are_numbered_and_their_citations_checked(tmp_path: Path) -> None:
    manual = [{"id": "D1", "topic": "IM1 范围", "inference": "只量注塑机", "basis": "准备资料时看过"}]
    tools = _site(tmp_path, manual)
    tools["caliber-facts"]({"run_id": RUN})
    summary = tools["settle-assumptions"](
        {
            "run_id": RUN,
            "draft": {
                "assumptions": [
                    {
                        "topic": "AC1 的计量范围",
                        "inference": "AC1 可能还带着别的常开负载",
                        "reasoning": "停产时仍有 12 kW；也可能是空压机自己保压。",
                        "basis_fact_ids": ["AC1-STATE", "AC1-NOTHING"],
                        "meter_ids": ["AC1", "XX"],
                        "affects": "停产时段的电量",
                        "how_to_confirm": "停产时看空压机是否加载",
                    },
                    {
                        "topic": "照明",
                        "inference": "照明可能按时钟开关",
                        "reasoning": "每天同一时刻起落。",
                        "basis_fact_ids": ["NONE"],
                        "how_to_confirm": "看照明控制柜",
                    },
                ]
            },
        }
    )
    assert summary == {"run_id": RUN, "drafted": 2, "with_notes": 2}
    listed = SiteData(tmp_path, RUN).assumptions()
    assert [entry["id"] for entry in listed] == ["D1", "D2", "D3"]
    first = listed[1]
    assert first["origin"] == "drafted" and first["basis_fact_ids"] == ["AC1-STATE"] and first["meter_ids"] == ["AC1"]
    assert "[AC1-STATE]" in first["basis"] and len(first["check_notes"]) == 2
    assert "没有一条能对上的依据" in listed[2]["check_notes"]
    # The finders get the reasoning and the fact ids, not the facts' text.
    rows = {row["id"]: row for row in brief_assumptions(SiteData(tmp_path, RUN))}
    assert rows["D2"]["basis_fact_ids"] == ["AC1-STATE"] and "basis" not in rows["D2"]
    assert rows["D1"]["basis"] == "准备资料时看过"


def test_left_out_spans_are_listed_and_the_focus_reaches_the_finders(tmp_path: Path) -> None:
    write_site(tmp_path / "data/site")
    start_input(tmp_path)(RUN, "energy-candidates", {"input_dir": "data/site", "focus": "空压系统"})
    tools = tool_implementations(tmp_path)
    tools["parse-energy-inputs"]({"run_id": RUN})
    tools["profile-energy-data"]({"run_id": RUN})
    tools["run-energy-prechecks"]({"run_id": RUN})
    # The moulding meter is reset once: the intervals around the drop are listed with their reason.
    spans = SiteData(tmp_path, RUN).quality("IM1").excluded_spans
    assert spans and all(span["reason"] for span in spans)
    assert any(span["start"] <= "2026-03-09 03:00" <= span["end"] for span in spans)
    briefs = tools["partition-energy-meters"]({"run_id": RUN, "max_meters": 6})["partitions"]
    assert {brief.get("focus") for brief in briefs} == {"空压系统"}
