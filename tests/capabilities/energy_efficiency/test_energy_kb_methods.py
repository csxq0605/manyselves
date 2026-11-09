"""Methods rewritten as knowledge-base operator chains, on small made-up series."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from manyselves.capabilities.energy_efficiency.domain.inputs import (
    Batch,
    CalendarWindow,
    MeterSpec,
    ProductionSpec,
    normalize_meter,
)
from manyselves.capabilities.energy_efficiency.domain.methods import (
    MENU,
    MethodContext,
    MethodParamsError,
    parse_params,
    readiness,
    run_method,
)
from manyselves.capabilities.energy_efficiency.domain.series import MeterSeries
from manyselves.capabilities.energy_efficiency.domain.store import series_kind

DAY = datetime(2026, 3, 2)


def _series(kw_at, days: int = 1, step: int = 15, skip=lambda moment: False, semantics="interval_energy") -> MeterSeries:
    instant = semantics == "instant_power"
    spec = MeterSpec(
        id="M1",
        name="表",
        file="m.csv",
        time_column="t",
        value_column="v",
        semantics=semantics,
        unit="kW" if instant else "kWh",
        interval_minutes=step,
        timestamp_marks=None if instant else "start",
    )
    rows = []
    moment = DAY
    while moment < DAY + timedelta(days=days):
        if not skip(moment):
            value = kw_at(moment) if instant else kw_at(moment) * step / 60
            rows.append((moment.strftime("%Y-%m-%d %H:%M"), str(value)))
        moment += timedelta(minutes=step)
    frame = pd.DataFrame({"t": [row[0] for row in rows], "v": [row[1] for row in rows]})
    normalized, _quality = normalize_meter(spec, frame, None)
    return MeterSeries(spec.id, series_kind(spec), normalized, step)


def _ctx(days: int = 1, **values) -> MethodContext:
    return MethodContext(period=(DAY, DAY + timedelta(days=days)), **values)


def _at(hour: int, minute: int = 0, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hour, minutes=minute)


def _batch(row: int, start: datetime | None, end: datetime | None, machine: str = "L1", quantity=None, product=None):
    problem = None
    if start is None or end is None:
        problem = "time_missing"
    elif end <= start:
        problem = "end_not_after_start"
    return Batch(row=row, machine=machine, start=start, end=end, quantity=quantity, product=product, problem=problem)


def test_every_menu_method_names_its_operator_chain() -> None:
    for spec in MENU.values():
        assert spec.operators, spec.id
        assert all(op.startswith("OP") and op != "OP018" for op in spec.operators)
        entry = spec.menu_entry()
        assert entry["operators"] == list(spec.operators)


# --- threshold segments (OP005 -> OP004 -> OP007) ---------------------------


def test_threshold_segments_need_their_bound_and_a_minimum_length() -> None:
    with pytest.raises(MethodParamsError, match="needs"):
        parse_params("threshold-segments", {"comparator": "below", "min_minutes": 30})
    with pytest.raises(MethodParamsError, match="min_minutes"):
        parse_params("threshold-segments", {"comparator": "below", "high_kw": 5})
    with pytest.raises(MethodParamsError, match="does not use"):
        parse_params("threshold-segments", {"comparator": "below", "high_kw": 5, "low_kw": 1, "min_minutes": 30})


def test_low_power_segments_break_at_missing_data_and_respect_the_minimum() -> None:
    # Low from 00:00 to 03:00 with a missing record at 01:00; low again 12:00-12:15 only.
    def kw(moment):
        return 2.0 if moment.hour < 3 or (moment.hour == 12 and moment.minute == 0) else 20.0

    series = _series(kw, skip=lambda moment: moment.hour == 1 and moment.minute == 0)
    run = run_method(
        "threshold-segments",
        {"comparator": "below", "high_kw": 5, "min_minutes": 45},
        series,
        _ctx(),
        None,
    )

    spans = [(item.measure.start, item.measure.end) for item in run.occurrences]
    assert spans == [(_at(0), _at(1)), (_at(1, 15), _at(3))]
    assert run.summary["segments_shorter_than_min"] == 1
    assert run.occurrences[0].measure.energy_kwh == pytest.approx(2.0)


def test_threshold_segments_leave_out_records_in_excluded_batches() -> None:
    series = _series(lambda moment: 2.0)
    ctx = _ctx(batches=[_batch(2, _at(4), _at(6))])
    run = run_method(
        "threshold-segments",
        {"comparator": "below", "high_kw": 5, "min_minutes": 60, "exclude": {"production_machines": ["L1"]}},
        series,
        ctx,
        None,
    )

    spans = [(item.measure.start, item.measure.end) for item in run.occurrences]
    assert spans == [(_at(0), _at(4)), (_at(6), _at(0, day=1))]


def test_an_incomplete_batch_blocks_the_segment_it_may_fall_in() -> None:
    series = _series(lambda moment: 2.0)
    ctx = _ctx(batches=[_batch(2, _at(4), _at(6)), _batch(3, _at(10), None)])
    run = run_method(
        "threshold-segments",
        {"comparator": "below", "high_kw": 5, "min_minutes": 60, "exclude": {"production_machines": ["L1"]}},
        series,
        ctx,
        None,
    )

    assert [item.measure.start for item in run.occurrences] == [_at(0)]
    assert run.summary["segments_skipped_incomplete_batch"] == 1


def test_instant_readings_segment_by_their_spacing() -> None:
    series = _series(lambda moment: 1.0 if moment.hour < 2 else 9.0, semantics="instant_power", step=15)
    run = run_method("threshold-segments", {"comparator": "below", "high_kw": 5, "min_minutes": 60}, series, _ctx(), None)

    (only,) = run.occurrences
    assert (only.measure.start, only.measure.end) == (_at(0), _at(2))
    assert only.measure.records == 8
    assert only.measure.energy_kwh is None


# --- power events (OP005 -> OP037 -> OP016) ---------------------------------


def test_power_events_join_short_recorded_gaps_and_report_them() -> None:
    def kw(moment):
        on = (8 <= moment.hour < 10) or (moment.hour == 10 and moment.minute >= 30) or moment.hour == 11
        return 50.0 if on else 1.0

    series = _series(kw)
    run = run_method(
        "power-events",
        {"on_kw": 30, "merge_gap_minutes": 30, "min_minutes": 60},
        series,
        _ctx(),
        None,
    )

    (event,) = run.occurrences
    assert (event.measure.start, event.measure.end) == (_at(8), _at(12))
    assert event.extra["runs_joined"] == 2
    assert event.extra["bridged_minutes"] == 30
    assert event.extra["bridged_kwh"] == pytest.approx(0.5)
    assert event.extra["peak_kw"] == pytest.approx(50.0)


def test_power_events_do_not_bridge_missing_data() -> None:
    series = _series(
        lambda moment: 50.0 if 8 <= moment.hour < 12 else 1.0,
        skip=lambda moment: moment.hour == 10 and moment.minute == 0,
    )
    run = run_method("power-events", {"on_kw": 30, "merge_gap_minutes": 60, "min_minutes": 60}, series, _ctx(), None)

    assert [(item.measure.start, item.measure.end) for item in run.occurrences] == [
        (_at(8), _at(10)),
        (_at(10, 15), _at(12)),
    ]


# --- exclusion in daily and calendar windows (OP004) ------------------------


def test_night_windows_minus_batches_are_measured_piece_by_piece() -> None:
    series = _series(lambda moment: 4.0, days=2)
    ctx = _ctx(days=2, batches=[_batch(2, _at(23), _at(1, day=1)), _batch(3, _at(3, day=1), _at(3, 5, day=1))])
    run = run_method(
        "daily-clock-window",
        {"start_time": "22:00", "end_time": "06:00", "exclude": {"production_machines": ["L1"]}},
        series,
        ctx,
        None,
    )

    spans = [(item.measure.start, item.measure.end) for item in run.occurrences]
    assert spans == [(_at(22), _at(23)), (_at(1, day=1), _at(3, day=1)), (_at(3, 5, day=1), _at(6, day=1))]
    assert run.summary["windows_before_exclusion"] == 1
    assert run.summary["pieces_too_short"] == 0


def test_a_piece_shorter_than_one_record_is_not_an_unknown_occurrence() -> None:
    series = _series(lambda moment: 4.0)
    calendar = [CalendarWindow(row=2, state="off", start=_at(0), end=_at(6))]
    batches = [_batch(2, _at(0, 5), _at(5, 55))]
    run = run_method(
        "calendar-state",
        {"state": "off", "min_avg_kw": 1, "exclude": {"production_machines": ["L1"]}},
        series,
        _ctx(calendar=calendar, batches=batches),
        None,
    )

    assert run.occurrences == []
    assert run.summary["pieces_too_short"] == 2
    assert run.summary["unknown"] == 0


def test_excluding_an_unknown_machine_needs_input() -> None:
    series = _series(lambda moment: 4.0)
    run = run_method(
        "daily-clock-window",
        {"start_time": "22:00", "end_time": "06:00", "exclude": {"production_machines": ["L9"]}},
        series,
        _ctx(batches=[_batch(2, _at(1), _at(2))]),
        None,
    )
    assert run.status == "needs_input"
    assert "L9" in run.reason


# --- production gaps with power bands (BS-04) -------------------------------


def test_gap_bands_count_hours_and_energy_and_leave_missing_time_unknown() -> None:
    def kw(moment):
        return 1.0 if moment.hour == 2 else 10.0

    series = _series(kw, skip=lambda moment: moment.hour == 3 and moment.minute == 0)
    batches = [_batch(2, _at(0), _at(2)), _batch(3, _at(4), _at(6))]
    run = run_method(
        "production-gaps",
        {"machine": "L1", "band_edges_kw": [5]},
        series,
        _ctx(batches=batches, machines={"M1": ["L1"]}),
        None,
    )

    (gap,) = run.occurrences
    assert gap.extra["bands"] == [
        {"band": "低于 5 kW", "hours": 1.0, "kwh": 1.0},
        {"band": "不低于 5 kW", "hours": 0.75, "kwh": 7.5},
    ]
    assert gap.extra["unknown_hours"] == 0.25
    assert run.summary["band_totals"][-1] == {"band": "无记录", "hours": 0.25, "kwh": None}


# --- batch intensity (OP004 -> OP007 -> OP015 -> OP009) ---------------------


def _intensity_ctx(batches):
    return _ctx(batches=batches, machines={"M1": ["L1"]}, quantity_basis="qualified", quantity_unit="件")


def test_batch_intensity_needs_a_declared_quantity_basis() -> None:
    series = _series(lambda moment: 10.0)
    run = run_method(
        "batch-intensity",
        {"machine": "L1"},
        series,
        _ctx(batches=[_batch(2, _at(0), _at(1), quantity=10)], machines={"M1": ["L1"]}),
        None,
    )
    assert run.status == "needs_input"
    assert "数量口径" in run.reason
    with pytest.raises(ValueError, match="quantity_column"):
        ProductionSpec(file="p.csv", machine_column="m", start_column="s", end_column="e", quantity_basis="qualified")


def test_batch_intensity_keeps_the_mean_of_ratios_apart_from_sum_over_sum() -> None:
    series = _series(lambda moment: 10.0 if moment.hour < 2 else 20.0)
    batches = [
        _batch(2, _at(0), _at(1), quantity=10),  # 10 kWh / 10
        _batch(3, _at(2), _at(4), quantity=10),  # 40 kWh / 10
        _batch(4, _at(5), _at(6), quantity=0),  # no intensity
        _batch(5, _at(7), _at(9), quantity=5),
        _batch(6, _at(8), _at(10), quantity=5),  # overlaps row 5: neither has an intensity
    ]
    run = run_method("batch-intensity", {"machine": "L1", "min_kwh_per_unit": 2}, series, _intensity_ctx(batches), None)

    summary = run.summary
    assert summary["intensity_count"] == 2
    assert summary["intensity_mean"] == pytest.approx(2.5)
    assert summary["intensity_of_sums"] == pytest.approx(2.5)
    assert summary["batches_without_intensity"] == 3
    assert summary["hits"] == 1
    assert summary["unknown"] == 3
    reasons = [item.extra["no_intensity_reason"] for item in run.occurrences]
    assert reasons.count("与同机台另一批次时间重叠，电量分不开") == 2
    assert summary["intensity_unit"] == "kWh/件"


# --- readiness ---------------------------------------------------------------


def test_readiness_follows_the_inputs_only() -> None:
    bare = _ctx()
    states = {spec.id: readiness(spec, "M1", True, bare)[0] for spec in MENU.values()}
    assert states["threshold-segments"] == "ready"
    assert states["calendar-state"] == "needs_input"
    assert states["production-gaps"] == "needs_input"
    assert states["batch-intensity"] == "needs_input"
    full = _intensity_ctx([_batch(2, _at(0), _at(1), quantity=1)])
    assert readiness(MENU["batch-intensity"], "M1", True, full) == ("ready", None)
    assert readiness(MENU["batch-intensity"], "M1", False, full)[0] == "not_applicable"


def test_a_meter_without_usable_records_fits_no_method() -> None:
    full = _intensity_ctx([_batch(2, _at(0), _at(1), quantity=1)])
    for spec in MENU.values():
        assert readiness(spec, "M1", True, full, records=0) == ("not_applicable", "这块表没有可用记录")
    assert readiness(MENU["fixed-windows"], "M1", True, full, records=5) == ("ready", None)


def test_quantity_basis_is_one_of_the_knowledge_base_kinds() -> None:
    with pytest.raises(ValueError):
        ProductionSpec(
            file="p.csv", machine_column="m", start_column="s", end_column="e", quantity_column="q", quantity_basis="件数"
        )
    spec = ProductionSpec(
        file="p.csv", machine_column="m", start_column="s", end_column="e", quantity_column="q", quantity_basis="input"
    )
    assert spec.quantity_basis == "input"


def test_batch_intensity_is_split_by_product_and_names_its_basis() -> None:
    series = _series(lambda moment: 10.0)
    batches = [
        _batch(2, _at(0), _at(1), quantity=10, product="红"),
        _batch(3, _at(2), _at(3), quantity=5, product="白"),
        _batch(4, _at(4), _at(5), quantity=5, product="白"),
    ]
    ctx = _ctx(batches=batches, machines={"M1": ["L1"]}, quantity_basis="input", quantity_unit="kg")
    run = run_method("batch-intensity", {"machine": "L1"}, series, ctx, None)

    assert run.summary["quantity_basis"] == "投入量"
    assert run.summary["by_product"] == {
        "白": {"count": 2, "median": 2.0, "of_sums": 2.0},
        "红": {"count": 1, "median": 1.0, "of_sums": 1.0},
    }
    only = run_method("batch-intensity", {"machine": "L1", "product": "白"}, series, ctx, None)
    assert only.summary["by_product"] is None and only.summary["intensity_count"] == 2
