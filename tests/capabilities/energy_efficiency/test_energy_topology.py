"""The asset list and plant tree as candidates, the survey of a folder, the trial read of a drafted manifest."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from manyselves.capabilities.energy_efficiency.domain.inputs import Manifest, load_manifest
from manyselves.capabilities.energy_efficiency.domain.manifest_draft import check_manifest
from manyselves.capabilities.energy_efficiency.domain.survey import survey_folder
from manyselves.capabilities.energy_efficiency.domain.topology import build_topology, topology_group
from tests.capabilities.energy_efficiency.synthetic import write_site


def _with_lists(folder: Path) -> Manifest:
    write_site(folder)
    (folder / "points.csv").write_text(
        "编号,名称,用能类别,关联设备\nAC-1,空压站,公辅,1# 空压机\n,注塑机,生产,IM-01\n,车间照明回路,照明,\n,冷却塔,公辅,\n",
        encoding="utf-8",
    )
    tree = {
        "data": [
            {
                "id": "1",
                "name": "工厂",
                "children": [
                    {"id": "AC1", "name": "空压站", "children": [{"id": "9", "name": "注塑机 1", "children": []}]},
                    {"id": "LT1", "name": "二楼车间照明回路", "children": []},
                ],
            }
        ]
    }
    (folder / "tree.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    data = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    data["assets"] = {"file": "points.csv", "code_column": "编号", "name_column": "名称", "category_column": "用能类别"}
    data["plant_tree"] = {"format": "json", "file": "tree.json", "nodes_path": ["data"]}
    for meter in data["meters"]:
        meter.pop("group", None)
    (folder / "manifest.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return load_manifest(folder)


def test_meters_get_candidate_rows_and_nodes(tmp_path: Path) -> None:
    manifest = _with_lists(tmp_path)
    topology = build_topology(tmp_path, manifest)
    assert topology is not None
    rows = {entry["meter_id"]: entry for entry in topology["meters"]}
    # A code one character away (AC-1 / AC1 differ by the dash), a name inside the meter's name.
    assert rows["AC1"]["asset_matches"][0]["how"] == "near" and rows["AC1"]["category"] == "公辅"
    # 注塑机 against 注塑机 1: one character away.  车间照明回路 is inside the tree node's name.
    assert rows["IM1"]["asset_matches"][0]["how"] == "near" and rows["IM1"]["category"] == "生产"
    assert rows["LT1"]["asset_matches"][0]["how"] == "contained"
    assert topology_group(topology, "LT1") == "照明"
    # IM1 sits under AC1 in the tree by its name: listed as a relation, the sum warned against.
    assert rows["IM1"]["tree_path"] == "工厂 / 空压站 / 注塑机 1"
    assert topology["relations"] == [{"upper": "AC1", "lower": "IM1", "how": "工厂树"}]
    assert any("不要相加" in note for note in topology["notes"])
    assert any("冷却塔" in note for note in topology["notes"])


def test_survey_describes_files_without_meaning(tmp_path: Path) -> None:
    write_site(tmp_path)
    survey = survey_folder(tmp_path)
    tables = {table["file"]: table for table in survey["tables"]}
    columns = {column["name"]: column for column in tables["moulding.csv"]["columns_under_guess"]}
    # The cumulative readings never go down except at the one reset.
    assert columns["读数kWh"]["share_never_down"] > 0.99
    assert columns["时间"]["median_spacing_minutes"] == 15.0
    assert any(entry["file"] == "manifest.yaml" for entry in survey["skipped"])


def test_trial_read_sends_back_what_does_not_behave(tmp_path: Path) -> None:
    write_site(tmp_path)
    data = yaml.safe_load((tmp_path / "manifest.yaml").read_text(encoding="utf-8"))
    assert check_manifest(tmp_path, Manifest.model_validate(data))["ok"] is True
    for meter in data["meters"]:
        if meter["id"] == "IM1":
            meter.update({"semantics": "interval_energy", "timestamp_marks": "end"})
        if meter["id"] == "LT1":
            meter["interval_minutes"] = 15
    data["meters"][0]["production_machines"] = ["IM-99"]
    check = check_manifest(tmp_path, Manifest.model_validate(data))
    assert check["ok"] is False and not check["blocking"]
    text = "\n".join(check["issues"])
    assert "IM1" in text and "像累计读数" in text
    assert "LT1" in text and "间隔中位数是 5 分钟" in text
    assert "IM-99" in text
    data["meters"][2]["value_column"] = "没有这列"
    assert check_manifest(tmp_path, Manifest.model_validate(data))["blocking"]
