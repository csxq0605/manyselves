from pathlib import Path

from openpyxl import Workbook

from manyselves.core.reporting.mappers.s2_1 import map_s2_1


def test_s2_1_maps_availability_and_effectiveness_with_exact_cells(tmp_path: Path) -> None:
    path = tmp_path / "S2-1收资表.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "收资表"
    sheet.append(
        [
            "类型",
            "文件名称",
            "形式",
            "文件说明",
            "评估内容",
            "评估后输出内容",
            "具备情况(OK/NG)",
            "有效性(OK/NG)",
            "备注",
        ]
    )
    sheet.append(
        [
            "基础图纸与技术参数",
            "主要开关（断路器）技术参数",
            "Excel",
            None,
            None,
            None,
            "OK",
            "NG",
            "已过期",
        ]
    )
    workbook.save(path)

    result = map_s2_1(path, file_id="file-s21")

    item = result.evidence_items[0]
    assert item.module_id == "2.4"
    assert item.submodule_id == "2.4.1.1"
    assert "具备情况=OK" in item.fact
    assert "有效性=NG" in item.fact
    assert item.source.sheet == "收资表"
    assert item.source.cell == "G2:H2"
    assert item.needs_confirmation is True


def test_s2_1_routes_management_and_protection_documents_to_exact_modules(
    tmp_path: Path,
) -> None:
    path = tmp_path / "S2-1收资表.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "收资表"
    sheet.append(["类型", "文件名称", None, None, None, None, "具备", "有效", "备注"])
    sheet.append(["运维资料", "SOP/EOP操作规程及应急预案", None, None, None, None, "OK", "OK"])
    sheet.append(["技术资料", "继电保护定值计算书", None, None, None, None, "OK", "OK"])
    sheet.append(["运维资料", "关键配电设备维护计划与记录", None, None, None, None, "OK", "OK"])
    workbook.save(path)

    result = map_s2_1(path, file_id="file-s21")

    routes = {
        item.subject.split("/", 1)[1]: (item.module_id, item.submodule_id)
        for item in result.evidence_items
    }
    assert routes["SOP/EOP操作规程及应急预案"] == ("2.5", "2.5.1")
    assert routes["继电保护定值计算书"] == ("2.3", "2.3.1")
    assert routes["关键配电设备维护计划与记录"] == ("2.5", "2.5.3.2")
