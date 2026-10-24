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

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .action_table import ACTION_PRIORITY_GROUPS, ACTION_TABLE_HEADERS
from .copied_text import COPIED_SUGGESTION, CopiedText, copied_problem
from .value_slots import (
    SLOT,
    empty_status_hints,
    fill_slots,
    mispaired_slots,
    quote_findings,
    slot_findings,
    strip_slots,
    value_hints,
)

Disposition = Literal["concern", "no_concern", "undetermined", "not_applicable"]
Priority = Literal["重要，紧急", "重要，不紧急", "一般"]
RiskLevel = Literal["高", "中", "低"]
EvidenceStatus = Literal["现场确认", "来源作者意见", "资料未提供", "照片待核", "推断"]
RelationStatus = Literal["成立", "待核实"]
PENDING_RELATION = "待核实"
EVIDENCE_STATUS_HELP = (
    "现场确认=表格或照片直接记录了该情况；来源作者意见=原表评语、既有评估等他人结论；"
    "资料未提供=缺资料，不能说明有或没有；照片待核=表格没有记录、只靠照片且照片看不清（表格有记录的按表格，不用照片待核）；"
    "推断=由其他事实推出，现场没有直接记录。"
)

DISPOSITION_LABELS = {
    "concern": "关注",
    "no_concern": "正常",
    "undetermined": "不能判定",
    "not_applicable": "不适用",
}
PROBLEM_TABLE_MARKER = "[[问题表]]"
# The problem table's "发现问题" cell is one short sentence for the reader.
FINDING_MAX = 60
PROBLEM_TABLE_HEADERS = ("对象", "检查内容", "发现问题", "风险等级", "建议措施", "现场图片")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RowPart(BaseModel):
    """A part of a judgment row: a field the contract does not have is dropped.

    A long judgment set is not sent back whole for one invented field (an
    ``action.decline_reason``); the field is dropped and logged.  Required
    fields are still required.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _log_dropped(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            dropped = sorted(set(value) - set(cls.model_fields))
            if dropped:
                logger.warning("{} dropped fields not in the contract: {}", cls.__name__, dropped)
        return value


class JudgmentAction(_RowPart):
    measure: str = Field(min_length=1)
    priority: Priority
    timeframe: str = Field(min_length=1)
    deadline_reason: str = ""


class JudgmentGap(_RowPart):
    missing: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    requested_input: str = Field(
        min_length=1,
        description="The step that closes the gap, written as an action, e.g. 补充该柜铭牌参数",
    )


class RowRelation(_RowPart):
    """Another module's row that bears on this row's conclusion, and why."""

    row_id: str = Field(pattern=r"^J-", description="别的模块的判断行编号（J-...）")
    relation: str = Field(
        min_length=1,
        description=(
            "关系名称：知识库关联方法列出的候选关系照抄 relation_names 里的名称；"
            "知识库没有收录的关联，自拟一个简短名称"
        ),
    )
    reason: str = Field(
        min_length=1,
        description="一句话：两边的哪项事实满足这条关系要核实的条件，对本行结论有什么作用",
    )
    mechanism: str = Field(
        default="",
        description="知识库没有收录的关联必填：两边为什么相关的机理（例如什么物理过程把两者连起来）",
    )
    condition: str = Field(
        default="",
        description="知识库没有收录的关联必填：这条关联成立要满足的条件，以及两边哪项事实满足了它",
    )
    status: RelationStatus = Field(
        default="成立",
        description=(
            "成立=两边的事实已经满足这条关系的条件；待核实=机理成立、但还有一项条件现有资料核实不了，"
            "在 missing 写明缺哪项、补什么能确认"
        ),
    )
    missing: str = Field(
        default="",
        description="待核实的关联必填：还缺哪项条件、补充什么资料或检测能确认；成立的关联不写",
    )

    @property
    def pending(self) -> bool:
        return self.status == PENDING_RELATION


class JudgmentRow(_RowPart):
    id: str = Field(pattern=r"^J-")
    submodule_id: str = Field(min_length=1)
    object: str = Field(min_length=1)
    checkpoint: str = Field(min_length=1)
    disposition: Disposition
    evidence_status: EvidenceStatus = Field(description=EVIDENCE_STATUS_HELP)
    evidence_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    basis: str = Field(min_length=1)
    finding: str = Field(
        default="",
        description=(
            "关注行必填：给报告读者看的一句话，写清这个对象有什么问题（现状，不写推理过程），"
            f"不超过 {FINDING_MAX} 字；不写编号、知识单元和“本行”“台账”这类内部说法。"
            "问题表印这一句，basis 只留给审查"
        ),
    )
    rule_refs: list[str] = Field(
        default_factory=list,
        description="本行所依据的知识单元 KU 编号，一项一个；涉及几个知识单元就写几个",
    )
    placement_reason: str = Field(
        default="",
        description=(
            "本行判断的事实在事实清单里默认属于别的小节，而你依据知识单元把它放在本小节判断时，"
            "写一句理由；默认小节就是本小节、或只是引用别处的事实作参考时留空"
        ),
    )
    risk_level: RiskLevel | None = None
    hazard: str = ""
    hazard_sources: list[str] = Field(
        default_factory=list,
        description=(
            "hazard 里前提的出处：本行所引的事实、计算、照片编号或知识单元 KU 编号；"
            "前提没有出处的后果写成“可能……，待核实”；从有出处的前提推出的后果是专业推理，不另要出处"
        ),
    )
    hazard_mechanism: str = Field(
        default="", description="后果怎样发生：从现状到后果经过什么过程，一句话"
    )
    hazard_conditions: list[str] = Field(
        default_factory=list,
        description=(
            "关注行必填：后果成立要满足的条件，一条一项；能由本行所引事实看出已满足的写明是哪项事实，"
            "看不出的写成待核实的条件"
        ),
    )
    hazard_checks: list[str] = Field(
        default_factory=list, description="要确认后果还需核实的事项，一条一项；没有就不写"
    )
    action: JudgmentAction | None = None
    gap: JudgmentGap | None = None
    photo_ids: list[str] = Field(default_factory=list)
    relations: list[RowRelation] = Field(
        default_factory=list,
        description="别的模块的判断行中，加重、解释或改变了本行结论的那些行，每条写明依据的候选关系和理由",
    )

    @property
    def related_row_ids(self) -> list[str]:
        return [relation.row_id for relation in self.relations]

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


class CandidateReview(_Model):
    """A candidate relation the author looked at and found not to hold here.

    The knowledge base's relations are references, not a list to satisfy:
    a relation that does not hold for this case is simply not linked.
    """

    relation: str = Field(min_length=1, description="考虑过、但在本次情况下不成立的关系名称")
    decision: Literal["not_linked"] = Field(
        default="not_linked", description="只记考虑过、不成立"
    )
    reason: str = Field(default="", description="可不写")


class DeclinedProposal(_Model):
    """A relation proposed for one of the author's rows, not taken."""

    row_id: str = Field(pattern=r"^J-", description="本模块的判断行编号")
    other_row_id: str = Field(pattern=r"^J-", description="提议关联的别的模块的行")
    reason: str = Field(min_length=1, description="一句话：为什么本次不成立")


