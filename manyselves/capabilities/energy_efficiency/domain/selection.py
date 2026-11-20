"""Decide which rows of the loss items go into the report: a person's marks, then the person's rules, then the screening.

A person marks items in the workbook (取舍: 保留 / 不保留 / 需补资料).
Rules are written by a person in a YAML file and may only read the numbers
the program computed (``scalars``) and a few structural fields; they never
read the model's text.  A mark wins over a rule; a rule wins over the
screening (有效 → keep, 无效 → drop, 待定 → needs_data), which the program
ran from the preference rules alone.

A rule file is checked when it is loaded: a condition must use an operator
that fits its field and a value of the field's type, so a mistyped rule is
refused instead of quietly matching nothing.  A number the program could not
compute is empty; only ``is_null`` / ``not_null`` match an empty number.

A mark is matched to its item by 编号 and 核对码 together: a marks file from
another run, or with rows edited out of place, is refused.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .lenses import LENSES
from .screen import RULES as SCREEN_RULES
from .workbook import (
    CHOICE_HEADER,
    CODE_HEADER,
    ID_HEADER,
    LABEL_DECISIONS,
    LEDGER_SHEET,
    LEVEL_HEADER,
    NOTE_HEADER,
    SCREEN_DECISIONS,
)

Decision = Literal["keep", "drop", "needs_data"]
NUMBER_FIELDS = {"excess_kwh", "conservative_kwh", "liberal_kwh", "share_of_meter", "meter_kwh", "hours", "money", "overlap_kwh", "data_coverage", "half_ratio", "largest_day_share"}
TEXT_FIELDS = {"lens", "system", "source", "screen", "screen_rule"}
LIST_FIELDS = {"meter_ids"}
STRUCTURAL_FIELDS = TEXT_FIELDS | LIST_FIELDS
RULE_FIELDS = NUMBER_FIELDS | STRUCTURAL_FIELDS
SOURCES = {"program", "rule", "model"}
SCREEN_RESULTS = {"有效", "无效", "待定"}
_NULL_OPS = {"is_null", "not_null"}
_OPS_BY_KIND = {
    "number": {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"} | _NULL_OPS,
    "text": {"eq", "ne", "in", "not_in"} | _NULL_OPS,
    "list": {"contains", "not_contains"},
}


def _kind(field: str) -> str:
    if field in NUMBER_FIELDS:
        return "number"
    return "list" if field in LIST_FIELDS else "text"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SelectionError(ValueError):
    pass


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: Literal[
        "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "not_contains", "is_null", "not_null"
    ]
    value: Any = None

    @field_validator("field")
    @classmethod
    def program_field(cls, value: str) -> str:
        if value not in RULE_FIELDS:
            raise ValueError(
                f"rules may only read program fields; {value!r} is not one of {sorted(RULE_FIELDS)}"
            )
        return value

    @model_validator(mode="after")
    def fits_field(self) -> Condition:
        kind = _kind(self.field)
        if self.op not in _OPS_BY_KIND[kind]:
            raise ValueError(
                f"{self.field} cannot use {self.op}; it takes {sorted(_OPS_BY_KIND[kind])}"
            )
        if self.op in _NULL_OPS:
            if self.value is not None:
                raise ValueError(f"{self.op} takes no value")
            return self
        if kind == "list":
            if not isinstance(self.value, str) or not self.value:
                raise ValueError(f"{self.field} {self.op} needs one meter id")
            return self
        values = self.value if self.op in {"in", "not_in"} else [self.value]
        if self.op in {"in", "not_in"} and (not isinstance(self.value, list) or not self.value):
            raise ValueError(f"{self.op} needs a non-empty list")
        for value in values:
            if kind == "number" and not _is_number(value):
                raise ValueError(f"{self.field} is a number; {value!r} is not")
            if kind == "text" and not isinstance(value, str):
                raise ValueError(f"{self.field} is text; {value!r} is not")
            if self.field == "source" and value not in SOURCES:
                raise ValueError(f"source is one of {sorted(SOURCES)}; {value!r} is not")
            if self.field == "lens" and value not in LENSES:
                raise ValueError(f"lens is one of {sorted(LENSES)}; {value!r} is not")
            if self.field == "screen" and value not in SCREEN_RESULTS:
                raise ValueError(f"screen is one of {sorted(SCREEN_RESULTS)}; {value!r} is not")
            if self.field == "screen_rule" and value not in SCREEN_RULES:
                raise ValueError(f"screen_rule is one of {sorted(SCREEN_RULES)}; {value!r} is not")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: list[Condition] = Field(min_length=1)
    decision: Decision
    note: str = ""


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(min_length=1)


def load_rules(path: Path) -> RuleSet:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return RuleSet.model_validate(data)
    except (OSError, yaml.YAMLError, UnicodeDecodeError, ValidationError) as exc:
        raise SelectionError(f"rules file is not valid: {exc}") from exc


def _field_value(item: dict[str, Any], name: str) -> Any:
    if name == "screen":
        return item["screen"]["result"]
    if name == "screen_rule":
        return item["screen"]["rule"]
    if name in STRUCTURAL_FIELDS:
        return item[name]
    return item["scalars"].get(name)


def _holds(condition: Condition, actual: Any) -> bool:
    op, expected = condition.op, condition.value
    if op == "is_null":
        return actual is None
    if op == "not_null":
        return actual is not None
    if op == "contains":
        return expected in (actual or [])
    if op == "not_contains":
        return expected not in (actual or [])
    if actual is None:
        return False
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "gt":
        return actual > expected
    if op == "gte":
        return actual >= expected
    if op == "lt":
        return actual < expected
    return actual <= expected


def _read_ledger(path: Path) -> tuple[pd.DataFrame, int]:
    """The ledger sheet (its first row names the column groups), with the sheet line of the first data row."""

    return pd.read_excel(path, sheet_name=LEDGER_SHEET, header=1, dtype=str).fillna(""), 3


def read_marks(path: Path, codes: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Read 编号 / 核对码 / 取舍 / 理由 / 选定参照 from the marked workbook (.xlsx) or a .csv with those columns.

    ``codes`` maps each item id of the register to its check code.  Every row
    that names an item must carry that item's code.
    """

    path = Path(path)
    suffix = path.suffix.casefold()
    if suffix not in {".csv", ".xlsx", ".xlsm"}:
        raise SelectionError(f"marks file must be the register's .xlsx or a .csv, not {suffix or 'a file without a suffix'}")
    try:
        if suffix == ".csv":
            frame, first = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False), 2
        else:
            frame, first = _read_ledger(path)
    except UnicodeDecodeError as exc:
        raise SelectionError(
            "marks file is not UTF-8 text; mark the register's .xlsx directly, or save the CSV as UTF-8"
        ) from exc
    except (OSError, ValueError, KeyError, ImportError, SyntaxError, zipfile.BadZipFile) as exc:
        raise SelectionError(f"marks file cannot be read (save the marked register as .xlsx): {exc}") from exc
    frame.columns = [str(column).strip() for column in frame.columns]
    for column in (ID_HEADER, CODE_HEADER, CHOICE_HEADER):
        if column not in frame.columns:
            raise SelectionError(f"marks file has no column {column}")
    marks: dict[str, dict[str, Any]] = {}
    problems = []
    for position, row in frame.iterrows():
        line = position + first
        item_id = str(row[ID_HEADER]).strip()
        code = str(row[CODE_HEADER]).strip()
        label = str(row[CHOICE_HEADER]).strip()
        note = str(row.get(NOTE_HEADER, "") or "").strip()
        level = str(row.get(LEVEL_HEADER, "") or "").strip()
        if not item_id and not label:
            continue
        if item_id not in codes:
            problems.append(f"第 {line} 行的编号 {item_id!r} 不在清单里")
            continue
        if code != codes[item_id]:
            problems.append(
                f"第 {line} 行编号 {item_id} 的核对码是 {code!r}，清单里这一项是 {codes[item_id]!r}"
                "（标记文件可能来自另一次运行，或编号被改过）"
            )
            continue
        if not label:
            if level:
                marks[item_id] = {"decision": None, "note": note, "level": level}
            continue
        if label not in LABEL_DECISIONS:
            problems.append(f"第 {line} 行的取舍 {label!r} 不是 保留/不保留/需补资料")
            continue
        if item_id in marks and marks[item_id]["decision"] is not None:
            problems.append(f"编号 {item_id} 标了不止一次")
            continue
        marks[item_id] = {"decision": LABEL_DECISIONS[label], "note": note, "level": level}
    if problems:
        raise SelectionError("marks file has problems: " + "；".join(problems))
    return marks


