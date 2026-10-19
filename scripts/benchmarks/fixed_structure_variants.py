"""Robustness of the fixed-structure pipeline when cell contents vary.

The input tables, their columns and the cell→checkpoint→module mapping are
fixed product structure.  What varies between projects is whether a cell is
filled and whether values agree.  This check copies one S4-4 workbook into
variants declared below (never edits the original), runs the production
mapper and the joint-dossier builder on each, and reports pre-declared
checks.  No Provider is called and no parameter is tuned on the results.

Variants (fixed before any run):

* ``baseline`` — the workbook as given;
* ``clear_capacity`` — empty column D (capacity) in 低配/总配;
* ``clear_ambient`` — empty column B (ambient temperature) in the thermal sheet;
* ``clear_thermal_readings`` — empty column G (measured temperature): the
  thermal checkpoints have no content at all;
* ``inject_load_mismatch`` — for every object that has a load rate in 低配/总配
  and a thermal row, write a different load rate into the thermal row;
* ``respell_rooms`` — thermal room names: "#"→"号", full-width digits and an
  inserted space, so the same cabinets are spelled differently across sheets.

Checks (fixed before any run):

* every row whose capacity was cleared states that the load rate cannot be
  computed, and carries no load-rate measurement;
* every thermal record without ambient temperature marks the rise as missing;
* empty thermal readings are reported as a gap, not silently dropped;
* every injected mismatch reaches a dossier with ``same_quantity_differs``;
* respelled objects still align with the other sheets (same aligned set);
* the mapper and dossier builder do not raise.

    uv run python scripts/benchmarks/fixed_structure_variants.py \\
        --s4-4 /path/to/Inputs/S4-4.xlsx --output out.json
    uv run python scripts/benchmarks/fixed_structure_variants.py --synthetic --output out.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from openpyxl import Workbook, load_workbook

from manyselves.capabilities.distribution_reporting.runtime.joint_dossiers import (
    build_joint_dossiers,
    object_key,
)
from manyselves.capabilities.distribution_reporting.runtime.mappers import map_s4_4

LOAD_SHEETS = ("低配评估详情", "总配评估详情表")
THERMAL = "红外热成像检测记录表"
FIRST_ROW = 3
FULL_WIDTH = str.maketrans("0123456789", "０１２３４５６７８９")


def _rows(sheet, first: int = FIRST_ROW):
    return range(first, sheet.max_row + 1)


def clear_capacity(workbook) -> dict:
    cleared = []
    for title in LOAD_SHEETS:
        if title not in workbook.sheetnames:
            continue
        sheet = workbook[title]
        for row in _rows(sheet):
            if sheet.cell(row, 2).value and sheet.cell(row, 4).value not in (None, ""):
                sheet.cell(row, 4).value = None
                cleared.append((title, row))
    return {"cleared_rows": cleared}


def clear_ambient(workbook) -> dict:
    if THERMAL not in workbook.sheetnames:
        return {"cleared_rows": []}
    sheet = workbook[THERMAL]
    cleared = [row for row in _rows(sheet) if sheet.cell(row, 2).value not in (None, "")]
    for row in cleared:
        sheet.cell(row, 2).value = None
    return {"cleared_rows": cleared}


def clear_thermal_readings(workbook) -> dict:
    if THERMAL not in workbook.sheetnames:
        return {"cleared_rows": []}
    sheet = workbook[THERMAL]
    cleared = [row for row in _rows(sheet) if sheet.cell(row, 7).value not in (None, "")]
    for row in cleared:
        sheet.cell(row, 7).value = None
    return {"cleared_rows": cleared}


def _thermal_context(sheet):
    """Room/cabinet per thermal row, following vertical merges like the mapper."""

    merges = {
        column: [
            (area.min_row, area.max_row) for area in sheet.merged_cells.ranges
            if area.min_col == area.max_col == column
        ]
        for column in (1, 4, 5)
    }

    def anchor(row: int, column: int) -> int:
        return next((start for start, end in merges[column] if start <= row <= end), row)

    for row in _rows(sheet):
        yield row, anchor(row, 1), anchor(row, 4), anchor(row, 5)


def inject_load_mismatch(workbook) -> dict:
    load_keys = set()
    for title in LOAD_SHEETS:
        if title in workbook.sheetnames:
            sheet = workbook[title]
            for row in _rows(sheet):
                room, cabinet = sheet.cell(row, 1).value, sheet.cell(row, 2).value
                if cabinet and isinstance(sheet.cell(row, 5).value, (int, float)):
                    load_keys.add(object_key(f"{str(room or '').strip()}/{str(cabinet).strip()}"))
    injected = []
    if THERMAL in workbook.sheetnames:
        sheet = workbook[THERMAL]
        done = set()
        for _row, room_row, cabinet_row, load_row in _thermal_context(sheet):
            room = sheet.cell(room_row, 1).value
            cabinet = sheet.cell(cabinet_row, 4).value
            if not room or not cabinet:
                continue
            key = object_key(f"{str(room).strip()}/{str(cabinet).strip()}")
            if key in load_keys and load_row not in done:
                current = sheet.cell(load_row, 5).value
                base = current if isinstance(current, (int, float)) else 0.0
                sheet.cell(load_row, 5).value = round(base + 0.1234, 4)
                done.add(load_row)
                injected.append(key)
    return {"injected_objects": sorted(set(injected))}


def respell_rooms(workbook) -> dict:
    changed = []
    if THERMAL in workbook.sheetnames:
        sheet = workbook[THERMAL]
        for row in _rows(sheet):
            value = sheet.cell(row, 1).value
            if isinstance(value, str) and value.strip():
                respelled = value.replace("#", "号").translate(FULL_WIDTH)
                respelled = respelled[:1] + " " + respelled[1:]
                sheet.cell(row, 1).value = respelled
                changed.append(row)
    return {"respelled_rows": changed}


VARIANTS: dict[str, Callable] = {
    "baseline": lambda workbook: {},
    "clear_capacity": clear_capacity,
    "clear_ambient": clear_ambient,
    "clear_thermal_readings": clear_thermal_readings,
    "inject_load_mismatch": inject_load_mismatch,
    "respell_rooms": respell_rooms,
}


def _map(path: Path, workspace: Path, run_id: str):
    result = map_s4_4(path, file_id="s4-4")
    items = [
        item.model_copy(update={"id": f"E-{number:04d}"})
        for number, item in enumerate(result.evidence_items, start=1)
    ]
    snapshot = workspace / f"Work/runs/{run_id}/preparation/evidence.jsonl"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        "".join(item.model_dump_json() + "\n" for item in items), encoding="utf-8",
    )
    dossiers = build_joint_dossiers(workspace, run_id, {})
    return items, result.gaps, dossiers


def _aligned(items) -> set[str]:
    sheets: dict[str, set[str]] = {}
    for item in items:
        sheets.setdefault(object_key(item.subject), set()).add(item.source.sheet or "")
    return {key for key, found in sheets.items() if len(found) >= 2}


def run(source: Path) -> dict:
    report: dict = {"variants": {}, "checks": {}}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        results = {}
        for name, mutate in VARIANTS.items():
            target = tmp_path / f"{name}.xlsx"
            shutil.copyfile(source, target)
            workbook = load_workbook(target)
            info = mutate(workbook)
            workbook.save(target)
            workbook.close()
            try:
                items, gaps, dossiers = _map(target, tmp_path / name, name)
                error = None
            except Exception as exc:  # A variant that crashes is itself a finding.
                items, gaps, dossiers, error = [], [], [], f"{type(exc).__name__}: {exc}"
            results[name] = (info, items, gaps, dossiers)
            report["variants"][name] = {
                **{key: len(value) if isinstance(value, list) else value for key, value in info.items()},
                "error": error,
                "evidence_items": len(items),
                "gaps": sorted({gap.code for gap in gaps}),
                "dossiers": len(dossiers),
                "same_quantity_dossiers": sum(
                    "same_quantity_differs" in dossier["reasons"] for dossier in dossiers
                ),
            }

    checks = report["checks"]
    checks["no_errors"] = all(v["error"] is None for v in report["variants"].values())

    info, items, _gaps, _ = results["clear_capacity"]
    cleared = {(sheet, row) for sheet, row in info["cleared_rows"]}
    affected = [
        item for item in items
        if (item.source.sheet, item.source.row) in cleared and "运行电流" in item.fact
    ]
    checks["clear_capacity_states_limit"] = {
        "rows": len(cleared),
        "items": len(affected),
        "missing_limit_statement": sum("不能" not in item.fact for item in affected),
        "has_load_rate_measurement": sum(
            any(m.quantity == "load_rate" for m in item.measurements) for item in affected
        ),
    }

    _info, items, _gaps, _ = results["clear_ambient"]
    thermal = [item for item in items if any(m.quantity == "temperature_rise" for m in item.measurements)]
    checks["clear_ambient_marks_rise_missing"] = {
        "thermal_items": len(thermal),
        "rise_not_marked_missing": sum(
            not any(m.quantity == "temperature_rise" and m.missing_reason for m in item.measurements)
            for item in thermal
        ),
    }

    info, items, gaps, _ = results["clear_thermal_readings"]
    checks["empty_thermal_readings_reported"] = {
        "cleared_rows": len(info["cleared_rows"]),
        "thermal_items_left": sum(
            item.source.sheet == THERMAL and any(m.quantity == "measured_temperature" for m in item.measurements)
            for item in items
        ),
        "gap_codes": sorted({gap.code for gap in gaps if (gap.sheet or "") == THERMAL}),
    }

    info, _items, _gaps, dossiers = results["inject_load_mismatch"]
    flagged = {
        object_key(dossier["subject"]) for dossier in dossiers
        if "same_quantity_differs" in dossier["reasons"]
    }
    injected = set(info["injected_objects"])
    checks["injected_mismatch_recall"] = {
        "injected": len(injected),
        "detected": len(injected & flagged),
        "missed": sorted(injected - flagged)[:10],
    }

    baseline_aligned = _aligned(results["baseline"][1])
    respelled_aligned = _aligned(results["respell_rooms"][1])
    checks["respelled_objects_still_align"] = {
        "baseline_aligned": len(baseline_aligned),
        "respelled_aligned": len(respelled_aligned),
        "lost": len(baseline_aligned - respelled_aligned),
    }
    return report


def synthetic_workbook(path: Path) -> Path:
    """Small workbook with the fixed S4-4 layout, for running the check offline."""

    workbook = Workbook()
    workbook.remove(workbook.active)
    for title in LOAD_SHEETS:
        sheet = workbook.create_sheet(title)
        sheet.append(["配电房", "柜号", "运行电流", "容量", "负荷率"])
        sheet.append(["", "", "A", "kVA", ""])
        for room, cabinet, current, capacity in (
            ("1#配电房", "D1701", 120, 630),
            ("1#配电房", "D1702", 300, 630),
            ("2#配电房", "G5", 80, None),
        ):
            rate = round(current * 0.4 * 1.732 / capacity, 4) if capacity else None
            sheet.append([room, cabinet, current, capacity, rate])
            if rate is not None:
                sheet.cell(sheet.max_row, 5).number_format = "0.00%"
    thermal = workbook.create_sheet(THERMAL)
    thermal.append(["配电房", "环境温度", "湿度", "柜号", "负荷率", "检测部位", "温度"])
    thermal.append(["", "℃", "%RH", "", "", "", "℃"])
    thermal.append(["1#配电房", 26, 60, "D1701", 0.6374, "A相出线", 41.5])
    thermal.append(["1#配电房", 26, 60, "D1702", None, "B相出线", 38.0])
    workbook.save(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--s4-4", dest="s4_4", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.synthetic:
        tmp = Path(tempfile.mkdtemp())
        source = synthetic_workbook(tmp / "S4-4.xlsx")
    elif args.s4_4:
        source = args.s4_4
    else:
        parser.error("give --s4-4 PATH or --synthetic")
    report = run(source)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