class RelationSet(_Model):
    """One author's cross-module relations, by its own row id."""

    relations: dict[str, list[RowRelation]] = Field(
        default_factory=dict,
        description="键是本模块的判断行编号，值是这一行的关联；不连的行不写",
    )
    considered: list[CandidateReview] = Field(
        default_factory=list,
        description="可选：考虑过、但本次不成立的候选关系名称；不用逐条覆盖，也不用写理由",
    )
    declined_proposals: list[DeclinedProposal] = Field(
        default_factory=list,
        description="提议、但你判断本次不成立的关联，每条写一句理由；采纳的写进 relations",
    )


class LinkProposal(_Model):
    """One relation between rows of two modules, proposed by the cross-module review."""

    row_id: str = Field(pattern=r"^J-", description="一端的判断行编号")
    other_row_id: str = Field(pattern=r"^J-", description="另一端、别的模块的判断行编号")
    relation: str = Field(
        min_length=1,
        description="关系名称：知识库关联方法列出的照抄 relation_names；知识库没有收录的自拟简短名称",
    )
    reason: str = Field(
        min_length=1,
        description="一句话：两边哪项事实满足这条关系要核实的条件，对两边结论有什么作用",
    )
    mechanism: str = Field(default="", description="知识库没有收录的关联必填：两边为什么相关的机理")
    condition: str = Field(
        default="", description="知识库没有收录的关联必填：成立条件，以及两边哪项事实满足了它"
    )


class RelationFinding(_Model):
    """One problem with the relations recorded on a row."""

    problem: str = Field(min_length=1, description="哪条关联（写明 row_id）有什么问题，照原文写出两边的事实")
    severity: Literal["blocking", "minor"] = Field(
        description="blocking=关联不成立或作用说过头，要删；minor=成立，但行的依据没有体现或体现过头"
    )
    suggestion: str = Field(default="", description="怎么改：删掉哪条，或依据里补写或收回什么")


class CrossReview(_Model):
    """The cross-module review of one slice: the relations checked, and the ones missing."""

    findings: dict[str, RelationFinding] = Field(
        default_factory=dict,
        description="键是记下这条关联的行（for_row）；同一行几条关联有问题的写在一条里。没有问题时留空",
    )
    proposals: list[LinkProposal] = Field(
        default_factory=list,
        description=(
            "作者没有连、但两边的事实确实能看出条件的跨模块关联，至少一端在 focus_rows 里；"
            "交给两边作者确认。宁缺不凑，没有就留空"
        ),
    )


class StoryLine(_Model):
    """One cross-module line of evidence the report carries through its sections."""

    title: str = Field(min_length=1, description="这条主线说的是什么，一句话，给读者看")
    row_ids: list[str] = Field(
        min_length=2,
        description="链上的判断行（J-…），至少两行，来自至少两个模块；顺序就是论证顺序",
    )
    conclusion: str = Field(
        min_length=1,
        description="几行合起来得出的结论，措辞不强于链上最弱一行的证据状态",
    )
    section_notes: dict[str, str] = Field(
        default_factory=dict,
        description="可选：键是小节编号，值是这一节写这条主线时要交代的一句（例如引哪一端、标“详见 2.x.x”）",
    )


class StoryPlan(_Model):
    """The cross-module lines of evidence, settled before any prose is written."""

    lines: list[StoryLine] = Field(
        default_factory=list,
        description="只收已审定的关联和同一对象上的判断能支持的主线；没有就留空",
    )


class RowPatch(_Model):
    """The fields of one existing row that a revision changes, and nothing else."""

    row_id: str = Field(pattern=r"^J-", description="要改的本模块已有判断行编号")
    fields: dict[str, Any] = Field(
        min_length=1,
        description=(
            "只写要改的字段：键是判断行的字段名（如 disposition、risk_level、basis、finding、hazard、"
            "hazard_conditions、action、gap、evidence_ids、relations），值是改好后的完整字段值；"
            "action、gap 这类对象字段写改好后的整个对象。没改的字段不写，行编号不改"
        ),
    )


class JudgmentRevision(_Model):
    """Targeted revision of a module's rows in answer to review findings.

    An existing row is changed by a patch of the fields that change; only a
    new row is written in full.  The program applies the patches and checks
    the rows they produce.
    """

    patches: list[RowPatch] = Field(
        default_factory=list, description="要改的已有行：每行一条，只写改动的字段"
    )
    rows: list[JudgmentRow] = Field(
        default_factory=list, description="新增的行，每行写完整；已有的行用 patches 改，不在这里重交"
    )
    removed_row_ids: list[str] = Field(default_factory=list)
    responses: dict[str, str] = Field(
        default_factory=dict,
        description="Finding target to the reason it was not changed",
    )
    relations: dict[str, list[RowRelation]] = Field(
        default_factory=dict,
        description=(
            "采纳的跨模块审查提议：键是本模块的行编号，值是给这一行新加的关联（不重写这一行已有的关联）；"
            "没有提议或都不采纳时留空"
        ),
    )
    declined_proposals: list[DeclinedProposal] = Field(
        default_factory=list,
        description="不采纳的提议，每条写一句理由",
    )


# Fields a patch may not touch: the row's identity.
_FIXED_FIELDS = frozenset({"id"})


def apply_row_patches(
    rows: Sequence[JudgmentRow], revision: Mapping[str, Any]
) -> tuple[list[JudgmentRow], list[str], dict[str, str]]:
    """(the changed and new rows in full, removed ids, problems by row) for one revision.

    A patch on an existing row replaces the named fields and the row is
    validated again; a patch that names a field rows do not have, a row that
    does not exist, or that leaves the row invalid is a problem of that row,
    and the row stays as it was.  A full row for an existing id (an older
    revision's form) replaces it.
    """

    current = {row.id: row for row in rows}
    changed: dict[str, JudgmentRow] = {}
    problems: dict[str, str] = {}
    for patch in revision.get("patches") or []:
        row_id = str(patch.get("row_id", ""))
        fields = dict(patch.get("fields") or {})
        base = changed.get(row_id) or current.get(row_id)
        if base is None:
            problems[row_id] = f"要改的行 {row_id} 不存在；新增的行写进 rows"
            continue
        unknown = sorted(set(fields) - set(JudgmentRow.model_fields) - _FIXED_FIELDS)
        fixed = sorted(set(fields) & _FIXED_FIELDS)
        if unknown or fixed:
            problems[row_id] = "改动里有判断行没有或不能改的字段：" + "、".join([*unknown, *fixed])
            continue
        try:
            changed[row_id] = JudgmentRow.model_validate(
                {**base.model_dump(mode="json", exclude_none=True), **fields}
            )
        except ValueError as error:
            problems[row_id] = "按改动改完后这一行不成立：" + _validation_text(error)
    for value in revision.get("rows") or []:
        row = value if isinstance(value, JudgmentRow) else JudgmentRow.model_validate(value)
        changed[row.id] = row
    return list(changed.values()), list(revision.get("removed_row_ids") or []), problems


