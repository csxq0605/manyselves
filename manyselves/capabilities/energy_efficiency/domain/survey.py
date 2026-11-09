"""What is in an input folder that has no manifest: the program looks, the model drafts.

For every table the survey gives the first rows as they are, a guess of the
header row, and for each column under that guess: how many cells are
numbers, times or text, a few samples, how often the numbers never go down
from one row to the next (readings of a cumulative meter do not), how far
apart the times are, and which values a column with few distinct values
holds (a long table names its meters in such a column).  Small sheets are
given whole.  For JSON it gives the lists of objects it finds and which key
holds their children.  Nothing here says what a column means; the drafter
reads this and writes the manifest, and what it cannot tell goes into the
manifest's assumptions.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .inputs import _header_names, _text, parse_numbers, parse_times

TABLE_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
KNOWN_SUFFIXES = TABLE_SUFFIXES | {".json"}
TOP_ROWS = 8
HEADER_SCAN = 20
CELL_CHARS = 40
FULL_ROWS = 60
FULL_COLUMNS = 15
MAX_COLUMNS = 60
DISTINCT_SHOWN = 60
SAMPLE_ROWS = 300_000
ENCODINGS = ("utf-8-sig", "gb18030")


def _cell(value: Any) -> str:
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat(sep=" ")
    text = _text(value)
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS] + "…"


def _raw_sheets(path: Path) -> tuple[dict[str, pd.DataFrame], str | None]:
    """Every sheet with no header taken, and the encoding a CSV was read with."""

    if path.suffix.casefold() == ".csv":
        last: Exception | None = None
        for encoding in ENCODINGS:
            try:
                frame = pd.read_csv(path, dtype=str, encoding=encoding, keep_default_na=False, header=None, on_bad_lines="skip", engine="python")
                return {"": frame}, encoding
            except (UnicodeDecodeError, pd.errors.ParserError) as exc:
                last = exc
        raise ValueError(f"cannot be read with {ENCODINGS}: {last}")
    sheets = pd.read_excel(path, sheet_name=None, dtype=object, header=None)
    return {str(name): frame for name, frame in sheets.items()}, None


def _header_guess(raw: pd.DataFrame) -> int:
    """The row (from 0) among the first ones with the most text cells and data below it."""

    best, best_score = 0, -1
    for index in range(min(HEADER_SCAN, len(raw) - 1)):
        row = raw.iloc[index]
        score = sum(1 for value in row if isinstance(value, str) and value.strip() and parse_numbers(pd.Series([value])).isna().all())
        if score > best_score:
            best, best_score = index, score
    return best


def _numbers(values: pd.Series) -> pd.Series:
    return parse_numbers(values.map(lambda value: value if not isinstance(value, (datetime, pd.Timestamp)) else None))


def _times(values: pd.Series) -> pd.Series:
    try:
        return parse_times(values, "survey")
    except ValueError:
        return pd.Series(pd.NaT, index=values.index)


def _never_down(values: pd.Series) -> float | None:
    numbers = values.dropna()
    if len(numbers) < 3:
        return None
    steps = numbers.diff().dropna()
    return round(float((steps >= 0).mean()), 3)


def _spacing(times: pd.Series) -> float | None:
    gaps = times.dropna().sort_values().diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    return None if gaps.empty else round(gaps.median().total_seconds() / 60, 2)


def _columns(frame: pd.DataFrame) -> list[dict[str, Any]]:
    described = []
    group_column = None
    for name in frame.columns[:MAX_COLUMNS]:
        values = frame[name]
        filled = values.map(lambda value: bool(_text(value)) if not isinstance(value, (datetime, pd.Timestamp)) else True)
        present = values[filled]
        numbers = _numbers(present)
        times = _times(present) if numbers.notna().mean() < 0.9 or present.map(lambda v: isinstance(v, (datetime, pd.Timestamp))).any() else pd.Series(pd.NaT, index=present.index)
        entry: dict[str, Any] = {
            "name": str(name),
            "filled": int(filled.sum()),
            "numbers": int(numbers.notna().sum()),
            "times": int(times.notna().sum()),
            "samples": list(dict.fromkeys(_cell(value) for value in present.head(50)))[:3],
        }
        distinct = present.map(_cell).nunique()
        entry["distinct"] = int(distinct)
        if entry["times"] >= 0.8 * max(1, entry["filled"]) and entry["filled"]:
            entry["time_range"] = [_cell(times.min()), _cell(times.max())]
            entry["median_spacing_minutes"] = _spacing(times)
        elif entry["numbers"] >= 0.8 * max(1, entry["filled"]) and entry["filled"]:
            entry["range"] = [round(float(numbers.min()), 4), round(float(numbers.max()), 4)]
            entry["share_never_down"] = _never_down(numbers)
        if 1 < distinct <= DISTINCT_SHOWN and entry["filled"] >= 3 * distinct:
            counts = present.map(_cell).value_counts()
            entry["values"] = {str(value): int(count) for value, count in counts.items()}
            if group_column is None and entry["filled"] >= 10 * distinct and entry["times"] == 0:
                group_column = str(name)
        described.append(entry)
    if group_column is not None:
        # A long table: the same measures within each value of the grouping column.
        groups = frame.groupby(frame[group_column].map(_cell), sort=False)
        for entry in described:
            if entry["name"] == group_column:
                continue
            if "share_never_down" in entry:
                shares = [_never_down(_numbers(part[entry["name"]])) for _key, part in groups]
                shares = [share for share in shares if share is not None]
                if shares:
                    entry["share_never_down_within_" + group_column] = [min(shares), max(shares)]
            if "median_spacing_minutes" in entry:
                spacing = [_spacing(_times(part[entry["name"]])) for _key, part in groups]
                spacing = [value for value in spacing if value is not None]
                if spacing:
                    entry["median_spacing_within_" + group_column] = [min(spacing), max(spacing)]
    return described


def _table(path: Path, name: str) -> list[dict[str, Any]]:
    sheets, encoding = _raw_sheets(path)
    described = []
    for sheet, raw in sheets.items():
        raw = raw.dropna(how="all").reset_index(drop=True) if len(raw) else raw
        entry: dict[str, Any] = {"file": name, "sheet": sheet or None, "rows": len(raw), "columns": raw.shape[1]}
        if encoding:
            entry["encoding"] = encoding
        if raw.empty:
            described.append(entry)
            continue
        guess = _header_guess(raw)
        entry["header_row_guess"] = guess + 1
        frame = raw.iloc[guess + 1 :].head(SAMPLE_ROWS).reset_index(drop=True)
        frame.columns = _header_names(raw.iloc[guess : guess + 1])
        if len(raw) <= FULL_ROWS and raw.shape[1] <= FULL_COLUMNS:
            entry["all_rows"] = [[_cell(value) for value in row] for row in raw.itertuples(index=False)]
        else:
            entry["top_rows"] = [[_cell(value) for value in row] for row in raw.head(TOP_ROWS).itertuples(index=False)]
            entry["columns_under_guess"] = _columns(frame)
            if len(raw) - guess - 1 > SAMPLE_ROWS:
                entry["note"] = f"列的统计只看了表头下前 {SAMPLE_ROWS} 行"
        described.append(entry)
    return described


def _lists(data: Any, path: list[str | int], found: list[dict[str, Any]], depth: int = 0) -> None:
    """Lists of objects in a JSON document, the shallowest first, and the key that nests them."""

    if depth > 6 or len(found) >= 5:
        return
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data[:20]):
        keys = sorted({key for item in data[:50] for key in item})
        nesting = [
            key
            for key in keys
            if any(isinstance(item.get(key), list) and item.get(key) and isinstance(item[key][0], dict) for item in data[:50])
        ]
        found.append(
            {
                "path": path,
                "items": len(data),
                "keys": keys[:30],
                "children_keys": nesting,
                "first": {key: _cell(value) for key, value in data[0].items() if not isinstance(value, (list, dict))},
                "nodes_below": _count(data, nesting[0]) if nesting else len(data),
            }
        )
        return
    if isinstance(data, dict):
        for key, value in data.items():
            _lists(value, [*path, key], found, depth + 1)


def _count(items: list[Any], key: str) -> int:
    total, stack = 0, list(items)
    while stack and total < 1_000_000:
        item = stack.pop()
        if isinstance(item, dict):
            total += 1
            children = item.get(key)
            if isinstance(children, list):
                stack.extend(children)
    return total


def _json(path: Path, name: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    found: list[dict[str, Any]] = []
    _lists(data, [], found)
    top = sorted(data) if isinstance(data, dict) else f"list of {len(data)}"
    return {"file": name, "kind": "json", "top": top, "lists_of_objects": found}


def survey_folder(folder: Path) -> dict[str, Any]:
    """Every file the reader could take, described; the others listed with why not."""

    folder = Path(folder)
    tables, documents, skipped = [], [], []
    for path in sorted(item for item in folder.rglob("*") if item.is_file()):
        name = path.relative_to(folder).as_posix()
        if any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        suffix = path.suffix.casefold()
        if suffix not in KNOWN_SUFFIXES:
            skipped.append({"file": name, "reason": "只读 .csv、.xlsx、.xlsm 和 .json"})
            continue
        try:
            if suffix == ".json":
                documents.append(_json(path, name))
            else:
                tables += _table(path, name)
        except (ValueError, OSError, UnicodeDecodeError, zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
            skipped.append({"file": name, "reason": f"读不出：{exc}"[:200]})
    return {"tables": tables, "documents": documents, "skipped": skipped}


__all__ = ["KNOWN_SUFFIXES", "survey_folder"]
