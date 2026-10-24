"""Reported load ratios are not permission to assume a universal 0.4 kV basis."""

import pytest
from openpyxl import Workbook

from tests.capabilities.distribution_reporting.test_intake_fidelity import _normalize
from tests.capabilities.distribution_reporting.test_summary_formula_fidelity import _cache, _sheet1


def _workbook(tmp_path, title, value, number_format):
    path = tmp_path / "S4-4.xlsx"
    sheet = Workbook().active
    sheet.title = title
    sheet["A3"], sheet["B3"], sheet["C3"], sheet["D3"] = "合成配电室", "柜A", 100, 1000
    sheet["E3"] = value
    sheet["E3"].number_format = number_format
    sheet.parent.save(path)
    return path


def _load_items(context, title):
    return [
        e for e in context.evidence_items if e.source.sheet == title and e.source.cell == "C3:E3"
    ]


@pytest.mark.parametrize("title", ["低配评估详情", "总配评估详情表"])
def test_source_formula_result_is_not_replaced_by_hardcoded_voltage(tmp_path, title):
    formula = "=C3*0.69*SQRT(3)/D3"
    path = _workbook(tmp_path, title, formula, "0.00%")
    _cache(path, _sheet1({"E3": ("n", "0.119511505722")}))
    context = _normalize(tmp_path, path)
    items = _load_items(context, title)
    assert len(items) == 2
    for item in items:
        assert item.value == pytest.approx(11.9511505722) and item.unit == "%"
        assert "原表负荷率=11.95%" in item.fact
        assert formula in item.fact
        # The program's caution is kept apart from what the record says.
        assert "测量侧别" not in item.fact and "测量侧别" in (item.program_note or "")
        assert item.needs_confirmation
        assert "计算负荷率=6.93%" not in item.fact


@pytest.mark.parametrize("title", ["低配评估详情", "总配评估详情表"])
def test_missing_source_ratio_keeps_current_and_capacity_without_inventing_voltage(tmp_path, title):
    path = _workbook(tmp_path, title, None, "0.00%")
    context = _normalize(tmp_path, path)
    items = _load_items(context, title)
    assert len(items) == 2
    assert all(item.value == 100 and item.unit == "A" and item.needs_confirmation for item in items)
    assert all(
        "变压器容量=1000kVA" in item.fact and "负荷率结果不可用" in item.fact for item in items
    )
    assert any(
        g.get("code") == "missing_load_rate_result" and g["cell"] == "E3"
        for g in context.mapping_gaps
    )


@pytest.mark.parametrize("number_format", ["General", '0.00"%"', r"0.00\%", "0.00;0.00%", "0.00%%"])
def test_unmarked_or_literal_percent_format_does_not_silently_scale_number(tmp_path, number_format):
    path = _workbook(tmp_path, "总配评估详情表", 0.5, number_format)
    context = _normalize(tmp_path, path)
    items = _load_items(context, "总配评估详情表")
    assert all(item.value == 100 and item.unit == "A" for item in items)
    assert all(
        "原表负荷率数值=0.5" in item.fact
        and "比例口径待核实" not in item.fact
        and item.program_note == "比例口径待核实"
        for item in items
    )
    assert any(g.get("code") == "ambiguous_load_rate_scale" for g in context.mapping_gaps)


@pytest.mark.parametrize(
    ("value", "number_format", "expected"), [(0, "0.00%", 0), ("12.5%", "General", 12.5)]
)
def test_explicit_percentage_and_zero_preserve_source_value(
    tmp_path, value, number_format, expected
):
    path = _workbook(tmp_path, "低配评估详情", value, number_format)
    context = _normalize(tmp_path, path)
    items = _load_items(context, "低配评估详情")
    assert all(item.value == expected and item.unit == "%" for item in items)
    assert not any(
        g.get("code") in {"missing_load_rate_result", "ambiguous_load_rate_scale"}
        for g in context.mapping_gaps
    )