def _validation_text(error: ValueError) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        return "；".join(
            f"{'.'.join(str(part) for part in item.get('loc', ()))} {item.get('msg', '')}".strip()
            for item in errors()
        )
    return str(error)


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
    cited_values: Mapping[str, Iterable[Sequence[Any]]] | None = None,
    summary_evidence_ids: Iterable[str] = (),
    copied: CopiedText | None = None,
) -> dict[str, dict[str, str]]:
    """Return program findings keyed by row id or by the uncovered fact id.

    With ``copied``, a printed field (checkpoint, finding, hazard, measure,
    timeframe, gap) that repeats the instructions word for word is sent back.

    A concern or undetermined row covers one object: when its facts name
    several objects it must be split, so counts and groups stay per object.
    With ``cited_values`` (id of a fact, calculation, photo or knowledge unit
    -> its values with units), a measured value written in a row's
    checkpoint, basis or hazard must be one of the values of what the row
    cites; counts and ratios are left to the report check.
    A concern row names the sources of its hazard among what it cites, or
    marks the hazard as 待核实, and states the conditions under which it holds.
    (A row resting only on summary-sheet entries is set to 来源作者意见 by
    ``settle_evidence_status``; ``summary_evidence_ids`` is kept for callers.)
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
        # A number of the author's own (a voltage, a limit, a computed figure)
        # may be right: pointed out, not sent back.
        hints: list[str] = []
        if cited_values is not None and (stray := _uncited_values(row, cited_values)):
            hints.append(
                "判断行里的数值不在它所引的事实、计算、照片或知识单元里："
                + "、".join(stray)
                + "（推理得出或通用常识的数可以保留）"
            )
        problems.extend(_hazard_problems(row))
        if row.disposition == "concern" and not row.finding.strip():
            problems.append(f"关注行没有写 finding（问题表印给读者的一句话，不超过 {FINDING_MAX} 字）")
        elif len(row.finding) > FINDING_MAX + 20:
            problems.append(
                f"finding 有 {len(row.finding)} 字，问题表一格要短：写成不超过 {FINDING_MAX} 字的一句现状"
            )
        covered.update(row.evidence_ids)
        # basis is the reasoning the reviewers read; it is not printed.
        texts = [row.checkpoint, row.finding, row.hazard]
        if row.action is not None:
            texts += [row.action.measure, row.action.timeframe]
        if row.gap is not None:
            texts += [row.gap.missing, row.gap.impact, row.gap.requested_input]
        # These row texts go into the problem table and the prose as written.
        leaked = list(dict.fromkeys(term for text in texts for term in process_terms(text)))
        words = [term for term in leaked if term in PROCESS_TERMS]
        if words:
            problems.append(
                "判断行要印进报告的文字（checkpoint、finding、hazard、措施、缺口）里有内部流程用语或编号："
                + "、".join(leaked) + "；这些文字会原样进入正文和问题表"
            )
        if copied is not None and (spans := copied.spans("\n".join(texts))):
            problems.append(copied_problem(spans) + "（这些字段会原样印进报告）")
        if problems:
            findings[row.id] = _finding(
                "；".join([*problems, *hints]),
                "按事实清单改正编号或小节归属；多个对象的拆成每个对象一行；删去流程用语和编号；"
                "抄自资料的数值补引它的出处；与写作说明逐字相同的文字用本项目的话重写。判断本身不变。",
            )
        elif leaked or hints:
            # An id alone is easy to drop in writing; it does not hold up a revision.
            findings[row.id] = _finding(
                "；".join([
                    *(["判断行要印进报告的文字里有内部编号：" + "、".join(leaked) + "；这些文字会原样进入正文和问题表"]
                      if leaked else []),
                    *hints,
                ]),
                "删去内部编号；抄自资料的数值补引它的出处。判断本身不变。",
                severity="minor",
            )
        elif row.disposition in {"concern", "undetermined"} and not any(
            _UNIT_REF.search(ref) for ref in row.rule_refs
        ):
            findings[row.id] = _finding(
                "关注或不能判定的行没有在 rule_refs 写所用知识单元的 KU 编号，写作拿不到标准名",
                "在 rule_refs 写所用知识单元的 KU 编号；确实没有适用的知识单元就不写。",
                severity="minor",
            )
    for evidence_id in sorted(set(module_evidence_ids) - covered):
        findings[evidence_id] = _finding(
            f"本模块事实 {evidence_id} 没有进入任何判断行",
            "补一行判断（可以是正常、关注、不能判定或不适用），或并入已有的同一对象判断行。",
        )
    return findings


UNVERIFIED_MARK = "待核实"


def settle_evidence_status(
    rows: Sequence[JudgmentRow], summary_evidence_ids: Iterable[str]
) -> list[JudgmentRow]:
    """A row resting only on summary-sheet entries is another author's opinion.

    The summary sheet's rows are the source author's own assessment, a fixed
    property of the table: the program sets the status instead of asking the
    author to.  Only the stronger claim 现场确认 is lowered.
    """

    summaries = set(summary_evidence_ids)
    return [
        row.model_copy(update={"evidence_status": "来源作者意见"})
        if row.evidence_status == "现场确认"
        and row.evidence_ids
        and set(row.evidence_ids) <= summaries
        else row
        for row in rows
    ]


def _hazard_problems(row: JudgmentRow) -> list[str]:
    if row.disposition != "concern":
        return []
    cited = {
        *row.evidence_ids,
        *row.calculation_ids,
        *row.photo_ids,
        *(unit for ref in row.rule_refs for unit in _UNIT_REF.findall(ref)),
    }
    stray = [source for source in row.hazard_sources if source not in cited]
    problems = []
    if not any(condition.strip() for condition in row.hazard_conditions):
        problems.append("hazard 没有写成立条件（hazard_conditions）；写明后果要在什么条件下才会发生")
    if stray:
        problems.append("hazard 的出处不在本行所引的资料里：" + "、".join(stray) + "；出处先写进本行的引用")
    if not row.hazard_sources and UNVERIFIED_MARK not in row.hazard:
        problems.append(
            "hazard 没有写出处（hazard_sources）；有出处就填所引的事实、计算、照片或 KU 编号，"
            f"没有出处的后果写成“可能……，{UNVERIFIED_MARK}”"
        )
    return problems


# Counts and ratios are derived in writing and checked against the report.
_DERIVED_UNITS = {"个", "倍"}


def _uncited_values(
    row: JudgmentRow, cited_values: Mapping[str, Iterable[Sequence[Any]]]
) -> list[str]:
    refs = [
        *row.evidence_ids,
        *row.calculation_ids,
        *row.photo_ids,
        *(unit for ref in row.rule_refs for unit in _UNIT_REF.findall(ref)),
    ]
    allowed: dict[str, set[float]] = {}
    for ref in refs:
        for value, unit in cited_values.get(ref, ()):
            allowed.setdefault(_UNIT_ALIASES.get(unit, unit), set()).add(float(value))
    stray = [
        f"{value}{unit}"
        for text in (row.checkpoint, row.basis, row.hazard)
        for value, unit in sorted(numbers_with_units(text))
        if unit not in _DERIVED_UNITS and not _matches_known(value, unit, allowed)
    ]
    return list(dict.fromkeys(stray))


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


def relation_pairs(ledger: Mapping[str, Sequence[JudgmentRow]]) -> set[tuple[str, str]]:
    """Every recorded relation as (row holding it, row it points to)."""

    return {
        (row.id, relation.row_id)
        for rows in ledger.values() for row in rows for relation in row.relations
    }


def drop_mirrored_relations(
    before: Mapping[str, Sequence[JudgmentRow]],
    after: Mapping[str, Sequence[JudgmentRow]],
    judged_invalid: Iterable[str],
) -> tuple[dict[str, list[JudgmentRow]], list[dict[str, str]]]:
    """A relation found not to hold at both ends goes from both ends.

    ``judged_invalid`` are the rows whose relations a reviewer judged not to
    hold (a blocking finding on the relations).  When A and B are both such
    rows and A's revision drops its relation to B, B's relation back to it is
    dropped as well, and each removal is listed.  When only A was found
    wanting, B keeps its relation: it was not challenged.
    """

    invalid = set(judged_invalid)
    # Only when the reviewer judged both ends' relations not to hold: one end
    # dropping a relation the other end still holds unchallenged is that
    # author's call on its own row, not a finding against the other row.
    given_up = {
        (a, b)
        for a, b in relation_pairs(before) - relation_pairs(after)
        if a in invalid and b in invalid
    }
    gone = {(b, a) for a, b in given_up}
    removed: list[dict[str, str]] = []
    result: dict[str, list[JudgmentRow]] = {}
    for module_id, rows in after.items():
        kept_rows = []
        for row in rows:
            dropped = [r for r in row.relations if (row.id, r.row_id) in gone]
            if dropped:
                removed.extend(
                    {"row_id": row.id, "other_row_id": r.row_id, "relation": r.relation}
                    for r in dropped
                )
                row = row.model_copy(
                    update={"relations": [r for r in row.relations if r not in dropped]}
                )
            kept_rows.append(row)
        result[module_id] = kept_rows
    return result, removed


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
    """The problem table of one submodule, or None when nothing is flagged.

    Objects with the same checkpoint, evidence status, finding, risk level and
    measure share one row.  Objects that could not be judged are not listed
    here: each one is in the data gap table already, so the table only says
    how many there are.
    """

    concerns = [row for row in rows if row.disposition == "concern"]
    unknown = list(dict.fromkeys(row.object for row in rows if row.disposition == "undetermined"))
    note = (
        f"另有 {len(unknown)} 个对象资料不足，不能判定，见数据缺口表。" if unknown else ""
    )
    if not concerns:
        return note or None
    groups: dict[tuple[str, ...], dict[str, list[str]]] = {}
    # A finding not confirmed on site is said once, under the table.
    unconfirmed: dict[str, list[str]] = {}
    for row in concerns:
        problem = row_finding(row)
        if row.evidence_status != "现场确认":
            objects = unconfirmed.setdefault(row.evidence_status, [])
            if row.object not in objects:
                objects.append(row.object)
        key = (
            row.checkpoint,
            row.evidence_status,
            problem,
            row.risk_level or "",
            row.action.measure if row.action else "",
        )
        group = groups.setdefault(key, {"objects": [], "photos": []})
        if row.object not in group["objects"]:
            group["objects"].append(row.object)
        group["photos"].extend(p for p in row.photo_ids if p not in group["photos"])
    has_photos = any(group["photos"] for group in groups.values())
    headers = PROBLEM_TABLE_HEADERS if has_photos else PROBLEM_TABLE_HEADERS[:-1]
    body = []
    for (checkpoint, _status, problem, level, measure), group in groups.items():
        values = ["、".join(group["objects"]), checkpoint, problem, level, measure]
        if has_photos:
            values.append("、".join(group["photos"]))
        body.append(values)
    table = _table(headers, body)
    if unconfirmed:
        status_note = "；".join(
            f"{'、'.join(objects)}的证据状态为{status}" for status, objects in unconfirmed.items()
        ) + "。"
        note = status_note + note
    return table + ("\n\n" + note if note else "")


def row_finding(row: JudgmentRow) -> str:
    """What the problem table says of a concern row: its finding for the reader.

    A row written before ``finding`` existed falls back to its basis, with
    internal ids removed.
    """

    if row.finding.strip():
        return row.finding.strip()
    text = _PROCESS_ID.sub("", row.basis)
    text = re.sub(r"(按|依据|根据)\s*[，,；;]", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


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
    problem: str = Field(min_length=1, description="发现问题，写明对象；合并几条行动时覆盖全部对象")
    hazard: str = Field(min_length=1, description="风险与危害")
    solution: str = Field(
        min_length=1,
        description="解决方案：对象上的动作，建议时限写在动作里（如“建议〈时限〉内…”），有依据时末尾写“依据：…”",
    )
    note: str = Field(default="", description="备注；模块之间对同一对象结论不同时，在这里说明以负责模块为准")
    row_ids: list[str] = Field(min_length=1)
    group_id: str = ""
    interim: str = ""

    @model_validator(mode="before")
    @classmethod
    def _drop_content(cls, value: Any) -> Any:
        # 评估内容 is filled by the program from the cited rows; an older run
        # or a model that still writes it is not an error.
        if isinstance(value, dict) and "content" in value:
            value = {k: v for k, v in value.items() if k != "content"}
        return value


class DimensionHealth(_Model):
    module_id: str = Field(min_length=1)
    status: str = Field(min_length=1, description="健康状态，一句话，例如〈主要问题〉，〈后果〉风险〈高低〉")
    key_indicators: str = Field(
        min_length=1, description="关键异常指标，写明所在小节，例如〈对象〉〈异常指标〉；见〈小节〉"
    )
    risk_level: Literal["高", "中", "低", "待定"]


class KeyRisk(_Model):
    description: str = Field(min_length=1, description="风险描述，例如〈问题〉→〈后果〉")
    hazard: str = Field(min_length=1, description="危害")
    action: str = Field(min_length=1, description="行动建议，与第三章的行动一致")
    row_ids: list[str] = Field(min_length=1)


class Phase(_Model):
    phase: str = Field(min_length=1, description="阶段名称")
    time: str = Field(min_length=1, description="时间范围；第三章没有时限的行动归入“时限待定”")
    tasks: str = Field(min_length=1, description="核心任务：第三章中建议时限落在这一范围的行动")
    effect: str = Field(min_length=1, description="预期效果")
    row_ids: list[str] = Field(
        default_factory=list,
        description="这一阶段的任务所依据的判断行（J-…）；程序核对这些行的时限落在本阶段的时间里",
    )


class CrossFinding(_Model):
    subject: str = Field(min_length=1, description="对象，用对象证据链里的写法")
    conclusion: str = Field(
        min_length=1,
        description="跨模块综合结论，一句、一行：几个模块的判断合起来说明什么（相互印证、可排除的原因、"
        "共同指向的根因或处置先后）；程序把它作为 3.1 风险汇总的一条写进去",
    )
    row_ids: list[str] = Field(min_length=2, description="结论依据的判断行，至少来自两个模块")


class ActionText(_Model):
    """The chief's words for one action group the program formed."""

    group_id: str = Field(pattern=r"^G-", description="action_groups 里的组编号")
    problem: str = Field(min_length=1, description="发现问题：写明对象，覆盖本组全部对象（可用对象清单占位符）")
    hazard: str = Field(min_length=1, description="风险与危害：写成“若〈条件〉，可能〈后果〉”，条件和后果取自本组判断行")
    solution: str = Field(
        min_length=1,
        description="解决方案：对象上的动作，建议时限写在动作里（如“建议〈时限〉内…”），有依据时末尾写“依据：…”",
    )
    note: str = Field(default="", description="备注；模块之间对同一对象结论不同时，在这里说明以负责模块为准")
    interim: str = Field(
        default="",
        description=(
            "本组的判断行时限不止一种时必填：几项措施的先后关系，以及最晚的一项完成前，"
            "期间（例如下一次作业）按什么做；组内时限只有一种时留空"
        ),
    )


