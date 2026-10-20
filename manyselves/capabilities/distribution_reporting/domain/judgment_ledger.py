"""The judgment ledger: one structured row per object and checkpoint.

Module authors submit judgment rows before any prose is written.  Everything
here is deterministic and works on the fixed input structure:

- ``ledger_findings`` checks rows against the evidence snapshot (unknown
  evidence or photos, a module fact no row covers, a submodule of another
  module) and returns findings in the generic review-finding shape;
- ``ledger_judgments`` turns rows into the per-object judgment records the
  joint dossiers are built from;
- the table builders render the problem tables of chapter 2 and the chief's
  structured chapter 1 and 3 tables, so table shape never depends on prose;
- ``report_check_findings`` is the program check of the finished text.

Nothing here tunes a threshold or a count to one run's data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .action_table import ACTION_PRIORITY_GROUPS, ACTION_TABLE_HEADERS

Disposition = Literal["concern", "no_concern", "undetermined", "not_applicable"]
Priority = Literal["重要，紧急", "重要，不紧急", "一般"]
RiskLevel = Literal["高", "中", "低"]
EvidenceStatus = Literal["现场确认", "来源作者意见", "资料未提供", "照片待核", "推断"]
EVIDENCE_STATUS_HELP = (
    "现场确认=表格或照片直接记录了该情况；来源作者意见=原表评语、既有评估等他人结论；"
    "资料未提供=缺资料，不能说明有或没有；照片待核=只靠照片且照片读数有疑问；"
    "推断=由其他事实推出，现场没有直接记录。"
)

DISPOSITION_LABELS = {
    "concern": "关注",
    "no_concern": "正常",
    "undetermined": "不能判定",
    "not_applicable": "不适用",
}
PROBLEM_TABLE_MARKER = "[[问题表]]"
PROBLEM_TABLE_HEADERS = ("对象", "检查内容", "发现问题", "风险等级", "建议措施", "现场图片")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JudgmentAction(_Model):
    measure: str = Field(min_length=1)
    priority: Priority
    timeframe: str = Field(min_length=1)
    deadline_reason: str = ""


class JudgmentGap(_Model):
    missing: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    requested_input: str = Field(min_length=1)


class JudgmentRow(_Model):
    id: str = Field(pattern=r"^J-")
    submodule_id: str = Field(min_length=1)
    object: str = Field(min_length=1)
    checkpoint: str = Field(min_length=1)
    disposition: Disposition
    evidence_status: EvidenceStatus = Field(description=EVIDENCE_STATUS_HELP)
    evidence_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    basis: str = Field(min_length=1)
    rule_refs: list[str] = Field(default_factory=list)
    risk_level: RiskLevel | None = None
    hazard: str = ""
    action: JudgmentAction | None = None
    gap: JudgmentGap | None = None
    photo_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def disposition_carries_its_fields(self) -> JudgmentRow:
        if self.disposition == "concern" and (
            self.action is None or self.risk_level is None or not self.hazard
        ):
            raise ValueError("a concern row needs risk_level, hazard and action")
        if self.disposition == "undetermined" and self.gap is None:
            raise ValueError("an undetermined row needs gap")
        return self


class JudgmentSet(_Model):
    module_id: str = Field(min_length=1)
    rows: list[JudgmentRow]


class JudgmentRevision(_Model):
    """Targeted revision of a module's rows in answer to review findings."""

    rows: list[JudgmentRow] = Field(
        default_factory=list, description="Changed or added rows, each complete"
    )
    removed_row_ids: list[str] = Field(default_factory=list)
    responses: dict[str, str] = Field(
        default_factory=dict,
        description="Finding target to the reason it was not changed",
    )


def _finding(problem: str, suggestion: str, severity: str = "blocking") -> dict[str, str]:
    return {"problem": problem, "severity": severity, "suggestion": suggestion}


