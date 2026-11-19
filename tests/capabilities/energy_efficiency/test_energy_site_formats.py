"""Site exports read as they come: long meter tables, report-style headers, monthly tariff workbooks."""

from __future__ import annotations

from datetime import datetime, timedelta

import openpyxl
import pytest

from manyselves.capabilities.energy_efficiency.domain.inputs import (
    InputFormatError,
    Manifest,
    MeterSpec,
    ProductionSpec,
    TariffFileSpec,
    TariffSchedule,
    TariffSpec,
    load_batches,
    load_tariff_file,
    manifest_files,
    read_meter_table,
    tariff_schedule,
)


def _write_rows(path, rows, sheet="Sheet1"):
    book = openpyxl.Workbook()
    book.active.title = sheet
    for row in rows:
        book.active.append(row)
    book.save(path)


def _meter(**extra) -> MeterSpec:
    values = {
        "id": "E01",
        "name": "1#线",
        "file": "long.csv",
        "time_column": "时间",
        "value_column": "读数",
        "semantics": "cumulative_energy",
        "unit": "kWh",
        "interval_minutes": 15,
    }
    values.update(extra)
    return MeterSpec(**values)


# --- long meter tables -------------------------------------------------------


def test_a_long_table_gives_each_meter_its_own_rows(tmp_path) -> None:
    (tmp_path / "long.csv").write_text(
        "时间,设备,读数\n2026-07-01 00:00,E01,1\n2026-07-01 00:00,E02,50\n2026-07-01 00:15,E01,2\n2026-07-01 00:15,E02,51\n",
        encoding="utf-8",
    )
    frame = read_meter_table(tmp_path, _meter(filter_column="设备", filter_value="E01"))
    assert frame["读数"].tolist() == ["1", "2"]
    assert list(frame.columns) == ["时间", "读数"]
    with pytest.raises(InputFormatError, match="E09"):
        read_meter_table(tmp_path, _meter(filter_column="设备", filter_value="E09"))


def test_a_filter_needs_both_its_column_and_its_value() -> None:
    with pytest.raises(ValueError, match="go together"):
        _meter(filter_column="设备")


def test_spreadsheet_numbers_match_the_filter_as_text(tmp_path) -> None:
    _write_rows(
        tmp_path / "long.xlsx",
        [["时间", "设备ID", "读数"], [datetime(2026, 7, 1), 1307630, 1.0], [datetime(2026, 7, 1), 1307626, 9.0]],
    )
    frame = read_meter_table(tmp_path, _meter(file="long.xlsx", filter_column="设备ID", filter_value="1307630"))
    assert frame["读数"].tolist() == [1.0]


# --- report-style production sheets ------------------------------------------


def _report(tmp_path):
    _write_rows(
        tmp_path / "report.xlsx",
        [
            [None, None, "2026年生产月报", None, None, None, None, None, None],
            ["序号", "开工日期", "开工时间", "完工日期", "完工时间", "机台", "型号规格", "成品", "呆滞品(kg)"],
            [None, None, None, None, None, None, None, None, "断条"],
            [1, "2026-06-30", "17:20", "2026-07-01", "15:40", "E01", "A280G7", 12500, 77],
            [2, "2026-07-01", "15:40", "2026-07-01", "19:50", "E01", "A280G7", 964, 21],
            [3, "2026-07-02", "08:00", None, None, "E02", "T1300", 10, 0],
            ["小计", None, None, None, None, None, None, 13474, 98],
        ],
    )
    return ProductionSpec(
        file="report.xlsx",
        header_row=2,
        header_rows=2,
        machine_column="机台",
        start_column="开工日期",
        start_time_column="开工时间",
        end_column="完工日期",
        end_time_column="完工时间",
        product_column="型号规格",
        product_group_columns=["型号规格"],
        quantity_column="成品",
        quantity_basis="output",
        quantity_unit="kg",
    )


def test_a_report_header_below_a_title_and_split_date_and_time_are_read(tmp_path) -> None:
    batches, skipped = load_batches(tmp_path, _report(tmp_path))

    assert skipped == 1
    assert [batch.row for batch in batches] == [4, 5, 6]
    first = batches[0]
    assert (first.start, first.end) == (datetime(2026, 6, 30, 17, 20), datetime(2026, 7, 1, 15, 40))
    assert first.quantity == 12500 and first.product == "A280G7" and first.groups == {"型号规格": "A280G7"}
    assert batches[2].problem == "time_missing"


def test_a_second_header_row_names_the_columns_under_a_heading(tmp_path) -> None:
    spec = _report(tmp_path).model_copy(update={"quantity_column": "呆滞品(kg)/断条"})
    batches, _skipped = load_batches(tmp_path, spec)
    assert [batch.quantity for batch in batches] == [77, 21, 0]


def test_a_time_of_day_that_is_not_one_leaves_the_batch_without_a_time(tmp_path) -> None:
    (tmp_path / "p.csv").write_text("机台,日,时,止日,止时\nL1,2026-07-01,25:10,2026-07-01,09:00\n", encoding="utf-8")
    spec = ProductionSpec(
        file="p.csv",
        machine_column="机台",
        start_column="日",
        start_time_column="时",
        end_column="止日",
        end_time_column="止时",
    )
    batches, _skipped = load_batches(tmp_path, spec)
    assert batches[0].problem == "time_missing" and batches[0].end == datetime(2026, 7, 1, 9)


