"""Specs: what the model hands in instead of numbers, and how the program computes them.

The model never computes an amount.  Where the lenses cannot see, it writes a
spec on the ledger's own axes; the program compiles it, computes it and
checks it.  Five kinds:

- ``reference``: what this meter could have used in these hours (zero, a
  fixed power, a quantile of its own power in other hours, a fraction of its
  mean power in other hours).  It belongs to one loss item and is computed
  over the whole period into one row per meter whose reference waits for a
  person (lens ``M``); the program also says how much the excess moves when
  the hours shift by one recording interval.  Once a person copies it into
  the preferences (P39) it is a rule (lens ``R``), an item of its own.
- ``residual``: why a piece of the unexplained excess is not a loss
  (necessary service, external driver, data problem, an existing item).
  Listed as the model's explanation; only a person's rule (P40) explains it.
- ``grouping``: which batches may be compared across lines; listed for a
  person to confirm (A05 peer_group).
- ``driver``: what else may push the energy; fitted on series the data has,
  or listed as data to add.
- cause tests are part of the judgment (``probes``).

A spec that does not compile, selects nothing, names a meter or a survey
row that does not exist, or calls an unconfirmed answer confirmed, is sent
back; it never becomes a number.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .grid import Grid, SelectorError, When
from .handles import handle_part
from .lenses import Candidate

SPEC_ID = r"^[A-Za-z][A-Za-z0-9_\-]{0,39}$"
BASIS_SOURCES = ("问卷", "知识库", "数据", "领域常识")
RESIDUAL_TYPES = {
    "necessary_service": "必要服务",
    "external_driver": "外部驱动",
    "data_problem": "数据问题",
    "existing_item": "已有项",
}
REFERENCE_TYPES = {
    "zero": "零",
    "fixed_kw": "固定功率",
    "quantile_of": "本表另一时段功率的分位",
    "fraction_of": "本表另一时段平均功率的几成",
}
DRIVERS = {
    "line_energy": "产线日电量之和",
    "running_lines": "在运行档的产线数（日均）",
    "date_trend": "日期（时间趋势）",
    "weekday": "星期",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Basis(_Strict):
    source: Literal["问卷", "知识库", "数据", "领域常识"]
    ref: str = Field(default="", description="问卷写行编号；知识库写段落或类别编号；数据写候选编号或句柄；领域常识可空")
    claims_confirmed: bool = Field(default=False, description="依据写成“已确认”时填 true；问卷那一行要是已核实的")


class RefValue(_Strict):
    type: Literal["zero", "fixed_kw", "quantile_of", "fraction_of"]
    value: float | None = Field(default=None, description="fixed_kw 写 kW；quantile_of 写 0–1 的分位；fraction_of 写倍数")
    when: When | None = Field(default=None, description="quantile_of、fraction_of：取本表哪些时段的功率")
    label: str = Field(default="", max_length=30, description="这一档的名字，如 领域参照、现场参照")

    def text(self) -> str:
        if self.type == "zero":
            return "零"
        if self.type == "fixed_kw":
            return f"固定 {self.value:g} kW"
        where = self.when.text() if self.when else "全期"
        if self.type == "quantile_of":
            return f"本表在“{where}”时功率的 P{int(round((self.value or 0) * 100))}"
        return f"本表在“{where}”时平均功率 × {self.value:g}"


class ReferenceSpec(_Strict):
    kind: Literal["reference"]
    id: str = Field(pattern=SPEC_ID)
    objects: list[str] = Field(min_length=1, description="表编号")
    when: When = Field(description="在哪些时段比")
    reference: RefValue
    alternatives: list[RefValue] = Field(default_factory=list, max_length=3, description="备选参照，一起算给人选")
    basis: list[Basis] = Field(default_factory=list)
    why: str = Field(min_length=1, description="为什么这些时段本可以用得更少；出处写在 basis")


class ResidualSpec(_Strict):
    kind: Literal["residual"]
    id: str = Field(pattern=SPEC_ID)
    objects: list[str] = Field(min_length=1)
    when: When
    type: Literal["necessary_service", "external_driver", "data_problem", "existing_item"]
    item: str | None = Field(default=None, description="existing_item：已有候选的编号")
    driver: str | None = Field(default=None, description="external_driver：什么在推动（如 室外温度）")
    request: str = Field(default="", description="要补的数据或要问的事")
    basis: list[Basis] = Field(default_factory=list)
    why: str = Field(min_length=1)


class GroupingSpec(_Strict):
    kind: Literal["grouping"]
    id: str = Field(pattern=SPEC_ID)
    group: str = Field(min_length=1, description="可比组（底座 GRP 表的组名）")
    lines: list[str] = Field(default_factory=list, description="组内可比的产线表；不写指全部")
    basis: list[Basis] = Field(default_factory=list)
    why: str = Field(min_length=1, description="为什么可比：同机型、同工艺、同批量等，出处写在 basis")


class DriverSpec(_Strict):
    kind: Literal["driver"]
    id: str = Field(pattern=SPEC_ID)
    objects: list[str] = Field(min_length=1)
    driver: str = Field(min_length=1, description="line_energy、running_lines、date_trend、weekday、另一块表的编号；数据里没有的写名字并把 in_data 设为 false")
    in_data: bool = True
    when: When | None = Field(default=None, description="只看这些时段的电量（可不写）")
    request: str = Field(default="", description="数据里没有时，要补什么数据")
    why: str = Field(min_length=1)


Spec = Annotated[ReferenceSpec | ResidualSpec | GroupingSpec | DriverSpec, Field(discriminator="kind")]
SPEC_ADAPTER: TypeAdapter[Any] = TypeAdapter(Spec)
SPEC_KINDS = {"reference": "参照", "residual": "剩余解释", "grouping": "可比分组", "driver": "驱动假设"}


def parse_spec(data: dict[str, Any]) -> Any:
    return SPEC_ADAPTER.validate_python(data)


def basis_text(basis: list[Basis]) -> str:
    return "；".join(f"{entry.source}{(' ' + entry.ref) if entry.ref else ''}{'（写为已确认）' if entry.claims_confirmed else ''}" for entry in basis)


def basis_issues(basis: list[Basis], facts: dict[str, Any], key: str) -> list[dict[str, str]]:
    """A survey row must exist, and an answer called confirmed must be confirmed in the survey."""

    issues = []
    for entry in basis:
        if entry.source != "问卷":
            continue
        fact = facts.get(entry.ref)
        if fact is None:
            issues.append({"key": key, "problem": f"问卷里没有编号 {entry.ref!r} 的行（有：{', '.join(list(facts)[:20])}…）"})
        elif entry.claims_confirmed and not fact.confirmed:
            issues.append({"key": key, "problem": f"问卷 {entry.ref} 的答复状态不是已核实（{fact.status or '没写状态'}），不能写成已确认"})
    return issues


# Computing -----------------------------------------------------------------------


def reference_kw(grid: Grid, meter_id: str, value: RefValue) -> float:
    if value.type == "zero":
        return 0.0
    if value.type == "fixed_kw":
        if value.value is None or value.value < 0:
            raise SelectorError("fixed_kw 要写不小于 0 的功率")
        return float(value.value)
    if value.when is None:
        raise SelectorError(f"{value.type} 要写 when：取本表哪些时段的功率")
    mask = grid.mask(value.when, meter_id) & grid.ok_of(meter_id)
    power = grid.power_of(meter_id)[mask]
    if not len(power):
        raise SelectorError(f"参照取的时段（{value.when.text()}）在 {meter_id} 上没有数据")
    if value.type == "quantile_of":
        if value.value is None or not 0 <= value.value <= 1:
            raise SelectorError("quantile_of 的 value 是 0 到 1 的分位")
        return float(np.quantile(power, value.value))
    if value.value is None or value.value < 0:
        raise SelectorError("fraction_of 的 value 是不小于 0 的倍数")
    return float(power.mean()) * float(value.value)


def reference_candidates(grid: Grid, spec: ReferenceSpec, source: str) -> tuple[list[Candidate], list[str]]:
    """One candidate per object meter: the excess over each listed reference in the selected hours."""

    lens = "R" if source == "rule" else "M"
    problems: list[str] = []
    candidates: list[Candidate] = []
    values = [spec.reference, *spec.alternatives]
    labels = []
    for number, value in enumerate(values):
        label = value.label or ("规格参照" if number == 0 else f"备选 {number}")
        while label in labels:
            label += "'"
        labels.append(label)
    for meter_id in spec.objects:
        if meter_id not in grid.index:
            problems.append(f"没有表 {meter_id}")
            continue
        try:
            mask = grid.mask(spec.when, meter_id) & grid.ok_of(meter_id)
        except SelectorError as exc:
            problems.append(f"{meter_id}：{exc}")
            continue
        if not mask.any():
            problems.append(f"{meter_id}：选出的时段（{spec.when.text()}）是空的或没有数据")
            continue
        power = grid.power_of(meter_id)
        levels = []
        first = None
        first_kw = 0.0
        failed = False
        for label, value in zip(labels, values, strict=True):
            try:
                kw = reference_kw(grid, meter_id, value)
            except SelectorError as exc:
                problems.append(f"{meter_id} 的参照“{label}”：{exc}")
                failed = True
                break
            cells = np.where(mask, np.clip(power - kw, 0, None) * grid.step_h, 0.0)
            levels.append({"label": label, "reference": f"{value.text()}（{kw:.2f} kW）", "excess_kwh": round(float(cells.sum()), 2)})
            if first is None:
                first, first_kw = cells, kw
        if failed or first is None:
            continue
        shift = window_shift(grid, meter_id, mask, first_kw)
        hours = float(mask.sum()) * grid.step_h
        candidates.append(
            Candidate(
                id=f"{lens}-{handle_part(spec.id)}-{handle_part(meter_id)}",
                lens=lens,
                meter=meter_id,
                part=spec.id,
                title=spec.why[:80],
                selection=spec.when.text(),
                levels=levels,
                default=labels[0],
                cells=first,
                source=source,
                spec_id=spec.id,
                notes=[] if source == "rule" else ["参照由模型提议，待人确认；确认后把规格抄进偏好 P39，下次运行按人定规则算"],
                details={"hours": round(hours, 2), "energy_kwh": round(float(np.where(mask, grid.energy_of(meter_id), 0).sum()), 2), "basis": basis_text(spec.basis), "why": spec.why, "shift": shift},
                preferences=["P39"] if source == "rule" else [],
            )
        )
    return candidates, problems


def window_shift(grid: Grid, meter_id: str, mask: np.ndarray, kw: float) -> dict[str, Any]:
    """The default excess with the selected hours one recording interval of the meter earlier and later."""

    power = grid.power_of(meter_id)
    ok = grid.ok_of(meter_id)
    cells = grid.interval_cells(meter_id)
    base = float(np.where(mask, np.clip(power - kw, 0, None) * grid.step_h, 0.0).sum())
    results = {}
    for name, step in (("earlier_kwh", -cells), ("later_kwh", cells)):
        moved = np.roll(mask, step)
        if step > 0:
            moved[:step] = False
        else:
            moved[step:] = False
        moved &= ok
        results[name] = round(float(np.where(moved, np.clip(power - kw, 0, None) * grid.step_h, 0.0).sum()), 2)
    largest = max(abs(results["earlier_kwh"] - base), abs(results["later_kwh"] - base))
    return {**results, "change_share": round(largest / base, 3) if base > 0 else None, "step_hours": round(cells * grid.step_h, 4)}


def residual_mask(grid: Grid, spec: ResidualSpec) -> tuple[dict[str, np.ndarray], list[str]]:
    masks: dict[str, np.ndarray] = {}
    problems = []
    for meter_id in spec.objects:
        if meter_id not in grid.index:
            problems.append(f"没有表 {meter_id}")
            continue
        try:
            mask = grid.mask(spec.when, meter_id) & grid.ok_of(meter_id)
        except SelectorError as exc:
            problems.append(f"{meter_id}：{exc}")
            continue
        if not mask.any():
            problems.append(f"{meter_id}：选出的时段（{spec.when.text()}）是空的或没有数据")
            continue
        masks[meter_id] = mask
    return masks, problems


def _daily_values(grid: Grid, meter_id: str, when: When | None) -> tuple[np.ndarray, np.ndarray]:
    full = np.flatnonzero(grid.full_days(meter_id))
    mask = grid.mask(when, meter_id) if when else np.ones(grid.cells, dtype=bool)
    energy = np.where(mask, grid.energy_of(meter_id), 0.0)
    return full, grid.daily(energy)[full]


def driver_fit(grid: Grid, spec: DriverSpec) -> dict[str, Any]:
    """Fit each object's daily energy on the named driver; say how much of its spread the driver explains."""

    if not spec.in_data:
        return {"status": "待补数据", "request": spec.request or f"补 {spec.driver} 的逐日数据", "objects": spec.objects}
    results = []
    problems = []
    line_daily = grid.daily(grid.line_energy())
    for meter_id in spec.objects:
        if meter_id not in grid.index:
            problems.append(f"没有表 {meter_id}")
            continue
        try:
            full, y = _daily_values(grid, meter_id, spec.when)
        except SelectorError as exc:
            problems.append(f"{meter_id}：{exc}")
            continue
        if len(full) < 7:
            problems.append(f"{meter_id}：数据完整的天不到 7 天，拟合不了")
            continue
        if spec.driver == "line_energy":
            x = line_daily[full][:, None]
        elif spec.driver == "running_lines":
            x = (np.bincount(grid.day, weights=grid.running.astype(float), minlength=len(grid.day_keys)) / np.maximum(np.bincount(grid.day, minlength=len(grid.day_keys)), 1))[full][:, None]
        elif spec.driver == "date_trend":
            x = full.astype(float)[:, None]
        elif spec.driver == "weekday":
            weekday = np.array([date.fromisoformat(key).weekday() for key in grid.day_keys])[full]
            x = np.eye(7)[weekday][:, 1:]
        elif spec.driver in grid.index:
            x = grid.daily(grid.energy_of(spec.driver))[full][:, None]
        else:
            problems.append(f"驱动 {spec.driver!r} 不在数据里：写 line_energy、running_lines、date_trend、weekday 或表编号；数据里没有的把 in_data 设为 false")
            continue
        design = np.hstack([np.ones((len(full), 1)), x])
        coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
        fitted = design @ coefficients
        spread = float(np.std(y))
        left = float(np.std(y - fitted))
        total = float(((y - y.mean()) ** 2).sum())
        r2 = 1 - float(((y - fitted) ** 2).sum()) / total if total > 0 else None
        results.append(
            {
                "meter": meter_id,
                "days": int(len(full)),
                "r2": None if r2 is None else round(r2, 3),
                "std_before_kwh": round(spread, 1),
                "std_after_kwh": round(left, 1),
                "explained_share": None if not spread else round(1 - left / spread, 3),
            }
        )
    return {"status": "已拟合" if results else "拟合不了", "driver": DRIVERS.get(spec.driver, spec.driver), "fits": results, "problems": problems}