class DimensionPoints(_Model):
    module_id: str = Field(min_length=1)
    points: list[str] = Field(min_length=1, description="本维度的要点，每条一句、一行，能定位到第二章小节")


class ChiefChapter3(_Model):
    risk_summary: list[str] = Field(
        default_factory=list,
        description="3.1“风险汇总如下”：全报告的主要风险，每条一句、一行，条数按实际；"
        "跨对象综合结论由程序从 cross_findings 加进来，不在这里重写",
    )
    risk_details: list[DimensionPoints] = Field(
        default_factory=list,
        description="3.1“详述见下”：按评估维度各列几条短要点，每条一句、一行",
    )
    cross_findings: list[CrossFinding] = Field(
        default_factory=list,
        description="对象证据链得出的跨模块综合结论；能合起来说明问题的对象各写一条，不凑数",
    )
    action_lead: str = Field(min_length=1, description="3.2 表前导语，一句话")
    actions: list[ActionText] = Field(
        description="3.2 速查表：action_groups 里每组写一条，组由程序按优先级和检查项分好、排好"
    )


class ChiefChapter1(_Model):
    assessment_background: str = Field(
        min_length=1,
        description="1.1 正文：委托方、评估对象与地点、现场工作时间、评估方法以及客户提出的专项话题；"
        "不写证据边界、资料清单或阅读说明",
    )
    overall_assessment: str = Field(
        min_length=1, description="1.2 表前的总体评价：一句定性结论，再用几句展开主要短板"
    )
    dimension_health: list[DimensionHealth] = Field(min_length=1, description="1.2 健康度表，每个评估维度一行")
    key_risks: list[KeyRisk] = Field(
        default_factory=list, description="1.2 重点风险表，条数按实际，不凑数；按提交顺序编号"
    )
    phases: list[Phase] = Field(
        default_factory=list, description="1.2 阶段表，由第三章行动的建议时限归并得出"
    )
    regional_executive_summary: str = Field(
        min_length=1,
        description="1.3 正文：项目分区域时，每个区域写供电架构、运行现状、最大风险、核心结论和五个维度的发现；"
        "不分区域时写全站摘要",
    )


