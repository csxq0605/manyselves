from pathlib import Path

from openpyxl import Workbook

from autoreport.core.reporting.mappers.s4_6 import map_s4_6


def test_s4_6_maps_summary_taxonomy_without_using_filename_as_location(
    tmp_path: Path,
) -> None:
    path = tmp_path / "S4-6评估总表.xlsx"
    workbook = Workbook()
    summary = workbook.active
    summary.title = "评估信息汇总表"
    summary.append([None] * 20)
    summary.append([None] * 20)
    summary.append([None] * 20)
    row = [None] * 20
    row[1] = "2.4.2.2"
    row[4] = "等电位连接与接地问题"
    row[5] = "总电箱进线缺少地线"
    row[6] = "补敷接地线"
    summary.append(row)
    architecture = [None] * 20
    architecture[1] = "2.1.1"
    architecture[5] = "负荷分配待优化"
    architecture[6] = "复核运行方式"
    summary.append(architecture)
    conclusion = workbook.create_sheet("结论建议汇总表")
    conclusion.append([None] * 8)
    conclusion.append(
        [
            None,
            None,
            "接地扁铁断开",
            "存在触电风险",
            "修复接地",
            None,
            None,
            "配电设备＞等电位连接与接地问题",
        ]
    )
    conclusion.append(
        [
            None,
            None,
            "操作规程未更新",
            "误操作风险",
            "修订规程",
            None,
            None,
            "运维管理＞SOP/EOP",
        ]
    )
    workbook.save(path)

    result = map_s4_6(path, file_id="file-s46")

    summary_item = next(item for item in result.evidence_items if item.source.cell == "F4:G4")
    assert summary_item.submodule_id == "2.4.2.2"
    assert summary_item.subject == "评估总表 2.4.2.2"
    assert path.name not in summary_item.subject
    assert summary_item.needs_confirmation is True
    conclusion_item = next(item for item in result.evidence_items if item.source.cell == "C2:E2")
    assert conclusion_item.submodule_id == "2.4.2.2"
    architecture_item = next(item for item in result.evidence_items if item.source.cell == "F5:G5")
    assert (architecture_item.module_id, architecture_item.submodule_id) == ("2.1", "2.1.1")
    operations_item = next(item for item in result.evidence_items if item.source.cell == "C3:E3")
    assert (operations_item.module_id, operations_item.submodule_id) == ("2.5", "2.5.1")
