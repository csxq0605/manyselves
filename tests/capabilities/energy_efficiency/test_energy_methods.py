"""The fixed method menu on small made-up series, and what the program refuses to guess."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest
from pydantic import ValidationError

from manyselves.capabilities.energy_efficiency.domain.inputs import (
    Batch,
    CalendarWindow,
    MeterSpec,
    normalize_meter,
    parse_times,
)
from manyselves.capabilities.energy_efficiency.domain.items import LossItem
from manyselves.capabilities.energy_efficiency.domain.methods import (
    MethodContext,
    MethodParamsError,
    item_kwh,
    parse_params,
    run_method,
)
from manyselves.capabilities.energy_efficiency.domain.series import MeterSeries
from manyselves.capabilities.energy_efficiency.domain.store import series_kind

DAY = datetime(2026, 3, 2)


def _energy_series(kw_at, days: int = 1, step: int = 15, skip=lambda moment: False) -> MeterSeries:
    spec = MeterSpec(
        id="M1",
        name="表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics="interval_energy",
        unit="kWh",
        interval_minutes=step,
        timestamp_marks="start",
    )
    rows = []
    moment = DAY
    while moment < DAY + timedelta(days=days):
        if not skip(moment):
            rows.append((moment.strftime("%Y-%m-%d %H:%M"), str(kw_at(moment) * step / 60)))
        moment += timedelta(minutes=step)
    frame = pd.DataFrame({"t": [row[0] for row in rows], "v": [row[1] for row in rows]})
    normalized, _quality = normalize_meter(spec, frame, None)
    return MeterSeries(spec.id, series_kind(spec), normalized, step)


def _ctx(days: int = 1, **values) -> MethodContext:
    return MethodContext(period=(DAY, DAY + timedelta(days=days)), **values)


# --- times and files -------------------------------------------------------


def test_day_first_times_are_not_guessed() -> None:
    values = pd.Series(["03/04/2026 08:00", "2026-04-03 08:00"])
    parsed = parse_times(values, "m.csv:t")
    assert pd.isna(parsed.iloc[0])  # 3 April or 4 March cannot be told
    assert parsed.iloc[1] == pd.Timestamp("2026-04-03 08:00")

    declared = parse_times(values, "m.csv:t", "%d/%m/%Y %H:%M")
    assert declared.iloc[0] == pd.Timestamp("2026-04-03 08:00")
    assert pd.isna(declared.iloc[1])  # a row in another writing is unreadable, not guessed


@pytest.mark.parametrize("name", ["../other/m.csv", "/etc/m.csv", "C:/data/m.csv", "a/../../m.csv"])
def test_data_files_must_be_inside_the_input_folder(name: str) -> None:
    with pytest.raises(ValidationError, match="inside the input folder"):
        MeterSpec(
            id="M1",
            name="表",
            file=name,
            time_column="t",
            value_column="v",
            semantics="instant_power",
            unit="kW",
            interval_minutes=5,
        )


# --- windows ---------------------------------------------------------------


def test_overlapping_windows_are_refused() -> None:
    with pytest.raises(MethodParamsError, match="overlap"):
        parse_params(
            "fixed-windows",
            {
                "windows": [
                    {"start": "2026-03-02 00:00", "end": "2026-03-02 06:00"},
                    {"start": "2026-03-02 05:00", "end": "2026-03-02 08:00"},
                ]
            },
        )
    with pytest.raises(ValidationError, match="overlap"):
        LossItem.model_validate(
            {
                "key": "A1",
                "meter_ids": ["M1"],
                "windows": [
                    {"start": "2026-03-02 00:00", "end": "2026-03-02 06:00"},
                    {"start": "2026-03-02 05:00", "end": "2026-03-02 08:00"},
                ],
                "finding_method": {"method_id": "none", "description": "看画像"},
                "phenomenon": "夜间不停",
                "evidence_ids": ["EV-0123456789"],
                "reasoning": "画像",
                "root_cause_candidates": [
                    {"cause": "未停机", "basis": "general_knowledge", "basis_detail": "常识", "how_to_check": "问"}
                ],
            }
        )


def test_calendar_rows_that_overlap_are_measured_once() -> None:
    series = _energy_series(lambda moment: 10.0)
    calendar = [
        CalendarWindow(row=2, state="idle", start=DAY, end=DAY + timedelta(hours=6)),
        CalendarWindow(row=3, state="idle", start=DAY + timedelta(hours=4), end=DAY + timedelta(hours=8)),
    ]
    run = run_method("calendar-state", {"state": "idle"}, series, _ctx(calendar=calendar), 240.0)

    assert run.summary["occurrences"] == 1
    assert run.summary["rows_merged"] == 1
    assert run.summary["total_kwh"] == 80.0  # 8 hours at 10 kW, not 100


def test_calendar_rows_that_touch_are_one_stretch() -> None:
    # Hourly records from hh:30 to hh+1:30; one record spans 12:00, where two rows touch.
    spec = MeterSpec(
        id="M1",
        name="表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics="interval_energy",
        unit="kWh",
        interval_minutes=60,
        timestamp_marks="start",
    )
    moments = [DAY - timedelta(minutes=30) + timedelta(hours=n) for n in range(26)]
    frame = pd.DataFrame({"t": [m.strftime("%Y-%m-%d %H:%M") for m in moments], "v": ["10"] * 26})
    normalized, _quality = normalize_meter(spec, frame, None)
    series = MeterSeries(spec.id, series_kind(spec), normalized, 60)
    calendar = [
        CalendarWindow(row=2, state="idle", start=DAY, end=DAY + timedelta(hours=12)),
        CalendarWindow(row=3, state="idle", start=DAY + timedelta(hours=12), end=DAY + timedelta(days=1)),
    ]
    run = run_method("calendar-state", {"state": "idle"}, series, _ctx(calendar=calendar), None)

    assert run.summary["occurrences"] == 1
    assert run.summary["total_kwh"] == 230.0  # the 11:30-12:30 record is not lost


# --- baseload --------------------------------------------------------------


def test_base_energy_never_exceeds_the_day() -> None:
    # 2 kW at night, 50 kW from 08:00 to 18:00.
    series = _energy_series(lambda moment: 50.0 if 8 <= moment.hour < 18 else 2.0, days=2)
    run = run_method("baseload", {"percentile": 10}, series, _ctx(days=2), None)

    for occurrence in run.occurrences:
        assert occurrence.extra["base_kw"] == 2.0
        assert occurrence.extra["base_kwh"] == 48.0  # 2 kW for 24 hours
        assert 0 < occurrence.extra["base_share"] <= 1
    assert item_kwh(run) == 96.0


def test_baseload_counts_only_the_days_that_hit() -> None:
    # Day one has a 20 kW base, day two a 2 kW base.
    series = _energy_series(lambda moment: 20.0 if moment.day == 2 else 2.0, days=2)
    run = run_method("baseload", {"min_avg_kw": 10}, series, _ctx(days=2), 528.0)

    assert [occurrence.hit for occurrence in run.occurrences] == [True, False]
    assert run.summary["hit_base_kwh"] == 480.0
    assert item_kwh(run) == 480.0
    assert run.summary["share_of_meter"] == round(480 / 528, 4)


def test_a_record_that_spans_a_gap_weighs_as_long_as_it_lasts() -> None:
    # Cumulative readings stop from 00:00 to 20:00: one 20-hour interval at
    # 1 kW on average, then sixteen quarter hours at 30 kW.
    spec = MeterSpec(
        id="M1",
        name="表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics="cumulative_energy",
        unit="kWh",
        interval_minutes=15,
    )
    # One reading before and after the day: the first and last climbs of an
    # export are left out.
    moments = [DAY - timedelta(minutes=15), DAY] + [DAY + timedelta(hours=20, minutes=15 * n) for n in range(18)]
    readings = [99.75, 100.0] + [120.0 + 7.5 * n for n in range(18)]
    frame = pd.DataFrame({"t": [m.strftime("%Y-%m-%d %H:%M") for m in moments], "v": [str(r) for r in readings]})
    normalized, _quality = normalize_meter(spec, frame, None)
    series = MeterSeries(spec.id, series_kind(spec), normalized, 15)
    run = run_method("baseload", {"percentile": 10}, series, _ctx(), None)
    day = run.occurrences[0]

    # Counted as one sample among seventeen, the long record would sit below
    # the 10th percentile and the base would be 30 kW.
    assert day.extra["base_kw"] == 1.0
    assert day.extra["base_kwh"] == 24.0  # 20 h at 1 kW + 16 quarter hours at 1 kW
    assert day.measure.energy_kwh == 140.0


# --- production gaps -------------------------------------------------------


def _batches(*spans: tuple[int, int, int, int], problem_rows: tuple[int, ...] = ()) -> list[Batch]:
    batches = []
    for row, (h1, m1, h2, m2) in enumerate(spans, start=2):
        batches.append(
            Batch(
                row=row,
                machine="IM-01",
                start=DAY.replace(hour=h1, minute=m1),
                end=DAY.replace(hour=h2, minute=m2) if row not in problem_rows else None,
                problem="结束时间缺失" if row in problem_rows else None,
            )
        )
    return batches


def test_a_batch_inside_a_longer_batch_leaves_no_gap() -> None:
    series = _energy_series(lambda moment: 5.0)
    # 08:00-17:00 runs the whole day; 09:00-10:00 runs inside it; next batch 18:00.
    batches = _batches((8, 0, 17, 0), (9, 0, 10, 0), (18, 0, 19, 0))
    ctx = _ctx(batches=batches, machines={"M1": ["IM-01"]})
    run = run_method("production-gaps", {"machine": "IM-01"}, series, ctx, None)

    assert [(o.measure.start.hour, o.measure.end.hour) for o in run.occurrences] == [(17, 18)]
    assert run.summary["batches_overlapping"] == 1


def test_a_gap_with_an_incomplete_batch_in_it_is_not_used() -> None:
    series = _energy_series(lambda moment: 5.0)
    # Row 3 starts at 12:00 but has no end: the machine was busy then.
    batches = _batches((8, 0, 11, 0), (12, 0, 0, 0), (14, 0, 16, 0), problem_rows=(3,))
    ctx = _ctx(batches=batches, machines={"M1": ["IM-01"]})
    run = run_method("production-gaps", {"machine": "IM-01"}, series, ctx, None)

    assert run.occurrences == []
    assert run.summary["batches_unusable"] == 1
    assert run.summary["gaps_skipped_unusable_batch"] == 1


# --- tariff ----------------------------------------------------------------


def test_a_record_across_two_tariff_periods_is_not_priced() -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import TariffSpec

    tariff = TariffSpec(
        periods=[
            {"name": "峰", "start": "08:00", "end": "22:00", "price": 1.0},
            {"name": "谷", "start": "22:00", "end": "08:00", "price": 0.4},
        ]
    )
    assert tariff.price_over(DAY.replace(hour=22), DAY.replace(hour=23)) == ("谷", 0.4)
    assert tariff.price_over(DAY.replace(hour=23), DAY + timedelta(days=1, hours=7)) == ("谷", 0.4)
    assert tariff.price_over(DAY.replace(hour=21, minute=45), DAY.replace(hour=22, minute=15)) is None
    assert tariff.price_over(DAY.replace(hour=7), DAY.replace(hour=9)) is None


def test_one_price_across_midnight_is_still_one_price() -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import TariffSpec

    flat = TariffSpec(periods=[{"name": "平", "start": "00:00", "end": "24:00", "price": 0.7}])
    split = TariffSpec(
        periods=[
            {"name": "峰", "start": "08:00", "end": "22:00", "price": 1.0},
            {"name": "谷", "start": "22:00", "end": "24:00", "price": 0.4},
            {"name": "谷", "start": "00:00", "end": "08:00", "price": 0.4},
        ]
    )
    night = (DAY.replace(hour=23, minute=30), DAY + timedelta(days=1, minutes=30))
    assert flat.price_over(*night) == ("平", 0.7)
    assert flat.price_over(DAY.replace(hour=8), DAY + timedelta(days=1, hours=8)) == ("平", 0.7)
    assert split.price_over(DAY.replace(hour=23), DAY + timedelta(days=1, hours=1)) == ("谷", 0.4)
    uncovered = TariffSpec(periods=[{"name": "峰", "start": "08:00", "end": "22:00", "price": 1.0}])
    assert uncovered.price_over(DAY.replace(hour=23), DAY + timedelta(days=1)) is None


# --- review round two ------------------------------------------------------


def test_24_00_cannot_start_a_daily_window() -> None:
    with pytest.raises(MethodParamsError, match="24:00"):
        parse_params("daily-clock-window", {"start_time": "24:00", "end_time": "06:00"})


def test_nothing_measured_is_empty_not_zero() -> None:
    series = _energy_series(lambda moment: 5.0)
    window = {"start": "2026-03-05 00:00", "end": "2026-03-05 06:00"}  # no data that day
    run = run_method("fixed-windows", {"windows": [window]}, series, _ctx(days=4), 120.0)

    assert run.summary["with_data"] == 0
    assert run.summary["total_kwh"] is None
    assert run.summary["share_of_meter"] is None
    assert item_kwh(run) is None


@pytest.mark.parametrize(
    ("start_hour", "end_hour"),
    [
        (11, None),  # starts when the morning batch ends, end unknown
        (None, 14),  # ends when the afternoon batch starts, start unknown
        (10, None),  # starts while the morning batch still runs, end unknown
    ],
)
def test_a_gap_an_incomplete_batch_may_fall_in_is_not_used(start_hour, end_hour) -> None:
    series = _energy_series(lambda moment: 5.0)
    batches = _batches((8, 0, 11, 0), (14, 0, 16, 0))
    batches.append(
        Batch(
            row=9,
            machine="IM-01",
            start=DAY.replace(hour=start_hour) if start_hour is not None else None,
            end=DAY.replace(hour=end_hour) if end_hour is not None else None,
            problem="time_missing",
        )
    )
    ctx = _ctx(batches=batches, machines={"M1": ["IM-01"]})
    run = run_method("production-gaps", {"machine": "IM-01"}, series, ctx, None)

    assert run.occurrences == []
    assert run.summary["gaps_skipped_unusable_batch"] == 1


def test_profile_base_power_and_days_cut_by_the_period() -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import MeterQuality
    from manyselves.capabilities.energy_efficiency.domain.profile import meter_profile, profile_text

    series = _energy_series(lambda moment: 2000000.0 if moment.hour >= 12 else 1.0, days=2)
    spec = MeterSpec(
        id="M1",
        name="主表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics="interval_energy",
        unit="kWh",
        interval_minutes=15,
        timestamp_marks="start",
    )
    profile = meter_profile(spec, series, MeterQuality(), (DAY + timedelta(hours=12), DAY + timedelta(days=2)))
    text = profile_text(profile)

    assert "e+" not in text  # large numbers are written out, not in exponent form
    assert "03-02" in text and "12:00 起" in text  # the cut day says so
    assert profile["days"][0]["coverage"] == 0.5
    # The base power weighs records by duration (all quarter hours here): the night's 1 kW.
    assert profile["median_daily_p10_kw"] == 1.0


def test_baseload_stability_follows_the_days_that_hit() -> None:
    from manyselves.capabilities.energy_efficiency.domain.quantify import _baseload_boundary

    # Days 1-2: base 12 kW.  Days 3-4: ten quarter hours at 9.9 kW, the rest at 10.1 kW,
    # so at the 10th percentile they miss 10 kW and at the 15th they hit.
    def kw(moment: datetime) -> float:
        if moment.day <= 3:
            return 12.0
        return 9.9 if moment.hour < 2 or (moment.hour == 2 and moment.minute < 30) else 10.1

    series = _energy_series(kw, days=4)
    params = {"percentile": 10, "min_avg_kw": 10}
    run = run_method("baseload", params, series, _ctx(days=4), None)
    boundary = _baseload_boundary(series, params, _ctx(days=4), run)

    assert [occurrence.hit for occurrence in run.occurrences] == [True, True, False, False]
    assert boundary["max_change"] > 0.5  # all four days hit at the 15th percentile


def test_baseload_with_thresholds_and_no_measured_day_is_empty_not_zero() -> None:
    series = _energy_series(lambda moment: 5.0)  # data on 2 March only
    ctx = MethodContext(period=(DAY + timedelta(days=1), DAY + timedelta(days=3)))
    run = run_method("baseload", {"min_avg_kw": 1}, series, ctx, 120.0)

    assert run.summary["hit_base_kwh"] is None
    assert item_kwh(run) is None
    assert run.summary["base_share_of_meter"] is None


def test_baseload_shares_sit_next_to_what_they_divide() -> None:
    series = _energy_series(lambda moment: 20.0 if moment.hour < 12 else 40.0, days=2)
    run = run_method("baseload", {"min_avg_kw": 10}, series, _ctx(days=2), 1440.0)

    # Whole days: 2 x 720 kWh, all of the meter; base: 20 kW x 48 h = 960 kWh.
    assert run.summary["hit_kwh"] == 1440.0 and run.summary["share_of_meter"] == 1.0
    assert run.summary["hit_base_kwh"] == 960.0 and run.summary["base_share_of_meter"] == round(960 / 1440, 4)


def test_a_gap_between_swapped_batch_times_is_not_used() -> None:
    series = _energy_series(lambda moment: 5.0)
    batches = _batches((8, 0, 11, 0), (14, 0, 16, 0))
    # Start 15:00 and end 10:00: one of them is wrong, so the machine may have run from 10:00 to 15:00.
    batches.append(
        Batch(
            row=9,
            machine="IM-01",
            start=DAY.replace(hour=15),
            end=DAY.replace(hour=10),
            problem="end_not_after_start",
        )
    )
    ctx = _ctx(batches=batches, machines={"M1": ["IM-01"]})
    run = run_method("production-gaps", {"machine": "IM-01"}, series, ctx, None)

    assert run.occurrences == []
    assert run.summary["gaps_skipped_unusable_batch"] == 1


def test_a_day_missing_one_record_does_not_read_as_whole() -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import MeterQuality
    from manyselves.capabilities.energy_efficiency.domain.profile import meter_profile, profile_text

    series = _energy_series(lambda moment: 1.0, step=5, skip=lambda moment: moment.hour == 3 and moment.minute == 0)
    spec = MeterSpec(
        id="M1",
        name="主表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics="interval_energy",
        unit="kWh",
        interval_minutes=5,
        timestamp_marks="start",
    )
    text = profile_text(meter_profile(spec, series, MeterQuality(), (DAY, DAY + timedelta(days=1))))

    assert "覆盖全天99.7%" in text


def test_swapped_batch_times_block_what_either_time_alone_would() -> None:
    series = _energy_series(lambda moment: 5.0)
    batches = _batches((6, 0, 8, 0), (9, 0, 11, 0), (12, 0, 14, 0))
    # Start 10:30 and end 10:00 both fall inside the 09-11 batch.
    batches.append(
        Batch(row=9, machine="IM-01", start=DAY.replace(hour=10, minute=30), end=DAY.replace(hour=10), problem="end_not_after_start")
    )
    ctx = _ctx(batches=batches, machines={"M1": ["IM-01"]})
    run = run_method("production-gaps", {"machine": "IM-01"}, series, ctx, None)

    assert run.occurrences == []
    assert run.summary["gaps_skipped_unusable_batch"] == 2


# --- knowledge-base round: unknown, tariff dates, one percentile rule --------


def test_an_occurrence_without_data_is_unknown_not_a_miss() -> None:
    series = _energy_series(lambda moment: 5.0, days=2, skip=lambda moment: moment.day == 3)
    run = run_method(
        "daily-clock-window",
        {"start_time": "00:00", "end_time": "06:00", "min_avg_kw": 1},
        series,
        _ctx(days=2),
        240.0,
    )

    assert run.summary["occurrences"] == 2
    assert run.summary["hits"] == 1
    assert run.summary["unknown"] == 1
    hits = sorted(str(item.as_dict()["hit"]) for item in run.occurrences)
    assert hits == ["True", "无数据，无法判断"]


def test_without_thresholds_nothing_is_counted_unknown() -> None:
    series = _energy_series(lambda moment: 5.0, days=2, skip=lambda moment: moment.day == 3)
    run = run_method("daily-clock-window", {"start_time": "00:00", "end_time": "06:00"}, series, _ctx(days=2), 240.0)

    assert run.summary["hits"] is None
    assert run.summary["unknown"] is None
    assert all("hit" not in item.as_dict() for item in run.occurrences)


def test_baseload_day_without_data_is_unknown() -> None:
    series = _energy_series(lambda moment: 5.0, days=2, skip=lambda moment: moment.day == 3)
    run = run_method("baseload", {"min_avg_kw": 1}, series, _ctx(days=2), 240.0)

    assert run.summary["hits"] == 1
    assert run.summary["unknown"] == 1


def test_tariff_dates_and_why_a_record_has_no_price() -> None:
    from manyselves.capabilities.energy_efficiency.domain.inputs import TariffSpec

    tariff = TariffSpec(
        periods=[
            {"name": "峰", "start": "08:00", "end": "22:00", "price": 1.0},
            {"name": "谷", "start": "00:00", "end": "06:00", "price": 0.4},
        ],
        valid_from=DAY,
        valid_until=DAY + timedelta(days=1),
    )
    assert tariff.price_status(DAY.replace(hour=9), DAY.replace(hour=10)) == (("峰", 1.0), None)
    assert tariff.price_status(DAY.replace(hour=7), DAY.replace(hour=7, minute=15)) == (None, "not_covered")
    assert tariff.price_status(DAY.replace(hour=21, minute=45), DAY.replace(hour=22, minute=15)) == (
        None,
        "not_covered",
    )
    assert tariff.price_status(DAY.replace(hour=5, minute=45), DAY.replace(hour=8, minute=15))[1] == "not_covered"
    two = TariffSpec(
        periods=[
            {"name": "峰", "start": "08:00", "end": "22:00", "price": 1.0},
            {"name": "谷", "start": "22:00", "end": "08:00", "price": 0.4},
        ]
    )
    assert two.price_status(DAY.replace(hour=21, minute=45), DAY.replace(hour=22, minute=15)) == (
        None,
        "crosses_prices",
    )
    later = DAY + timedelta(days=1, hours=9)
    assert tariff.price_status(later, later + timedelta(minutes=15)) == (None, "outside_dates")
    with pytest.raises(ValidationError, match="valid_until"):
        TariffSpec(periods=two.periods, valid_from=DAY, valid_until=DAY)


def test_one_percentile_rule_for_instant_and_interval_meters() -> None:
    from manyselves.capabilities.energy_efficiency.domain.methods import base_power

    spec = MeterSpec(
        id="P1",
        name="表",
        file="p.csv",
        time_column="t",
        value_column="v",
        semantics="instant_power",
        unit="kW",
        interval_minutes=15,
    )
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    rows = [((DAY + timedelta(minutes=15 * n)).strftime("%Y-%m-%d %H:%M"), str(value)) for n, value in enumerate(values)]
    frame = pd.DataFrame({"t": [row[0] for row in rows], "v": [row[1] for row in rows]})
    normalized, _quality = normalize_meter(spec, frame, None)
    instant = MeterSeries("P1", series_kind(spec), normalized, 15)
    interval = _energy_series(lambda moment: values[(moment.hour * 60 + moment.minute) // 15 % 10])

    # Nearest rank without interpolation: the 10th percentile of 1..10 is 1, not 1.9.
    assert base_power(instant, 0, len(instant), 10) == 1.0
    assert base_power(instant, 0, len(instant), 25) == 3.0
    assert base_power(interval, 0, 10, 25) == pytest.approx(3.0)


def test_evidence_and_register_show_the_parameters_used() -> None:
    from manyselves.capabilities.energy_efficiency.domain.methods import resolved_params

    assert resolved_params("baseload", {}) == {"percentile": 10.0, "min_share": None, "min_avg_kw": None}


def test_the_cost_note_names_why_records_were_not_priced() -> None:
    from manyselves.capabilities.energy_efficiency.domain.register import cost_note

    cost = {
        "cost": None,
        "unpriced_records": 3,
        "unpriced_kwh": 4.5,
        "unpriced_by_reason": {"crosses_prices": {"records": 1, "kwh": 1.5}, "outside_dates": {"records": 2, "kwh": 3.0}},
    }
    item = {"computed": {"per_meter": {"M1": {"cost": {"window": cost, "replay": None}}}}}
    note = cost_note(item)

    assert "1 条跨两个不同价格的电价时段（1.5 kWh）" in note
    assert "2 条不在电价的适用日期内（3.0 kWh）" in note
    assert "priced_part" not in cost