def ledger_findings(
    judgment: JudgmentSet,
    *,
    submodule_ids: Iterable[str],
    module_evidence_ids: Iterable[str],
    known_evidence_ids: Iterable[str],
    known_photo_ids: Iterable[str],
    known_calculation_ids: Iterable[str] = (),
    evidence_subjects: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Return program findings keyed by row id or by the uncovered fact id.

    A concern or undetermined row covers one object: when its facts name
    several objects it must be split, so counts and groups stay per object.
    """

    submodules = set(submodule_ids)
    evidence = set(known_evidence_ids)
    photos = set(known_photo_ids)
    calculations = set(known_calculation_ids)
    findings: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    covered: set[str] = set()
    for row in judgment.rows:
        problems: list[str] = []
        if row.id in seen:
            problems.append(f"判断行编号 {row.id} 重复")
        seen.add(row.id)
        if row.submodule_id not in submodules:
            problems.append(f"小节 {row.submodule_id} 不属于模块 {judgment.module_id}")
        if unknown := sorted(set(row.evidence_ids) - evidence):
            problems.append("事实编号不存在：" + "、".join(unknown))
        if unknown := sorted(set(row.photo_ids) - photos):
            problems.append("照片编号不存在：" + "、".join(unknown))
        if unknown := sorted(set(row.calculation_ids) - calculations):
            problems.append("计算编号不存在：" + "、".join(unknown))
        if not row.evidence_ids and row.disposition != "undetermined":
            problems.append("没有引用任何事实；只有资料缺失的不能判定行可以不引用事实")
        objects = sorted({
            evidence_subjects[e] for e in row.evidence_ids if e in (evidence_subjects or {})
        })
        if row.disposition in {"concern", "undetermined"} and len(objects) > 1:
            problems.append(
                "一行判断写了多个对象（" + "、".join(objects) + "），关注项和不能判定项要一行一个对象"
            )
        covered.update(row.evidence_ids)
        if problems:
            findings[row.id] = _finding(
                "；".join(problems),
                "按事实清单改正编号或小节归属；多个对象的拆成每个对象一行，判断本身不变。",
            )
    for evidence_id in sorted(set(module_evidence_ids) - covered):
        findings[evidence_id] = _finding(
            f"本模块事实 {evidence_id} 没有进入任何判断行",
            "补一行判断（可以是正常、关注、不能判定或不适用），或并入已有的同一对象判断行。",
        )
    return findings


def ledger_judgments(
    rows_by_module: Mapping[str, Sequence[JudgmentRow]],
) -> list[dict[str, Any]]:
    """Per-row judgment records for the joint dossiers."""

    return [
        {
            "module_id": module_id,
            "submodule_id": row.submodule_id,
            "row_id": row.id,
            "evidence_ids": list(row.evidence_ids),
            "disposition": row.disposition,
            "excerpt": f"{row.object}｜{row.checkpoint}：{row.basis}",
            **({"action_excerpt": row.action.measure} if row.action else {}),
        }
        for module_id, rows in rows_by_module.items()
        for row in rows
    ]


def apply_revisions(
    rows: Sequence[JudgmentRow], revised: Sequence[JudgmentRow], removed: Iterable[str] = ()
) -> list[JudgmentRow]:
    """Replace rows by id, append new ones and drop removed ids, keeping order."""

    replacements = {row.id: row for row in revised}
    dropped = set(removed)
    merged: list[JudgmentRow] = []
    for row in rows:
        if row.id in dropped:
            continue
        merged.append(replacements.pop(row.id, row))
    merged.extend(row for row in revised if row.id in replacements)
    return merged


def _cell(text: str) -> str:
    return " ".join(str(text).replace("|", "｜").split())


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def problem_table(rows: Sequence[JudgmentRow]) -> str | None:
    """The per-object problem table of one submodule, or None without problems."""

    flagged = [row for row in rows if row.disposition in {"concern", "undetermined"}]
    if not flagged:
        return None
    has_photos = any(row.photo_ids for row in flagged)
    headers = PROBLEM_TABLE_HEADERS if has_photos else PROBLEM_TABLE_HEADERS[:-1]
    body = []
    for row in flagged:
        if row.disposition == "concern":
            problem = row.basis
            level = row.risk_level or ""
            measure = row.action.measure if row.action else ""
        else:
            problem = f"不能判定：{row.gap.missing if row.gap else row.basis}"
            level = "待定"
            measure = f"补充{row.gap.requested_input}" if row.gap else ""
        if row.evidence_status != "现场确认":
            problem = f"（{row.evidence_status}）{problem}"
        values = [row.object, row.checkpoint, problem, level, measure]
        if has_photos:
            values.append("、".join(row.photo_ids))
        body.append(values)
    return _table(headers, body)


def place_problem_table(prose: str, rows: Sequence[JudgmentRow]) -> str:
    """Put the submodule problem table at the author's marker, or after the prose."""

    table = problem_table(rows)
    text = prose.strip()
    if table is None:
        return text.replace(PROBLEM_TABLE_MARKER, "").strip()
    if PROBLEM_TABLE_MARKER in text:
        return text.replace(PROBLEM_TABLE_MARKER, "\n\n" + table + "\n\n", 1).replace(
            PROBLEM_TABLE_MARKER, ""
        ).strip()
    return text + "\n\n" + table


class ChiefAction(_Model):
    priority: Priority
    content: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    hazard: str = Field(min_length=1)
    solution: str = Field(min_length=1)
    note: str = ""
    row_ids: list[str] = Field(min_length=1)


class DimensionHealth(_Model):
    module_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    key_indicators: str = Field(min_length=1)
    risk_level: Literal["高", "中", "低", "待定"]


class KeyRisk(_Model):
    description: str = Field(min_length=1)
    hazard: str = Field(min_length=1)
    action: str = Field(min_length=1)
    row_ids: list[str] = Field(min_length=1)


class Phase(_Model):
    phase: str = Field(min_length=1)
    time: str = Field(min_length=1)
    tasks: str = Field(min_length=1)
    effect: str = Field(min_length=1)


class ChiefChapter3(_Model):
    risk_overview: str = Field(min_length=1, description="3.1 正文")
    action_lead: str = Field(min_length=1, description="3.2 表前导语")
    actions: list[ChiefAction]


class ChiefChapter1(_Model):
    assessment_background: str = Field(min_length=1, description="1.1 正文")
    overall_assessment: str = Field(min_length=1, description="1.2 表前的总体评价")
    dimension_health: list[DimensionHealth] = Field(min_length=1)
    key_risks: list[KeyRisk] = Field(default_factory=list)
    phases: list[Phase] = Field(default_factory=list)
    regional_executive_summary: str = Field(min_length=1, description="1.3 正文")


class SpecialTopicAnalysis(_Model):
    special_topic_analysis: str = Field(min_length=1)


class SectionText(_Model):
    submodule_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SectionSet(_Model):
    sections: dict[str, str] = Field(description="Section id to its complete revised text")


def _status_note(row_ids: Sequence[str], row_status: Mapping[str, str] | None) -> str:
    statuses = [
        status
        for status in dict.fromkeys((row_status or {}).get(row_id) for row_id in row_ids)
        if status and status != "现场确认"
    ]
    return "依据：" + "、".join(statuses) if statuses else ""


def action_table_markdown(
    lead: str,
    actions: Sequence[ChiefAction],
    row_status: Mapping[str, str] | None = None,
) -> str:
    """3.2: a lead sentence, then one five-column table per priority group.

    The note column carries the evidence status of the cited rows when any
    of them is not confirmed on site.
    """

    blocks = [lead.strip()]
    for group in ACTION_PRIORITY_GROUPS:
        members = [action for action in actions if action.priority == group]
        if not members:
            continue
        blocks.append(f"**{group}**")
        blocks.append(
            _table(
                ACTION_TABLE_HEADERS,
                (
                    [
                        a.content,
                        a.problem,
                        a.hazard,
                        a.solution,
                        "；".join(
                            text for text in (a.note, _status_note(a.row_ids, row_status)) if text
                        ),
                    ]
                    for a in members
                ),
            )
        )
    return "\n\n".join(block for block in blocks if block)


def findings_overview_markdown(
    assessment: str,
    health: Sequence[DimensionHealth],
    risks: Sequence[KeyRisk],
    phases: Sequence[Phase],
    module_titles: Mapping[str, str],
    row_status: Mapping[str, str] | None = None,
) -> str:
    """1.2: the overall assessment followed by the three expert-version tables.

    A key risk whose cited rows are not all confirmed on site says so.
    """

    blocks = [assessment.strip()]
    blocks.append(
        _table(
            ("评估维度", "健康状态", "关键异常指标", "风险等级"),
            (
                [module_titles.get(h.module_id, h.module_id), h.status, h.key_indicators, h.risk_level]
                for h in health
            ),
        )
    )
    if risks:
        blocks.append(
            _table(
                ("优先级", "风险描述", "危害", "行动建议"),
                (
                    [
                        str(index),
                        r.description
                        + (f"（{note}）" if (note := _status_note(r.row_ids, row_status)) else ""),
                        r.hazard,
                        r.action,
                    ]
                    for index, r in enumerate(risks, 1)
                ),
            )
        )
    if phases:
        blocks.append(
            _table(
                ("阶段", "时间", "核心任务", "预期效果"),
                ([p.phase, p.time, p.tasks, p.effect] for p in phases),
            )
        )
    return "\n\n".join(block for block in blocks if block)


def chief_findings(
    *,
    actions: Sequence[ChiefAction],
    health: Sequence[DimensionHealth],
    risks: Sequence[KeyRisk],
    rows_by_module: Mapping[str, Sequence[JudgmentRow]],
) -> dict[str, dict[str, str]]:
    """Every action row is merged into 3.2 once; every dimension has health."""

    action_rows = {
        row.id
        for rows in rows_by_module.values()
        for row in rows
        if row.action is not None
    }
    known_rows = {row.id for rows in rows_by_module.values() for row in rows}
    findings: dict[str, dict[str, str]] = {}
    cited = [row_id for action in actions for row_id in action.row_ids]
    if missing := sorted(action_rows - set(cited)):
        findings["3.2"] = _finding(
            "以下判断行的措施没有进入速查表：" + "、".join(missing),
            "把这些行并入对应的同类行动，或单列一行。",
        )
    unknown = sorted((set(cited) | {i for r in risks for i in r.row_ids}) - known_rows)
    if unknown:
        findings["row_ids"] = _finding(
            "引用了台账里没有的判断行：" + "、".join(unknown),
            "只引用台账中已有的判断行编号。",
        )
    if missing_modules := sorted(set(rows_by_module) - {h.module_id for h in health}):
        findings["1.2"] = _finding(
            "健康度表缺少维度：" + "、".join(missing_modules),
            "每个评估维度写一行。",
        )
    return findings


_NUMBER_WITH_UNIT = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*"
    r"(kVA|kW|kV|V|A|K|℃|°C|%|Ω|MΩ|mA|Hz|倍|×|个|处|台|面|组|只)(?![A-Za-z月小天年周])"
)
_UNIT_ALIASES = {"°C": "℃", "×": "倍", "处": "个", "台": "个", "面": "个", "组": "个", "只": "个"}