def select(
    register: dict[str, Any],
    marks: dict[str, dict[str, Any]] | None,
    rules: RuleSet | None,
) -> dict[str, Any]:
    decisions = []
    warnings: list[str] = []
    for item in register["items"]:
        screen = item["screen"]
        entry: dict[str, Any] = {
            "item_id": item["id"],
            "decision": SCREEN_DECISIONS[screen["result"]],
            "by": f"screen {screen['rule']}",
            "note": screen["reason"],
        }
        mark = (marks or {}).get(item["id"])
        if mark is not None and mark.get("level"):
            labels = [level["label"] for level in item["levels"]]
            if mark["level"] in labels:
                entry["level"] = mark["level"]
            else:
                warnings.append(f"{item['id']} 选定的参照 {mark['level']!r} 不在它的参照档 {labels} 里，按默认参照写")
        if mark is not None and mark["decision"] is not None:
            entry.update({"decision": mark["decision"], "by": "person", "note": mark["note"]})
        elif rules is not None:
            for number, rule in enumerate(rules.rules, start=1):
                if all(_holds(condition, _field_value(item, condition.field)) for condition in rule.when):
                    entry.update({"decision": rule.decision, "by": f"rule {number}", "note": rule.note})
                    break
        decisions.append(entry)
    counts: dict[str, int] = {"keep": 0, "drop": 0, "needs_data": 0}
    for entry in decisions:
        counts[entry["decision"]] += 1
    warnings += _unmatched_values(register, rules)
    if marks is not None and not any(mark["decision"] for mark in marks.values()):
        warnings.insert(0, "标记文件里没有一项填了取舍，按规则文件和筛选结果处理")
    return {
        "source_run_id": register["run_id"],
        "decisions": decisions,
        "counts": counts,
        "warnings": warnings,
    }


def _unmatched_values(register: dict[str, Any], rules: RuleSet | None) -> list[str]:
    """Rule values naming a meter or system that no item of this register has.

    A rules file may be kept for several sites, so this is said, not refused.
    """

    if rules is None:
        return []
    seen = {
        "system": {item.get("system") for item in register["items"]},
        "meter_ids": {meter_id for item in register["items"] for meter_id in item.get("meter_ids", [])},
    }
    warnings = []
    for number, rule in enumerate(rules.rules, start=1):
        for condition in rule.when:
            if condition.field not in seen or condition.value is None:
                continue
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            missing = [value for value in values if value not in seen[condition.field]]
            if missing:
                warnings.append(f"规则 {number} 的 {condition.field} 写了 {missing}，这份清单里没有这样的项")
    return warnings


__all__ = ["RuleSet", "SelectionError", "load_rules", "read_marks", "select"]