class SpecialTopicAnalysis(_Model):
    special_topic_analysis: str = Field(min_length=1)


class SectionText(_Model):
    submodule_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


DECLINED_HELP = (
    "不采纳的意见：键是意见所在的小节或条目编号，值是一句理由（对照所引材料说明意见为什么不成立）；"
    "全部采纳时留空"
)


class SectionSet(_Model):
    sections: dict[str, str] = Field(description="Section id to its complete revised text")
    declined: dict[str, str] = Field(default_factory=dict, description=DECLINED_HELP)


class ChiefFix(_Model):
    """The chief's one answer to the fact check of chapters 1 and 3.

    Only what changes is submitted, each piece under the place the finding
    names: a paragraph or a 3.1 line by its unit id, a table row of chapter 1
    by its id, and the 3.2 groups that change.  The program puts each back in
    place; the rest of the chapters stays as it was.
    """

    units: dict[str, str] = Field(
        default_factory=dict,
        description="要改的段落和 3.1 条目：键是位置编号（1.1-段n、1.2-段n、1.3-段n、3.1-汇总-n、"
        "3.1-详述-模块-n），值是改好后的这一段或这一条全文；不改的不提交",
    )
    health: list[DimensionHealth] = Field(
        default_factory=list, description="1.2 健康度表要改的行：每个模块一条，写改好后的整行（module_id 照抄）；不改的不提交"
    )
    key_risks: dict[str, KeyRisk] = Field(
        default_factory=dict, description="1.2 重点风险表要改的条：键是位置编号（1.2-重点风险-n），值是改好后的整条；不改的不提交"
    )
    phases: dict[str, Phase] = Field(
        default_factory=dict, description="1.2 阶段表要改的条：键是位置编号（1.2-阶段-n），值是改好后的整条；不改的不提交"
    )
    cross_findings: dict[str, CrossFinding] = Field(
        default_factory=dict, description="3.1 跨对象综合结论要改的条：键是位置编号（3.1-综合-n），值是改好后的整条；不改的不提交"
    )
    actions: list[ActionText] = Field(
        default_factory=list,
        description="3.2 速查表要改的行动组：每组一条，写改好后的这一组（group_id 照抄）；不改的组不提交",
    )
    action_lead: str = Field(default="", description="3.2 表前导语要改时写改好后的一句；不改留空")
    declined: dict[str, str] = Field(default_factory=dict, description=DECLINED_HELP)


def _status_note(row_ids: Sequence[str], row_status: Mapping[str, str] | None) -> str:
    statuses = [
        status
        for status in dict.fromkeys((row_status or {}).get(row_id) for row_id in row_ids)
        if status and status != "现场确认"
    ]
    return "依据：" + "、".join(statuses) if statuses else ""


def action_content(row_ids: Sequence[str], row_places: Mapping[str, Sequence[str]]) -> str:
    """The 评估内容 column: where the cited rows sit, by title and without numbers.

    ``row_places`` maps a row id to its place, e.g. (dimension title,
    section title, checkpoint).  Rows of one section share one entry; the
    checkpoint is kept only when all of them check the same thing.
    """

    sections: dict[tuple[str, ...], list[str]] = {}
    for row_id in row_ids:
        place = tuple(row_places.get(row_id, ()))
        if len(place) < 2:
            continue
        checks = sections.setdefault(place[:2], [])
        if len(place) > 2 and place[2] not in checks:
            checks.append(place[2])
    return "；".join(
        "＞".join([*section, *checks]) if len(checks) == 1 else "＞".join(section)
        for section, checks in sections.items()
    )