def spec_problems(grid: Grid, spec: Any, groups: set[str], candidate_ids: set[str], meters: set[str] | None) -> list[str]:
    """Why a spec cannot be computed (empty when it can)."""

    problems: list[str] = []
    objects = getattr(spec, "objects", None) or []
    if meters is not None:
        outside = [meter_id for meter_id in objects if meter_id in grid.index and meter_id not in meters]
        if outside:
            problems.append(f"表 {outside} 不在这次要看的表里（{sorted(meters)}）")
    if isinstance(spec, ReferenceSpec):
        _candidates, found = reference_candidates(grid, spec, "model")
        problems += found
    elif isinstance(spec, ResidualSpec):
        _masks, found = residual_mask(grid, spec)
        problems += found
        if spec.type == "existing_item" and spec.item not in candidate_ids:
            problems.append(f"existing_item 要写已有候选的编号，{spec.item!r} 不是")
    elif isinstance(spec, GroupingSpec):
        if spec.group not in groups:
            problems.append(f"底座里没有可比组 {spec.group!r}（有：{sorted(groups)[:20]}）")
        unknown = [line for line in spec.lines if line not in grid.line_index]
        if unknown:
            problems.append(f"{unknown} 不是产线表")
    elif isinstance(spec, DriverSpec):
        unknown = [meter_id for meter_id in spec.objects if meter_id not in grid.index]
        if unknown:
            problems.append(f"没有表 {unknown}")
        if spec.in_data and spec.driver not in DRIVERS and spec.driver not in grid.index:
            problems.append(f"驱动 {spec.driver!r} 不在数据里：写 {sorted(DRIVERS)} 或表编号；数据里没有的把 in_data 设为 false 并写 request")
        if not spec.in_data and not spec.request:
            problems.append("数据里没有的驱动要写 request：补什么数据")
        if spec.when is not None:
            for meter_id in spec.objects:
                if meter_id in grid.index:
                    try:
                        grid.mask(spec.when, meter_id)
                    except SelectorError as exc:
                        problems.append(f"{meter_id}：{exc}")
    return problems