def numbers_with_units(text: str) -> set[tuple[str, str]]:
    """Numbers written with a unit, as written (so the precision is kept).

    Counts written with 个/处/台/面/组/只 share the unit 个; × is 倍.
    """

    return {
        (value, _UNIT_ALIASES.get(unit, unit))
        for value, unit in _NUMBER_WITH_UNIT.findall(text)
    }


def _matches_known(written: str, unit: str, known: Mapping[str, set[float]]) -> bool:
    decimals = len(written.split(".", 1)[1]) if "." in written else 0
    target = float(written)
    return any(
        round(value, decimals) == target or value == target
        for value in known.get(unit, ())
    )


def known_values_from_text(texts: Iterable[str]) -> list[tuple[float, str]]:
    return [
        (float(value), unit) for text in texts for value, unit in numbers_with_units(text)
    ]


_SENTENCE = re.compile(r"[^。；;\n]+")


def _object_names(subject: str) -> list[str]:
    parts = [part.strip() for part in subject.split("/") if part.strip()]
    return list(dict.fromkeys([subject, *parts[1:]]))


def _mispaired(
    text: str, subject_values: Mapping[str, Iterable[tuple[float, str]]]
) -> list[str]:
    """Values written next to one object that belong to a different object."""

    values = {
        subject: {(_UNIT_ALIASES.get(unit, unit), float(value)) for value, unit in pairs}
        for subject, pairs in subject_values.items()
    }
    owners: dict[tuple[str, float], set[str]] = {}
    for subject, pairs in values.items():
        for pair in pairs:
            owners.setdefault(pair, set()).add(subject)
    problems: list[str] = []
    for sentence in _SENTENCE.findall(text):
        named = {
            subject
            for subject in values
            if any(name in sentence for name in _object_names(subject))
        }
        if len(named) != 1:
            continue
        (subject,) = named
        written_values = numbers_with_units(sentence)
        for written, unit in written_values:
            if sum(1 for _, other in written_values if other == unit) > 1:
                continue  # a comparison between objects
            if (unit, float(written)) in values[subject]:
                continue
            others = owners.get((unit, float(written)), set())
            if others and any(u == unit for u, _ in values[subject]):
                problems.append(
                    f"{subject} 旁写的 {written}{unit} 是 " + "、".join(sorted(others)) + " 的值"
                )
    return problems


