"""The inputs of EveryMe's definition (A01–A05), the foundation and its handles, and the plant stopped by power."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from manyselves.capabilities.energy_efficiency.adapters.runtime import (
    start_input,
    tool_implementations,
)
from manyselves.capabilities.energy_efficiency.domain.foundation import load_foundation, load_model
from manyselves.capabilities.energy_efficiency.domain.handles import (
    Foundation,
    HandleError,
    handle_part,
    numbers_match,
)
from manyselves.capabilities.energy_efficiency.domain.inputs import InputFormatError
from manyselves.capabilities.energy_efficiency.domain.preparation import load_parameters
from manyselves.capabilities.energy_efficiency.domain.store import SiteData, read_json
from tests.capabilities.energy_efficiency.synthetic import write_site

RUN = "candidates-1"


def _site(workspace: Path, **files: Any) -> Path:
    folder = write_site(workspace / "data/site", prechecks=False)
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["production"].update({"quantity_basis": "output", "quantity_unit": "件"})
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for name, content in files.items():
        (folder / f"{name}.yaml").write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return folder


def _built(workspace: Path, **files: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    _site(workspace, **files)
    start_input(workspace)(RUN, "energy-candidates", {"input_dir": "data/site"})
    tools = tool_implementations(workspace)
    tools["parse-energy-inputs"]({"run_id": RUN})
    tools["profile-energy-data"]({"run_id": RUN})
    summary = tools["build-foundation"]({"run_id": RUN})
    return tools, {"summary": summary}


def test_handles_name_values_and_numbers_match_their_rounding() -> None:
    assert handle_part("IM-01") == "IM-01"
    assert handle_part("壳体") == "壳体" and handle_part("壳体 / 黑").startswith("壳体_黑-")
    assert handle_part("a b") != handle_part("a_b")
    assert numbers_match(12.4, 12.4371) and numbers_match(12, 12.4) and not numbers_match(13, 12.4)
    assert numbers_match(0.45, 0.4535) and numbers_match(0.5, 0.4535) and not numbers_match(0.6, 0.4535)
    assert numbers_match("OPERATING", "OPERATING")
    foundation = Foundation({})
    with pytest.raises(HandleError):
        foundation.split("not a handle")


def test_the_foundation_describes_states_and_events_with_handles(tmp_path: Path) -> None:
    _tools, built = _built(tmp_path)
    tables = built["summary"]["tables"]
    assert tables["DAY"] == 14 and tables["TASK"] == 20 and tables["TR"] == 19
    site = SiteData(tmp_path, RUN)
    foundation = load_foundation(site)
    # Batches run at 30 kW: the band's reference is the 95th percentile of the batch time.
    assert foundation.value("BAND.IM1.reference_kw")["value"] == 30.0
    # 8 kW in the lunch hour between the two batches of each weekday is LOW: ten hours.
    assert foundation.value("XS.IM1.BETWEEN_TASKS.LOW.hours")["value"] == 10.0
    days = foundation.value("DAYS.ALL.operating_days")
    # P01 (how long a day must run to count) is an analysis preference, not a site assumption.
    assert days["value"] == 10 and "P01" not in days["assumption_ids"]
    # One model, 20 batches of 100 件: one comparable group, a candidate nobody confirmed.
    group = foundation.rows("GRP")[0]
    assert group["batches"] == 20 and group["status"] == "CANDIDATE"
    # Rows are named by the row of the production file (the header is row 1).
    value = foundation.value("TASK.IM-01.R2.kwh_per_unit")
    assert value["unit"] == "kWh/件" and value["value"] == pytest.approx(105 / 100)
    model = load_model(site)
    assert model["counts"]["ProductionTask"] == 20
    assert {gap["kind"] for gap in model["gaps"]} >= {"meter_scope", "service", "tariff_binding"}
    bases = {item["id"]: item["basis"] for item in model["parameters"]}
    assert {ident for ident, basis in bases.items() if basis == "ASSUMED"} >= {"P06", "P07"}
    assert {ident for ident, basis in bases.items() if basis == "CONVENTION"} >= {"P01", "P02", "P05"}
    assert read_json(site.path("sources.json"))["register"][0]["source_id"] == "S01"


def test_parameters_and_confirmations_change_what_rests_on_assumptions(tmp_path: Path) -> None:
    _tools, built = _built(
        tmp_path,
        parameters={"version": "1", "values": {"day_run_hours": {"value": 2.0, "source": "生产科 11 月给的口径"}}},
        confirmations={
            "confirmations": [
                {"kind": "calendar", "subject": "2026-03-07", "day_status": "OPERATING", "confirmed_by": "生产科", "basis": "排班表"},
                {"kind": "service", "subject": "AC1", "serves": ["IM-01"], "confirmed_by": "设备科", "basis": "管路图"},
                {"kind": "peer_group", "subject": "壳体", "confirmed_by": "工艺科", "basis": "同一模具"},
            ]
        },
    )
    site = SiteData(tmp_path, RUN)
    foundation = load_foundation(site)
    saturday = foundation.value("DAY.2026-03-07.day_status")
    assert saturday["value"] == "OPERATING" and saturday["status"] == "CONFIRMED"
    assert "P01" not in foundation.value("DAY.2026-03-08.day_status")["assumption_ids"]
    service = foundation.row("DFS.AC1")
    assert service["served_basis"] == "USER_CONFIRMED" and service["served"] == ["IM-01"]
    assert foundation.rows("GRP")[0]["status"] is None  # confirmed: the column's status stands
    model = load_model(site)
    p01 = next(item for item in model["parameters"] if item["id"] == "P01")
    assert p01["basis"] == "REGISTERED" and p01["value"] == 2.0
    assert not [gap for gap in model["gaps"] if gap["kind"] == "service" and "AC1" in gap["objects"]]
    assert len(model["confirmations"]["applied"]) == 3


def test_a_parameter_the_program_does_not_know_is_refused(tmp_path: Path) -> None:
    (tmp_path / "parameters.yaml").write_text("values:\n  night_hours: {value: 3, source: 我}\n", encoding="utf-8")
    with pytest.raises(InputFormatError):
        load_parameters(tmp_path)


def test_the_plant_is_stopped_by_power_not_by_the_calendar(tmp_path: Path) -> None:
    _tools, built = _built(tmp_path)
    assert built["summary"]["tables"]["STP"] == 12
    foundation = load_foundation(SiteData(tmp_path, RUN))
    stretches = foundation.rows("STP")
    # Every night after the last batch (the moulding line drops to 1 kW at 17:00) and both weekends.
    assert sum(row["hours"] for row in stretches) == pytest.approx(244.75)
    assert stretches[0]["start"] == "2026-03-02 00:15:00" and stretches[0]["end"] == "2026-03-02 08:00:00"
    assert foundation.value("DAY.2026-03-07.stop_hours_by_power")["value"] == 24.0
    assert foundation.value("DAY.2026-03-02.stop_hours_by_power")["value"] == 14.75
    # The compressor keeps 12 kW while stopped and 40 kW until 20:00: about four tenths of its running power.
    compressor = foundation.row("PWS.AC1")
    assert compressor["run_kw"] == 40.0 and compressor["ratio"] == pytest.approx(0.386)
    assert compressor["stop_share"] == pytest.approx(0.511, abs=1e-3)
    assert foundation.row("PWS.IM1")["ratio"] == pytest.approx(0.033)
    assert "P20" in compressor["assumption_ids"]
