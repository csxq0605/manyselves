from pathlib import Path

import pytest
from openpyxl import Workbook

from autoreport.core.reporting.mappers.s4_4 import map_s4_4


def _write_s4_4(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "低配评估详情"
    sheet.append([None] * 26)
    sheet.append(
        [
            "配电房",
            "低压柜",
            "运行电流(A)",
            "变压器容量(kVA)",
            "负荷率",
            "安全连锁",
            None,
            "电缆状态",
            None,
            "剩余电流(A)",
            None,
            "剩余电流占比",
            "IP与接地",
            None,
            "色标标识",
            None,
            "谐波",
            None,
            "电涌保护装置",
            None,
            "变压器噪声震动",
            "变压器温度",
            None,
            "其他",
            None,
            None,
        ]
    )
    sheet.append(["芜湖"] + [None] * 25)
    sheet.append(["宿舍配电房", "3G总柜", 33, 1250] + [None] * 22)
    sheet["F4"] = "NG"
    sheet.merge_cells("F4:G4")
    sheet.append(
        [
            "车间配电房",
            "1A2",
            2300,
            2500,
            "=C5*0.4*1.732/D5",
            "NG",
            '=DISPIMG("ID_LOCK",1)',
            "NG",
            '=DISPIMG("ID_CABLE",1)',
            46.8,
            '=DISPIMG("ID_RESIDUAL",1)',
            "=J5/C5",
            "OK",
            None,
            "OK",
            None,
            ">6.2%",
            None,
            "OK",
            None,
            "OK",
            "OK",
            None,
            None,
            None,
            None,
        ]
    )
    sheet.append(
        [
            "车间配电房",
            "2A2",
            3500,
            2500,
            "=C6*0.4*1.732/D6",
            "NG",
            None,
            "OK",
            None,
            59.6,
            '=DISPIMG("ID_RESIDUAL_2",1)',
            "=J6/C6",
            "OK",
            None,
            "OK",
            None,
            ">6.8%",
            None,
            "OK",
            None,
            "OK",
            "NG（三相温度差异大）",
            '=DISPIMG("ID_TEMP",1)',
            None,
            None,
            None,
        ]
    )
    workbook.save(path)


def test_s4_4_maps_low_voltage_issue_to_same_row_photo(tmp_path: Path) -> None:
    path = tmp_path / "S4-4诊断工作用表.xlsx"
    _write_s4_4(path)

    result = map_s4_4(path, file_id="file-s44")

    item = next(
        item
        for item in result.evidence_items
        if item.subject == "车间配电房/1A2" and "电缆状态=NG" in item.fact
    )
    assert item.submodule_id == "2.4.2.5"
    assert item.photo_refs == ["ID_CABLE"]
    assert item.source.sheet == "低配评估详情"
    assert item.source.cell == "H5:I5"


def test_s4_4_calculates_96_99_percent_as_below_100_not_existing_overload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "S4-4诊断工作用表.xlsx"
    _write_s4_4(path)

    result = map_s4_4(path, file_id="file-s44")
    item = next(
        item
        for item in result.evidence_items
        if item.subject == "车间配电房/2A2" and item.source.cell == "C6:E6"
    )

    assert item.value == pytest.approx(96.992)
    assert item.unit == "%"
    assert "过载" not in item.fact
    assert "96.99%" in item.fact


def test_s4_4_maps_residual_current_with_its_own_photo(tmp_path: Path) -> None:
    path = tmp_path / "S4-4诊断工作用表.xlsx"
    _write_s4_4(path)

    result = map_s4_4(path, file_id="file-s44")
    item = next(
        item
        for item in result.evidence_items
        if item.subject == "车间配电房/1A2" and item.submodule_id == "2.4.3.1"
    )

    assert item.value == 46.8
    assert item.unit == "A"
    assert item.photo_refs == ["ID_RESIDUAL"]
    assert item.source.cell == "J5:K5"
