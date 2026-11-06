"""What each kind of meter column means, and how a window counts records."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from manyselves.capabilities.energy_efficiency.domain.inputs import (
    InputFormatError,
    MeterSpec,
    normalize_meter,
    parse_times,
)
from manyselves.capabilities.energy_efficiency.domain.series import MeterSeries
from manyselves.capabilities.energy_efficiency.domain.store import series_kind


def _spec(semantics: str, unit: str, interval: int = 15, marks: str | None = None) -> MeterSpec:
    return MeterSpec(
        id="M1",
        name="表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics=semantics,
        unit=unit,
        interval_minutes=interval,
        timestamp_marks=marks,
    )


def _frame(rows: list[tuple[str, object]]) -> pd.DataFrame:
    return pd.DataFrame({"t": [row[0] for row in rows], "v": [str(row[1]) for row in rows]})


def _series(spec: MeterSpec, frame: pd.DataFrame) -> MeterSeries:
    normalized, _quality = normalize_meter(spec, frame, None)
    return MeterSeries(spec.id, series_kind(spec), normalized, spec.interval_minutes)


def test_interval_energy_follows_the_declared_timestamp_mark() -> None:
    rows = [("2026-03-02 00:15", 1), ("2026-03-02 00:30", 2)]
    at_end, _ = normalize_meter(_spec("interval_energy", "kWh", marks="end"), _frame(rows), None)
    at_start, _ = normalize_meter(_spec("interval_energy", "kWh", marks="start"), _frame(rows), None)

    assert at_end["start"].iloc[0] == pd.Timestamp("2026-03-02 00:00")
    assert at_end["end"].iloc[0] == pd.Timestamp("2026-03-02 00:15")
    assert at_start["start"].iloc[0] == pd.Timestamp("2026-03-02 00:15")
    assert at_start["end"].iloc[1] == pd.Timestamp("2026-03-02 00:45")


def test_interval_data_needs_its_timestamp_mark() -> None:
    with pytest.raises(ValueError, match="timestamp_marks"):
        _spec("interval_energy", "kWh")
    with pytest.raises(ValueError, match="does not fit"):
        _spec("instant_power", "kWh")


def test_units_are_converted_to_kwh() -> None:
    rows = [("2026-03-02 00:15", 500), ("2026-03-02 00:30", 1500)]
    normalized, _ = normalize_meter(_spec("interval_energy", "Wh", marks="end"), _frame(rows), None)
    assert normalized["kwh"].tolist() == [0.5, 1.5]


def _quarter_hours(values: list[float]) -> list[tuple[str, float]]:
    start = pd.Timestamp("2026-03-02 00:00")
    return [((start + pd.Timedelta(minutes=15 * n)).strftime("%Y-%m-%d %H:%M"), value) for n, value in enumerate(values)]


def test_cumulative_readings_are_not_differenced_across_a_decrease() -> None:
    rows = _quarter_hours([100, 101, 102, 3, 5, 7, 9])
    normalized, quality = normalize_meter(_spec("cumulative_energy", "kWh"), _frame(rows), None)

    # The interval across the drop and the ones on either side are left out.
    assert normalized["kwh"].tolist() == [1.0, 2.0, 2.0]
    assert normalized["start"].tolist() == [
        pd.Timestamp("2026-03-02 00:00"),
        pd.Timestamp("2026-03-02 01:00"),
        pd.Timestamp("2026-03-02 01:15"),
    ]
    assert quality.reading_decreases == ["2026-03-02 00:45:00"]
    assert quality.intervals_excluded_at_breaks == 3
    assert "不做差分" in quality.notes()[0]


def test_one_wrong_low_reading_does_not_become_a_large_interval() -> None:
    rows = _quarter_hours([100, 101, 102, 3, 4, 103, 104, 105])
    normalized, quality = normalize_meter(_spec("cumulative_energy", "kWh"), _frame(rows), None)

    # Climbing back from 4 to 103 is not counted as 99 kWh.
    assert normalized["kwh"].tolist() == [1.0, 1.0, 1.0]
    assert max(normalized["kwh"]) == 1.0
    assert quality.intervals_excluded_at_breaks == 4


def test_a_cumulative_meter_left_with_no_intervals_is_empty_not_broken() -> None:
    normalized, _quality = normalize_meter(
        _spec("cumulative_energy", "kWh"), _frame(_quarter_hours([100, 101, 3, 5])), None
    )

    assert normalized.empty
    assert list(normalized.columns) == ["start", "end", "kwh"]


def test_instant_power_is_never_turned_into_energy() -> None:
    rows = [("2026-03-02 00:00", 10), ("2026-03-02 00:05", 20), ("2026-03-02 00:10", 30)]
    series = _series(_spec("instant_power", "kW", interval=5), _frame(rows))
    measure = series.measure(datetime(2026, 3, 2), datetime(2026, 3, 2, 0, 15))

    assert not series.has_energy
    assert measure.energy_kwh is None
    assert measure.avg_kw == 20.0
    assert measure.records == 3


def test_interval_average_power_stays_power() -> None:
    rows = [("2026-03-02 00:15", 10), ("2026-03-02 00:30", 30)]
    series = _series(_spec("interval_avg_power", "kW", marks="end"), _frame(rows))
    measure = series.measure(datetime(2026, 3, 2), datetime(2026, 3, 2, 0, 30))

    assert series.kind == "avg_power"
    assert measure.energy_kwh is None
    assert measure.avg_kw == 20.0


def test_a_window_counts_only_records_wholly_inside_it() -> None:
    rows = [("2026-03-02 00:15", 1), ("2026-03-02 00:30", 2), ("2026-03-02 00:45", 4)]
    series = _series(_spec("interval_energy", "kWh", marks="end"), _frame(rows))
    measure = series.measure(datetime(2026, 3, 2, 0, 5), datetime(2026, 3, 2, 0, 45))

    assert measure.records == 2
    assert measure.partial == 1
    assert measure.energy_kwh == 6.0
    assert measure.coverage == pytest.approx(30 / 40)


def test_conflicting_repeats_are_dropped_and_counted() -> None:
    rows = [
        ("2026-03-02 00:15", 1),
        ("2026-03-02 00:15", 1),
        ("2026-03-02 00:30", 2),
        ("2026-03-02 00:30", 9),
        ("bad time", 3),
        ("2026-03-02 00:45", "x"),
    ]
    normalized, quality = normalize_meter(_spec("interval_energy", "kWh", marks="end"), _frame(rows), None)

    assert normalized["kwh"].tolist() == [1.0]
    assert quality.duplicate_rows_dropped == 1
    assert quality.conflicting_duplicate_times == 1
    assert quality.unreadable_time_rows == 1
    assert quality.unreadable_value_rows == 1


def test_times_with_a_time_zone_are_refused() -> None:
    with pytest.raises(InputFormatError, match="time zone"):
        parse_times(pd.Series(["2026-03-02T00:00:00+08:00"]), "m.csv:t")


def test_a_run_of_wrong_high_readings_is_left_out_whole() -> None:
    rows = _quarter_hours([1000, 1010, 1020, 9999, 9999, 1030, 1040, 1050])
    normalized, _quality = normalize_meter(_spec("cumulative_energy", "kWh"), _frame(rows), None)

    # The climb into 9999 is not kept as 8979 kWh.
    assert normalized["kwh"].tolist() == [10.0, 10.0, 10.0]


def test_a_run_of_zero_readings_is_not_read_as_no_use() -> None:
    rows = _quarter_hours([100, 110, 120, 0, 0, 0, 150, 160, 170])
    normalized, quality = normalize_meter(_spec("cumulative_energy", "kWh"), _frame(rows), None)

    # Neither 0 -> 0 (no use) nor 0 -> 150 (a made-up 150 kWh) is kept.
    assert normalized["kwh"].tolist() == [10.0, 10.0, 10.0]
    assert quality.intervals_excluded_at_breaks == 5


def test_a_glitch_after_a_real_reset_is_still_left_out() -> None:
    rows = _quarter_hours([5000, 5010, 5020, 0, 10, 20, 30, 40, 50, 60, 70, 3, 4, 80, 90])
    normalized, _quality = normalize_meter(_spec("cumulative_energy", "kWh"), _frame(rows), None)

    assert max(normalized["kwh"]) == 10.0


def test_local_writings_ending_in_digits_are_not_taken_for_a_time_zone() -> None:
    dates = parse_times(pd.Series(["01-05-2026", "03-05-2026"]), "cal.csv:s", "%d-%m-%Y")
    assert dates.tolist() == [pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-03")]
    stray = parse_times(pd.Series(["2026-03-02 08:15", "02-03-2026"]), "m.csv:t")
    assert stray.iloc[0] == pd.Timestamp("2026-03-02 08:15") and pd.isna(stray.iloc[1])


def test_24_00_is_midnight_of_the_next_day() -> None:
    plain = parse_times(pd.Series(["2026-03-02 24:00"]), "m.csv:t")
    declared = parse_times(pd.Series(["02/03/2026 24:00"]), "m.csv:t", "%d/%m/%Y %H:%M")
    assert plain.iloc[0] == declared.iloc[0] == pd.Timestamp("2026-03-03 00:00")


def test_spreadsheet_date_cells_keep_their_time_under_a_declared_format() -> None:
    parsed = parse_times(pd.Series(["01/05/2026", datetime(2026, 5, 1, 18, 0)]), "cal.xlsx:s", "%d/%m/%Y")
    assert parsed.tolist() == [pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-01 18:00")]


def test_infinite_values_are_unreadable() -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import parse_numbers

    assert parse_numbers(pd.Series(["inf", "-Infinity", "1.5"])).isna().tolist() == [True, True, False]


def test_empty_spreadsheet_cells_are_empty_not_nan(tmp_path) -> None:
    import openpyxl

    from manyselves.capabilities.energy_efficiency.domain.inputs import CalendarSpec, load_calendar

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["开始", "结束", "状态"])
    sheet.append([datetime(2026, 3, 7), datetime(2026, 3, 8), "non_production"])
    sheet.append([datetime(2026, 3, 9), datetime(2026, 3, 10), None])
    book.save(tmp_path / "cal.xlsx")
    spec = CalendarSpec(file="cal.xlsx", start_column="开始", end_column="结束", state_column="状态")
    windows, unusable = load_calendar(tmp_path, spec)

    assert [window.state for window in windows] == ["non_production"]
    assert unusable == 1


def test_a_broken_spreadsheet_is_an_input_error(tmp_path) -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import CalendarSpec, load_calendar

    (tmp_path / "cal.xlsx").write_bytes(b"not a zip file")
    spec = CalendarSpec(file="cal.xlsx", start_column="开始", end_column="结束", state_column="状态")
    with pytest.raises(InputFormatError, match="cannot be read"):
        load_calendar(tmp_path, spec)


def _breaks(readings: list[float]) -> tuple[pd.DataFrame, object]:
    return normalize_meter(_spec("cumulative_energy", "kWh"), _frame(_quarter_hours(readings)), None)


def test_two_link_down_zero_runs_keep_the_good_data_between_them() -> None:
    readings = [*range(100, 110), 0, 0, 0, *range(200, 1160), 0, 0, 0, *range(1500, 1510)]
    normalized, quality = _breaks(readings)

    # Only the two zero runs and the intervals next to them are left out.
    assert quality.intervals_excluded_at_breaks == 10
    assert max(normalized["kwh"]) == 1.0


@pytest.mark.parametrize(
    "readings",
    [
        [value % 100 for value in range(400)],  # a register that rolls over
        [*range(0, 100), *range(0, 100), *range(0, 100)],  # reset to zero twice
        [*range(0, 300), 0, 1, 2, 3],  # a logger that started at zero, then reset
    ],
)
def test_a_reset_or_rollover_costs_a_few_intervals_not_the_whole_cycle(readings: list[float]) -> None:
    normalized, quality = _breaks(readings)

    assert quality.intervals_excluded_at_breaks <= 5 * len(quality.reading_decreases)
    assert max(normalized["kwh"]) == 1.0


def test_wrong_high_readings_that_return_to_the_same_reading_are_left_out() -> None:
    normalized, _quality = _breaks([102, 9999, 9999, 102, 103, 104])

    assert normalized["kwh"].tolist() == [1.0]


def test_a_column_that_keeps_falling_is_all_left_out() -> None:
    readings = [100000 - 0.5 * n if n % 2 else 100000 - 0.5 * n + 0.2 for n in range(20000)]
    normalized, quality = _breaks(readings)

    assert normalized.empty
    assert quality.intervals_excluded_at_breaks == len(readings) - 1


@pytest.mark.parametrize("time_format", ["mixed", "ISO8601", "%d/%m/%Y %H:%Q", "%d/%m/%Y %H:%M%", "%Y-%m-%d %I:%M"])
def test_time_format_must_be_a_layout_that_reads_one_way(time_format: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="time_format"):
        MeterSpec(
            id="M1",
            name="表",
            file="m.csv",
            time_column="t",
            value_column="v",
            semantics="instant_power",
            unit="kW",
            interval_minutes=15,
            time_format=time_format,
        )


def test_24_00_with_zero_seconds_and_fraction_is_midnight() -> None:
    parsed = parse_times(pd.Series(["2026-03-02 24:00:00.000", "2026-03-02 24:00:00.5"]), "m.csv:t")
    assert parsed.iloc[0] == pd.Timestamp("2026-03-03 00:00") and pd.isna(parsed.iloc[1])


def test_broken_xml_inside_a_spreadsheet_is_an_input_error(tmp_path) -> None:
    import zipfile

    import openpyxl

    from manyselves.capabilities.energy_efficiency.domain.inputs import CalendarSpec, load_calendar

    book = openpyxl.Workbook()
    book.active.append(["开始", "结束", "状态"])
    book.save(tmp_path / "good.xlsx")
    with zipfile.ZipFile(tmp_path / "good.xlsx") as good, zipfile.ZipFile(tmp_path / "cal.xlsx", "w") as broken:
        for name in good.namelist():
            broken.writestr(name, b"<x><<<" if name == "xl/workbook.xml" else good.read(name))
    spec = CalendarSpec(file="cal.xlsx", start_column="开始", end_column="结束", state_column="状态")
    with pytest.raises(InputFormatError, match="cannot be read"):
        load_calendar(tmp_path, spec)


def test_a_rollover_during_a_link_down_run_is_not_one_large_interval() -> None:
    normalized, _quality = _breaks([99950, 99960, 99970, 99980, 99990, *[0] * 96, 960, 970, 980])

    # Neither the zeros (no use) nor 0 -> 960 (a made-up 960 kWh) is kept.
    assert normalized["kwh"].tolist() == [10.0] * 5


def test_one_low_reading_on_an_idle_plateau_does_not_cost_the_plateau() -> None:
    normalized, quality = _breaks([4990.0, *[5000.25] * 193, 5000.2, 5000.25, 5010.0])

    assert quality.intervals_excluded_at_breaks <= 4
    assert (normalized["kwh"] == 0.0).sum() >= 190


def test_a_long_falling_column_is_quick() -> None:
    readings = [-50000 - 0.25 * n for n in range(50000)]
    readings[25000] = 0
    normalized, _quality = _breaks(readings)

    assert normalized.empty


def test_a_moment_pandas_cannot_hold_is_unreadable_not_a_crash() -> None:
    declared = parse_times(
        pd.Series([datetime(2026, 3, 2, 8), datetime(2626, 3, 2, 8)]), "cal.xlsx:s", "%Y-%m-%d %H:%M"
    )
    plain = parse_times(pd.Series(["9999-12-31 24:00", "2026-03-02 24:00"]), "m.csv:t")

    assert declared.iloc[0] == pd.Timestamp("2026-03-02 08:00") and pd.isna(declared.iloc[1])
    assert pd.isna(plain.iloc[0]) and plain.iloc[1] == pd.Timestamp("2026-03-03 00:00")


ERROR_VALUE = 65535  # what some gateways log when a read fails


@pytest.mark.parametrize(
    "readings",
    [
        [5000, 5001, *[ERROR_VALUE] * 6, 5007, 5008, 5009, *[ERROR_VALUE] * 8, 5018, 5019, 5020],  # two outages
        [5000, 5001, *[ERROR_VALUE] * 6, 5007, *[ERROR_VALUE] * 6, 5014, 5015, 5016],  # a flaky link
        [8000, 8001, 8002, *[ERROR_VALUE] * 8, 0, 0.8, 1.6, 2.4],  # meter swapped during the outage
        [5000, 5001, 5002, 5003, 5004, *[0] * 8],  # the export ends while the link is down
    ],
)
def test_error_values_and_outages_never_read_as_no_use_or_a_jump(readings: list[float]) -> None:
    normalized, _quality = _breaks(readings)

    assert 0.0 not in normalized["kwh"].tolist()
    assert max(normalized["kwh"]) <= 1.0


def test_one_error_value_on_an_idle_plateau_does_not_cost_the_plateau() -> None:
    normalized, quality = _breaks([4990, *[5000] * 96, ERROR_VALUE, *[5000] * 96, 5010, 5020])

    assert quality.intervals_excluded_at_breaks <= 4
    assert max(normalized["kwh"]) == 10.0