# --- monthly tariff workbooks --------------------------------------------------


def _tariff_book(path, sep_peak_price=None):
    book = openpyxl.Workbook()
    prices = book.active
    prices.title = "分时电价"
    prices.append(["电价方案编号", "月份", "尖峰", "高峰", "平段", "低谷", "深谷"])
    prices.append(["1G3/2G3", 6, None, None, None, None, None])
    prices.append(["1G3/2G3", 7, 1.07, 0.98, 0.57, 0.29, None])
    prices.append(["1G3/2G3", 9, sep_peak_price, 1.03, 0.63, 0.34, None])
    periods = book.create_sheet("时段划分")
    periods.append(["电价方案编号", "月份", "时段类型", "小时区间", "时段类型", "小时区间", "时段类型", "小时区间", "时段类型", "小时区间"])
    periods.append(["1G3/2G3", "7月", "尖峰", "18:00 - 22:00", "高峰", "16:00 - 18:00、22:00 - 23:00", "平段", "7:00 - 11:00\n14:00 - 16:00\n23:00 - 24:00", "低谷", "0:00 - 7:00\n11:00 - 14:00"])
    periods.append(["1G3/2G3", "9月", "尖峰", None, "高峰", "16:00 - 23:00", "平段", "7:00 - 11:00\n14:00 - 16:00\n23:00 - 24:00", "低谷", "0:00 - 7:00\n11:00 - 14:00"])
    book.save(path)


def test_a_tariff_workbook_gives_one_tariff_per_month(tmp_path) -> None:
    _tariff_book(tmp_path / "电价.xlsx")
    tariffs = load_tariff_file(tmp_path, TariffFileSpec(file="电价.xlsx", plan="1G3", year=2026))

    assert [(item.valid_from, item.valid_until) for item in tariffs] == [
        (datetime(2026, 7, 1), datetime(2026, 8, 1)),
        (datetime(2026, 9, 1), datetime(2026, 10, 1)),
    ]
    schedule = TariffSchedule(tariffs, "元")
    at = datetime(2026, 7, 1, 18)
    assert schedule.price_status(at, at + timedelta(minutes=15)) == (("尖峰", 1.07), None)
    at = datetime(2026, 9, 3, 18)
    assert schedule.price_status(at, at + timedelta(minutes=15)) == (("高峰", 1.03), None)
    at = datetime(2026, 8, 3, 18)
    assert schedule.price_status(at, at + timedelta(minutes=15)) == (None, "outside_dates")
    at = datetime(2026, 7, 31, 23, 45)
    assert schedule.price_status(at, at + timedelta(minutes=15)) == (("平段", 0.57), None)


def test_a_period_with_hours_but_no_price_is_an_input_error(tmp_path) -> None:
    book_path = tmp_path / "电价.xlsx"
    _tariff_book(book_path)
    book = openpyxl.load_workbook(book_path)
    book["分时电价"]["C3"] = None  # July's 尖峰 price
    book.save(book_path)
    with pytest.raises(InputFormatError, match="尖峰"):
        load_tariff_file(tmp_path, TariffFileSpec(file="电价.xlsx", plan="1G3", year=2026))


def test_a_plan_the_workbook_does_not_have_is_an_input_error(tmp_path) -> None:
    _tariff_book(tmp_path / "电价.xlsx")
    with pytest.raises(InputFormatError, match="9G9"):
        load_tariff_file(tmp_path, TariffFileSpec(file="电价.xlsx", plan="9G9", year=2026))


def _flat(price: float, start: datetime | None = None, until: datetime | None = None) -> TariffSpec:
    return TariffSpec(periods=[{"name": "全天", "start": "00:00", "end": "24:00", "price": price}], valid_from=start, valid_until=until)


def test_several_inline_tariffs_need_dates_that_do_not_overlap(tmp_path) -> None:
    july, august = datetime(2026, 7, 1), datetime(2026, 8, 1)
    with pytest.raises(ValueError, match="valid_from"):
        TariffSchedule([_flat(1.0), _flat(2.0, august, datetime(2026, 9, 1))], "元")
    with pytest.raises(ValueError, match="overlap"):
        TariffSchedule([_flat(1.0, july, datetime(2026, 8, 15)), _flat(2.0, august, datetime(2026, 9, 1))], "元")
    manifest = Manifest(
        site={"name": "厂"},
        meters=[_meter()],
        tariff=[_flat(1.0, july, august), _flat(2.0, august, datetime(2026, 9, 1))],
    )
    schedule = tariff_schedule(tmp_path, manifest)
    at = datetime(2026, 8, 2)
    assert schedule is not None and schedule.price_status(at, at + timedelta(hours=1)) == (("全天", 2.0), None)


def test_the_tariff_workbook_is_one_of_the_frozen_input_files() -> None:
    manifest = Manifest(
        site={"name": "厂"}, meters=[_meter()], tariff={"file": "电价.xlsx", "plan": "1G3", "year": 2026}
    )
    assert manifest_files(manifest) == ["long.csv", "电价.xlsx"]