def action_table_markdown(
    lead: str,
    actions: Sequence[ChiefAction],
    row_status: Mapping[str, str] | None = None,
    row_places: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """3.2: a lead sentence, then one six-column table per priority group, the group id first.

    The 评估内容 column is filled by the program from the cited rows' places;
    the note column carries the evidence status of the cited rows when any
    of them is not confirmed on site.
    """

    blocks = [lead.strip()]
    for group in ACTION_PRIORITY_GROUPS:
        members = [action for action in actions if action.priority == group]
        if not members:
            continue
        notes = [_status_note(a.row_ids, row_status) for a in members]
        # A note every row of the group shares is stated once, by the group.
        shared = notes[0] if len(members) > 1 and notes[0] and len(set(notes)) == 1 else ""
        blocks.append(f"**{group} · 以下各项{shared}**" if shared else f"**{group}**")
        blocks.append(
            _table(
                ACTION_TABLE_HEADERS,
                (
                    [
                        a.group_id,
                        action_content(a.row_ids, row_places or {}),
                        a.problem,
                        a.hazard,
                        a.solution,
                        "；".join(
                            text
                            for text in (
                                f"过渡安排：{a.interim.strip()}" if a.interim.strip() else "",
                                a.note,
                                "" if shared else note,
                            )
                            if text
                        ),
                    ]
                    for a, note in zip(members, notes, strict=True)
                ),
            )
        )
    return "\n\n".join(block for block in blocks if block)


_RISK_ORDER = {"高": 0, "中": 1, "低": 2}


def action_groups(
    rows_by_module: Mapping[str, Sequence[JudgmentRow]],
    checkpoint_of: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """3.2's rows, formed and ordered by the program.

    Rows with an action are grouped by priority and checkpoint.  The
    checkpoint is the declared column (or summary-sheet row) of the row's
    facts, given in ``checkpoint_of`` (fact id -> checkpoint); a row whose
    facts declare none keeps its own checkpoint text.  Groups follow the
    priority order, then the highest risk level, then first appearance.
    """

    declared = checkpoint_of or {}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order = 0
    for rows in rows_by_module.values():
        for row in rows:
            if row.action is None:
                continue
            fields = sorted({declared[e] for e in row.evidence_ids if declared.get(e)})
            checkpoint = "、".join(fields) if fields else row.checkpoint
            group = groups.setdefault(
                (row.action.priority, checkpoint),
                {
                    "priority": row.action.priority,
                    "checkpoint": checkpoint,
                    "row_ids": [],
                    "objects": [],
                    "risk_levels": [],
                    "measures": [],
                    "timeframes": [],
                    "first": order,
                },
            )
            order += 1
            group["row_ids"].append(row.id)
            for key, value in (
                ("objects", row.object),
                ("risk_levels", row.risk_level),
                ("measures", row.action.measure),
                ("timeframes", row.action.timeframe),
            ):
                if value and value not in group[key]:
                    group[key].append(value)
    ordered = sorted(
        groups.values(),
        key=lambda g: (
            ACTION_PRIORITY_GROUPS.index(g["priority"]),
            min((_RISK_ORDER.get(level, 3) for level in g["risk_levels"]), default=3),
            g["first"],
        ),
    )
    return [
        {"group_id": f"G-{index:02d}", **{k: v for k, v in group.items() if k != "first"}}
        for index, group in enumerate(ordered, 1)
    ]


def grouped_actions(
    groups: Sequence[Mapping[str, Any]], texts: Sequence[ActionText]
) -> list[ChiefAction]:
    """Join the program's groups with the chief's words, in the program's order.

    A group the chief left unwritten keeps its rows' own words, so no action
    row drops out of 3.2.
    """

    by_group = {text.group_id: text for text in texts}
    actions = []
    for group in groups:
        text = by_group.get(group["group_id"]) or ActionText(
            group_id=group["group_id"],
            problem=f"{'、'.join(group['objects'])}：{group['checkpoint']}",
            hazard="见第二章相应小节",
            solution="；".join(
                f"{measure}（{'、'.join(group['timeframes'])}）" if group["timeframes"] else measure
                for measure in group["measures"]
            ),
        )
        actions.append(
            ChiefAction(
                priority=group["priority"],
                problem=text.problem,
                hazard=text.hazard,
                solution=text.solution,
                note=text.note,
                row_ids=list(group["row_ids"]),
                group_id=group["group_id"],
                interim=text.interim,
            )
        )
    return actions


def risk_overview_markdown(
    chapter3: ChiefChapter3,
    module_titles: Mapping[str, str],
    conflicts: Sequence[str] = (),
) -> str:
    """3.1 in the expert version's two layers, one line per item.

    The summary layer holds the chief's risks, then each cross-object
    conclusion and each unresolved conflict as its own line; the detail layer
    lists each dimension's points.
    """

    summary = [
        *(line.strip() for line in chapter3.risk_summary if line.strip()),
        # A cross-object conclusion names its object: the line stands alone.
        *(f"{f.subject.strip()}：{f.conclusion.strip()}" for f in chapter3.cross_findings),
        *(line.strip() for line in conflicts if line.strip()),
    ]
    blocks = []
    if summary:
        blocks.append("风险汇总如下：")
        blocks.append("\n".join(f"{index}. {line}" for index, line in enumerate(summary, 1)))
    details = [d for d in chapter3.risk_details if d.points]
    if details:
        blocks.append("详述见下：")
        for dimension in details:
            blocks.append(f"**{module_titles.get(dimension.module_id, dimension.module_id)}**")
            blocks.append("\n".join(f"- {point.strip()}" for point in dimension.points))
    return "\n\n".join(blocks)


_NUMERALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_N = r"\d+|[一二两三四五六七八九十]+"
_TERM = re.compile(rf"({_N})\s*(?:(?:至|到|-|－|～|~)\s*({_N})\s*)?(个月|周|天|日)")


def _number(text: str) -> int:
    if text.isdigit():
        return int(text)
    if text.startswith("十"):
        return 10 + _NUMERALS.get(text[1:], 0)
    if "十" in text:
        tens, _, ones = text.partition("十")
        return _NUMERALS.get(tens, 1) * 10 + _NUMERALS.get(ones, 0)
    return _NUMERALS.get(text, 0)


def time_terms(text: str) -> list[tuple[int, str]]:
    """The deadlines a text names, in order: "1至3个月内" -> (1, 个月), (3, 个月)."""

    terms: list[tuple[int, str]] = []
    for match in _TERM.finditer(str(text or "")):
        unit = "天" if match.group(3) == "日" else match.group(3)
        for number in (match.group(1), match.group(2)):
            if number and (term := (_number(number), unit)) not in terms:
                terms.append(term)
    return terms


def _term_text(term: tuple[int, str]) -> str:
    return f"{term[0]}{term[1]}"


def deadline_findings(
    groups: Sequence[Mapping[str, Any]], texts: Sequence[ActionText]
) -> dict[str, dict[str, str]]:
    """A group's words name only the deadlines its rows give.

    The chief may order the measures and say what is done meanwhile, but a
    deadline none of the group's rows gives moves a row's own deadline.
    """

    by_group = {text.group_id: text for text in texts}
    problems = []
    for group in groups:
        text = by_group.get(group["group_id"])
        allowed = {t for timeframe in group.get("timeframes") or () for t in time_terms(timeframe)}
        if text is None or not allowed:
            continue
        if extra := [t for t in time_terms(f"{text.solution} {text.interim}") if t not in allowed]:
            problems.append(
                f"{group['group_id']} 写了时限 {'、'.join(map(_term_text, extra))}，"
                f"本组判断行的时限只有 {'、'.join(group.get('timeframes') or ())}"
            )
    if not problems:
        return {}
    return {
        "3.2": _finding(
            "；".join(problems),
            "每项措施的时限照它所在判断行的时限写，不合并成区间、不另定期限；先后关系写在 interim。",
        )
    }


def phase_findings(
    phases: Sequence[Phase], rows_by_module: Mapping[str, Sequence[JudgmentRow]]
) -> dict[str, dict[str, str]]:
    """Each phase's rows are due within the phase's time, as their own deadlines say."""

    timeframe = {
        row.id: row.action.timeframe
        for rows in rows_by_module.values()
        for row in rows
        if row.action is not None
    }
    problems = []
    for number, phase in enumerate(phases, 1):
        allowed = time_terms(phase.time)
        if not allowed:
            continue
        for row_id in phase.row_ids:
            own = time_terms(timeframe.get(row_id, ""))
            if own and own[0] not in allowed:
                problems.append(
                    f"1.2-阶段-{number}（{phase.time}）列入了 {row_id}，该行时限为“{timeframe[row_id]}”"
                )
    if not problems:
        return {}
    return {
        "1.2": _finding(
            "；".join(problems),
            "阶段按判断行的时限归并：把这些任务移到与其时限相同的阶段，或改写阶段时间。",
        )
    }


def action_group_findings(
    groups: Sequence[Mapping[str, Any]], texts: Sequence[ActionText]
) -> dict[str, dict[str, str]]:
    """Every program group gets the chief's words once; no other group is named."""

    known = [group["group_id"] for group in groups]
    written = [text.group_id for text in texts]
    problems = []
    if missing := [g for g in known if g not in written]:
        problems.append("以下行动组没有写：" + "、".join(missing))
    if unknown := sorted(set(written) - set(known)):
        problems.append("以下组编号不存在：" + "、".join(unknown))
    if repeated := sorted({g for g in written if written.count(g) > 1}):
        problems.append("以下组写了不止一次：" + "、".join(repeated))
    by_group = {text.group_id: text for text in texts}
    # Rows of one group due at different times: until the last is done, how
    # the work in between is done has to be said once, by the group.
    if no_interim := [
        group["group_id"]
        for group in groups
        if len(group.get("timeframes") or ()) > 1
        and group["group_id"] in by_group
        and not by_group[group["group_id"]].interim.strip()
    ]:
        problems.append(
            "以下行动组里各行的时限不止一种，没有写 interim（先后关系和完成前的过渡安排）："
            + "、".join(no_interim)
        )
    if not problems:
        return {}
    return {
        "3.2": _finding(
            "；".join(problems),
            "action_groups 里每组写一条，组编号照抄；组内时限不止一种的，在 interim 写先后关系和完成前的过渡安排。",
        )
    }


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


def cross_finding_findings(
    findings: Sequence[CrossFinding],
    *,
    rows_by_module: Mapping[str, Sequence[JudgmentRow]],
    overview: str,
    section: str = "3.1",
    own_row_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Each cross-module conclusion cites existing rows of two or more modules and is in the text.

    In a chapter 2 section (``own_row_ids`` given) it also cites a row of that section.
    """

    own = set(own_row_ids) if own_row_ids is not None else None

    module_of = {row.id: module_id for module_id, rows in rows_by_module.items() for row in rows}
    problems = []
    for finding in findings:
        unknown = [row_id for row_id in finding.row_ids if row_id not in module_of]
        if unknown:
            problems.append(f"{finding.subject}：引用了不存在的判断行 {'、'.join(unknown)}")
            continue
        if len({module_of[row_id] for row_id in finding.row_ids}) < 2:
            problems.append(f"{finding.subject}：所引判断行只来自一个模块，不是跨模块结论")
        if own is not None and not own & set(finding.row_ids):
            problems.append(f"{finding.subject}：没有引用本小节的判断行")
        if " ".join(finding.conclusion.split()) not in " ".join(overview.split()):
            problems.append(f"{finding.subject}：综合结论没有原样写进正文")
    if not problems:
        return {}
    return {
        section: _finding(
            "；".join(problems),
            "跨模块结论只引用台账里的行，至少来自两个模块，并把 conclusion 原样写进正文",
        )
    }


def chief_findings(
    *,
    actions: Sequence[ChiefAction],
    health: Sequence[DimensionHealth],
    risks: Sequence[KeyRisk],
    rows_by_module: Mapping[str, Sequence[JudgmentRow]],
    texts: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Every action row is merged into 3.2 once; every dimension has health.

    Text fields hold prose only: the tables come from the structured fields.
    """

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
    for field, text in (texts or {}).items():
        if field in GENERATED_TABLE_FIELDS and has_markdown_table(text):
            findings[field] = _finding(
                "文字字段里画了表格；本章的表格由程序按结构化字段生成",
                "删去文字里的表格，只留文字；表里的内容放进对应的结构化字段。",
            )
    return findings


# Text fields placed right before tables the program builds from structured
# rows; a table written in them would be a second, unchecked copy.
GENERATED_TABLE_FIELDS = frozenset({"overall_assessment", "action_lead"})


def has_markdown_table(text: str) -> bool:
    """Two consecutive lines the renderer would draw as table rows."""

    rows = [
        line.strip().startswith("|") and line.count("|") >= 2
        for line in text.splitlines()
    ]
    return any(a and b for a, b in zip(rows, rows[1:]))


_NUMBER_WITH_UNIT = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*"
    r"(kVA|kW|kV|V|A|K|℃|°C|%RH|%|Ω|MΩ|mA|Hz|倍|×|个|处|台|面|组|只)(?![A-Za-z月小天年周])"
)
_UNIT_ALIASES = {
    "°C": "℃", "%RH": "%", "×": "倍",
    "处": "个", "台": "个", "面": "个", "组": "个", "只": "个",
}


def numbers_with_units(text: str) -> set[tuple[str, str]]:
    """Numbers written with a unit, as written (so the precision is kept).

    Counts written with 个/处/台/面/组/只 share the unit 个; × is 倍; %RH is %.
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


# Words of this product's own workflow that a reader of the report cannot know.
# Ordinary trade words with another meaning (设备台账, 返修) are not listed.
PROCESS_TERMS = (
    "联合审查", "联合卷宗", "卷宗", "判断台账", "判断行", "审查人", "程序统计", "知识单元",
    "另一模块", "其他模块", "别的模块",
)
# Ids of facts, knowledge, sources, photos, rows and dossiers; section numbers (2.1.1) are fine.
_PROCESS_ID = re.compile(r"(?<![A-Za-z0-9])(?:KU-\d[\d.]*-\d+|[EKRPDJ]-[\d.\-]*\d)")


_UNIT_REF = re.compile(r"KU-\d[\d.]*-\d+")


def process_terms(text: str) -> list[str]:
    """Internal workflow words or ids that leaked into report prose."""

    found = [term for term in PROCESS_TERMS if term in text]
    found = [term for term in found if not any(term != other and term in other for other in found)]
    return [*found, *dict.fromkeys(_PROCESS_ID.findall(text))]


def slot_check(
    text: str, slots: Mapping[str, Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    """(problems that send the text back, hints for the editor) for one text.

    Sent back: slots the program cannot fill and one object's value slot next
    to another object alone.  Hints: values that look like another object's
    and a status list with nothing in it.  If a check itself fails on
    unexpected input, it becomes a hint and the rest carries on.
    """

    problems: list[str] = []
    hints: list[str] = []
    for target, check in (
        (problems, slot_findings),
        (problems, mispaired_slots),
        (hints, value_hints),
        (hints, empty_status_hints),
    ):
        try:
            target.extend(check(text, slots))
        except Exception as error:  # noqa: BLE001 - a check must not stop the run
            hints.append(f"程序核对未完成（{check.__name__}：{type(error).__name__}），请人工核对数值")
    return problems, hints


SLOT_SUGGESTION = (
    "占位符只用 value_slots 里有的；列举几个对象的值时用〔V-…:对象〕或〔S-…〕，"
    "让程序把对象和数值一起写。"
)
HINT_SUGGESTION = "核对这些数是否写对；写对了不用改。"


def report_check_findings(
    *,
    sections: Mapping[str, str],
    rows_by_submodule: Mapping[str, Sequence[JudgmentRow]],
    slots: Mapping[str, Mapping[str, Any]],
    quote_sources: Mapping[str, Iterable[str]] | None = None,
    copied: CopiedText | None = None,
    hints_out: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, str]]:
    """Program check of the written sections against the slots and the ledger.

    Numbers are free to write; slots are recommended.  Blocking: a slot the
    program cannot fill, one object's value slot next to another object
    alone, text in 「」 that is not the source author's recorded words, and
    text copied word for word from the instructions (``copied``).
    Hints (values that may be another object's, a concern row whose object
    the section never names) are not certain: with ``hints_out`` they are
    collected there instead of reaching the editor.
    """

    findings: dict[str, dict[str, str]] = {}
    for section_id, text in sections.items():
        problems, hints = slot_check(text, slots)
        if quoted := quote_findings(text, (quote_sources or {}).get(section_id, ())):
            problems.append("「」里的话不是来源记录的原话：" + "、".join(quoted))
        if copied is not None and (spans := copied.spans(text)):
            problems.append(copied_problem(spans))
        filled = fill_slots(text, slots)
        missing = [
            row.id
            for row in rows_by_submodule.get(section_id, ())
            if row.disposition == "concern" and row.object not in filled
        ]
        if missing:
            hints.append("以下关注项的对象没有在本节正文点名：" + "、".join(missing))
        if leaked := process_terms(strip_slots(text)):
            problems.append("正文里出现了内部流程用语或编号：" + "、".join(leaked))
        if hints_out is not None:
            if hints:
                hints_out[section_id] = list(hints)
            hints = []
        if problems:
            findings[section_id] = _finding(
                "；".join(problems) + ("；另请核对：" + "；".join(hints) if hints else ""),
                SLOT_SUGGESTION + "「」里只放来源记录的原话；删去内部流程用语；"
                + COPIED_SUGGESTION,
            )
        elif hints:
            findings[section_id] = _finding(
                "请核对：" + "；".join(hints), HINT_SUGGESTION, severity="minor"
            )
    return findings


_TABLE_POINTER = re.compile(r"(?:见|如|参见|详见)下表|下表(?:所示|列出|汇总)")


def layout_findings(
    *,
    sections: Mapping[str, str],
    rows_by_submodule: Mapping[str, Sequence[JudgmentRow]],
    elsewhere: Mapping[str, Sequence[str]],
    slots: Mapping[str, Mapping[str, Any]],
    row_sections: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    """Minor findings on where the prose points and what it repeats.

    With ``row_sections`` (row id -> its subsection), each relation a row
    records must show in the prose as a reference to the related row's
    subsection, so a cross-module link is not lost in writing.

    The prose points to a table below although the section has no problem
    table; or a row whose data another section presents (``elsewhere``) has
    two or more of its facts' value slots written again in this section, where
    one key value with the section reference is enough.
    """

    findings: dict[str, dict[str, str]] = {}
    for section_id, text in sections.items():
        rows = rows_by_submodule.get(section_id, ())
        problems: list[str] = []
        # Only concern rows make a table; the rest is one sentence at most.
        if _TABLE_POINTER.search(text) and not any(row.disposition == "concern" for row in rows):
            problems.append("正文写了“见下表”一类的话，但本节没有问题表")
        written = list(dict.fromkeys(m.group(1) for m in SLOT.finditer(text)))
        for row in rows:
            if row.id not in elsewhere:
                continue
            own = set(row.evidence_ids)
            repeated = [
                f"〔{slot_id}〕"
                for slot_id in written
                if slot_id.startswith("V-")
                and own & set((slots.get(slot_id) or {}).get("evidence_ids") or ())
            ]
            if len(repeated) >= 2:
                problems.append(
                    f"{row.object} 的数据由 " + "、".join(elsewhere[row.id])
                    + " 呈现，本节又写了 " + "、".join(repeated)
                )
        missing_links = [
            f"{row.object}↔{(row_sections or {})[relation.row_id]}"
            for row in rows
            for relation in row.relations
            if row_sections and relation.row_id in row_sections
            and row_sections[relation.row_id] not in text
        ]
        if missing_links:
            problems.append(
                "判断行记下的跨模块关联没有写进正文（找不到所关联的小节号）："
                + "、".join(dict.fromkeys(missing_links))
            )
        if problems:
            findings[section_id] = _finding(
                "；".join(problems),
                "没有问题表就删去指向表格的话；数据在别的小节呈现的，只留一个关键值并标“（数据见 2.x.x）”；"
                "记下的关联在风险分析里写出来并标“（详见 2.x.x）”。",
                severity="minor",
            )
    return findings


__all__ = [
    "drop_mirrored_relations",
    "relation_pairs",
    "DeclinedProposal",
    "LinkProposal",
    "CrossReview",
    "RelationFinding",
    "StoryLine",
    "StoryPlan",
    "RelationSet",
    "UNVERIFIED_MARK",
    "action_content",
    "layout_findings",
    "SpecialTopicAnalysis",
    "SectionText",
    "SectionSet",
    "ChiefFix",
    "DECLINED_HELP",
    "JudgmentRevision",
    "PENDING_RELATION",
    "RowPatch",
    "apply_row_patches",
    "ActionText",
    "ChiefChapter3",
    "DimensionPoints",
    "action_group_findings",
    "action_groups",
    "deadline_findings",
    "phase_findings",
    "time_terms",
    "grouped_actions",
    "risk_overview_markdown",
    "settle_evidence_status",
    "CrossFinding",
    "cross_finding_findings",
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
    "RowRelation",
    "JudgmentSet",
    "KeyRisk",
    "Phase",
    "action_table_markdown",
    "apply_revisions",
    "chief_findings",
    "findings_overview_markdown",
    "has_markdown_table",
    "known_values_from_text",
    "ledger_findings",
    "ledger_judgments",
    "numbers_with_units",
    "place_problem_table",
    "problem_table",
    "report_check_findings",
    "slot_check",
    "HINT_SUGGESTION",
    "SLOT_SUGGESTION",
]