def report_check_findings(
    *,
    sections: Mapping[str, str],
    rows_by_submodule: Mapping[str, Sequence[JudgmentRow]],
    known_values: Iterable[tuple[float, str]],
    subject_values: Mapping[str, Iterable[tuple[float, str]]] | None = None,
) -> dict[str, dict[str, str]]:
    """Program check of the written sections against the ledger and facts.

    A number written with a unit must be a fact or calculation value at the
    written precision; a count (个/处/台…) must be a program count or a count
    of this section's rows; in a sentence naming exactly one known object, a
    value must be that object's own value (all blocking).  A concern row whose object the section
    never names is reported as minor, because wording may differ.
    """

    known: dict[str, set[float]] = {}
    for value, unit in known_values:
        known.setdefault(_UNIT_ALIASES.get(unit, unit), set()).add(float(value))
    findings: dict[str, dict[str, str]] = {}
    for section_id, text in sections.items():
        rows = rows_by_submodule.get(section_id, ())
        local = {unit: set(values) for unit, values in known.items()}
        # A count of this section's own rows is also a known count.
        counts = local.setdefault("个", set())
        for disposition in ("concern", "undetermined", "no_concern", "not_applicable"):
            counts.add(float(len({r.object for r in rows if r.disposition == disposition})))
        counts.add(float(len({r.object for r in rows})))
        unknown = sorted(
            f"{value}{unit}"
            for value, unit in numbers_with_units(text)
            if not _matches_known(value, unit, local)
        )
        mispaired = _mispaired(text, subject_values or {})
        if mispaired:
            unknown.extend(mispaired)
        missing = [
            row.id
            for row in rows_by_submodule.get(section_id, ())
            if row.disposition == "concern" and row.object not in text
        ]
        if unknown:
            findings[section_id] = _finding(
                "正文中的数值或数量对不上事实、程序统计或本节判断行：" + "、".join(unknown),
                "数值、数量和对象—数值配对改为事实或程序统计里的值，或删去没有依据的数值。",
            )
        elif missing:
            findings[section_id] = _finding(
                "以下关注项的对象没有在本节正文点名：" + "、".join(missing),
                "在正文里点名这些对象。",
                severity="minor",
            )
    return findings


__all__ = [
    "SpecialTopicAnalysis",
    "SectionText",
    "SectionSet",
    "JudgmentRevision",
    "ChiefChapter3",
    "ChiefChapter1",
    "DISPOSITION_LABELS",
    "EVIDENCE_STATUS_HELP",
    "EvidenceStatus",
    "PROBLEM_TABLE_MARKER",
    "ChiefAction",
    "DimensionHealth",
    "JudgmentAction",
    "JudgmentGap",
    "JudgmentRow",
    "JudgmentSet",
    "KeyRisk",
    "Phase",
    "action_table_markdown",
    "apply_revisions",
    "chief_findings",
    "findings_overview_markdown",
    "known_values_from_text",
    "ledger_findings",
    "ledger_judgments",
    "numbers_with_units",
    "place_problem_table",
    "problem_table",
    "report_check_findings",
]