def preference_specs(rules: list[dict[str, Any]], kind: str) -> tuple[list[Any], list[str]]:
    """The reference rules (P39) or residual rules (P40) a person confirmed, read as specs."""

    specs, problems = [], []
    for number, rule in enumerate(rules or [], start=1):
        data = dict(rule)
        data.setdefault("kind", kind)
        data.setdefault("id", f"{'P39' if kind == 'reference' else 'P40'}-{number}")
        data.setdefault("why", "人确认的规则")
        try:
            spec = parse_spec(data)
        except ValidationError as exc:
            problems.append(f"偏好 {'P39' if kind == 'reference' else 'P40'} 第 {number} 条读不了：{exc.errors()[0]['msg']}")
            continue
        if spec.kind != kind:
            problems.append(f"偏好 {'P39' if kind == 'reference' else 'P40'} 第 {number} 条的 kind 应是 {kind}")
            continue
        specs.append(spec)
    return specs, problems


def rule_snippet(spec: Any) -> str:
    """The spec as the person would paste it into the preferences (P39 or P40)."""

    import yaml

    data = spec.model_dump(mode="json", exclude_none=True)
    for key in ("kind",):
        data.pop(key, None)
    for key in list(data):
        if data[key] in ([], "", {}):
            data.pop(key)
    return yaml.safe_dump([data], allow_unicode=True, sort_keys=False).strip()


_NUMBER = re.compile(r"(\d[\d,]*\.?\d*)\s*(kWh|千瓦时|度)")


def unsourced_numbers(text: str, known: list[float]) -> list[str]:
    """Energy amounts in a text that match none of the program's numbers (a hint, never a blocker)."""

    hints = []
    for match in _NUMBER.finditer(text or ""):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if not any(abs(value - number) <= max(1.0, abs(number) * 0.02) for number in known):
            hints.append(match.group(0))
    return hints


__all__ = [
    "BASIS_SOURCES",
    "DRIVERS",
    "REFERENCE_TYPES",
    "RESIDUAL_TYPES",
    "SPEC_KINDS",
    "Basis",
    "DriverSpec",
    "GroupingSpec",
    "RefValue",
    "ReferenceSpec",
    "ResidualSpec",
    "Spec",
    "basis_issues",
    "basis_text",
    "driver_fit",
    "parse_spec",
    "preference_specs",
    "reference_candidates",
    "residual_mask",
    "rule_snippet",
    "spec_problems",
    "unsourced_numbers",
    "window_shift",
]
