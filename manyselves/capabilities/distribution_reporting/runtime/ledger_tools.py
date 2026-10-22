"""Deterministic Tools of the judgment-ledger report flow.

The workflow files decide the order: shared context, judgment, review and
targeted revision, writing, chief summary, checks and delivery.  Each Tool
here does one deterministic step on ordinary files of the current Run:

- ``prepare_context`` assembles the shared project context every conversation
  starts from, and one brief per module;
- ``build_ledger`` checks the judgment rows and builds the joint dossiers;
- ``merge_findings`` combines program and reviewer findings for one owner;
- ``finalize_ledger`` applies targeted revisions and plans the writing;
- ``check_chief`` and ``check_report`` are the program checks of the chief's
  structured tables and of the written sections;
- ``compose_report`` assembles the module submissions and the edited report
  and runs the existing render and delivery.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from ..domain.calculations import compute, render_calculations, render_statistics, statistics
from ..domain.judgment_ledger import (
    PROBLEM_TABLE_MARKER,
    ActionText,
    ChiefChapter3,
    CrossFinding,
    DimensionHealth,
    JudgmentRow,
    JudgmentSet,
    KeyRisk,
    Phase,
    action_group_findings,
    action_groups,
    action_table_markdown,
    apply_revisions,
    chief_findings,
    cross_finding_findings,
    findings_overview_markdown,
    grouped_actions,
    known_values_from_text,
    layout_findings,
    ledger_findings,
    ledger_judgments,
    place_problem_table,
    problem_table,
    process_terms,
    HINT_SUGGESTION,
    SLOT_SUGGESTION,
    report_check_findings,
    risk_overview_markdown,
    settle_evidence_status,
    slot_check,
)
from ..domain.taxonomy import (
    REPORT_TAXONOMY,
)
from ..domain.value_slots import (
    SLOT,
    UNRESOLVED,
    fill_slots,
    format_value,
    object_names,
    slot_lines,
    slot_table,
    slots_for_evidence,
    strip_slots,
)
from .assets import runtime_photo_ids
from .cross_module import (
    checked_relations,
    unlisted_relations,
    conflict_summary,
    flagged_rows,
    object_chains,
    relation_names,
    related_rows,
    see_also_by_row,
)
from .delivery_tools import DeliveryTools
from .joint_dossiers import build_dossiers
from .models.agentic import (
    EditedReportSubmission,
    ModuleSubmission,
    ObservationAssessment,
    PartAssessment,
)
from .models.reporting import EvidenceItem, PhotoAsset, ReportRequest, SpecialTopicPlan
from .module_authoring_preparation import load_template_skill
from .observation_ledger import build_observation_ledger, shared_labels
from .models.agentic import PhotoReadingSubmission
from .models.photo_reading import PhotoReadingInput
from .photo_reading import (
    load_photo_readings,
    photo_reading_labels,
    photo_readings_ref,
    photo_records,
    reading_label,
    readings_agree,
    unread_agree,
    unread_labels,
)
from .research.fact_context import register_fact_sources, render_fact_packet
from .research.knowledge_context import KnowledgeContextBuilder
from .research.project_evidence import ProjectEvidenceIndex
from .review_packets import ReviewPackets
from .rule_standards import (
    knowledge_units,
    section_standards,
    standards_coverage,
    unit_bodies,
    unit_ids,
)
from .storage import ReportingStore
from .template_lengths import matched_lengths, render_length_reference, template_sections
from .template_resolver import resolve_report_template
from .template_skill_paths import is_template_skill_path
from .run_taxonomy import run_taxonomy
from .template_skills import plain_value, skill_order, skills_for_run, skills_with_lessons

_ROLES_FILE = Path(__file__).resolve().parents[1] / "domain/module-roles.yaml"
CHIEF_OWNER = "chief"
# Chief sections the edit pass reads as text; 3.1 is drawn from its two
# structured layers first.
CHIEF_SECTIONS = {
    "1.1": "assessment_background",
    "1.2": "overall_assessment",
    "1.3": "regional_executive_summary",
    "3.1": "risk_overview",
}
CHIEF_SECTION_TITLES = {
    "1.1": "评估背景",
    "1.2": "待提升问题与建议概览",
    "1.3": "各区域执行摘要",
    "3.1": "风险/问题汇总与概览",
    "3.2": "改善行动列表与优先级",
}
REPORT_TITLE = "配电安全专家咨询报告"


@contextmanager
def _taxonomy(state: Mapping[str, Any]) -> Iterator[None]:
    """Bind the run's tree; without one, reading the tree fails (no default)."""

    with run_taxonomy(state):
        yield


def _plain(value: Any) -> Any:
    """Typed Agent submissions arrive as models; the Tools read plain JSON."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def photos_to_check(
    rows: list[JudgmentRow], reference: Mapping[str, Any], readings: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Original images for the rows whose photo reading is in doubt.

    A photo is sent when the program found its reading differs from the
    record, or two independent readings of it differ.  A reader's own wish
    for a second look is what the re-read answers; sending every such photo
    put two dozen images into one review.
    """

    evidence_photos = reference.get("evidence_photos") or {}
    assets = reference.get("photos") or {}
    by_photo: dict[str, list[str]] = {}
    for row in rows:
        refs = [*row.photo_ids, *(p for e in row.evidence_ids for p in evidence_photos.get(e, []))]
        for photo_id in dict.fromkeys(refs):
            by_photo.setdefault(photo_id, []).append(row.id)
    photos = []
    for photo_id, row_ids in by_photo.items():
        reading = readings.get(photo_id)
        asset = assets.get(photo_id)
        if asset is None or reading is None:
            continue
        if reading.record_match != "conflicts" and reading.reread_agrees is not False:
            continue
        photos.append(
            {
                "photo_id": photo_id,
                "path": asset["path"],
                "media_type": asset["media_type"],
                "row_ids": row_ids,
                "reading": reading_label(reading),
            }
        )
    return photos


def column_headers(state: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, str]]:
    """(source path, sheet) -> column -> header text, from the preparation results."""

    headers: dict[tuple[str, str], dict[str, str]] = {}
    for result in state.get("preparation_worker_results") or []:
        value = _plain(result)
        path = Path(str(value.get("source_path", ""))).as_posix()
        for sheet, columns in (value.get("column_headers") or {}).items():
            headers[(path, sheet)] = dict(columns)
    return headers


def _combined(
    program: Mapping[str, Any] | None, reviewed: Mapping[str, Any] | None
) -> dict[str, Any]:
    if program is None or reviewed is None:
        return dict(program or reviewed or {})
    severity = (
        "blocking"
        if "blocking" in (program.get("severity"), reviewed.get("severity"))
        else "minor"
    )
    return {
        "problem": f"程序核对：{program.get('problem', '')}；审阅意见：{reviewed.get('problem', '')}",
        "severity": severity,
        "suggestion": "；".join(
            text
            for text in (program.get("suggestion"), reviewed.get("suggestion"))
            if text
        ),
    }


def _filled(value: Any, slots: Mapping[str, Mapping[str, Any]]) -> Any:
    if isinstance(value, str):
        return fill_slots(value, slots, unresolved=UNRESOLVED)
    if isinstance(value, Mapping):
        return {key: _filled(item, slots) for key, item in value.items()}
    if isinstance(value, list):
        return [_filled(item, slots) for item in value]
    return value


def _section_ids() -> list[str]:
    """Every section number the report has: the run's chapter 2 tree and the chief's."""

    return [
        *CHIEF_SECTION_TITLES,
        *REPORT_TAXONOMY,
        *(sub_id for module in REPORT_TAXONOMY.values() for sub_id in module.submodules),
    ]


def _strings(value: Any) -> list[str]:
    """Every text a structured chief output holds, ids aside."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [
            text
            for key, item in value.items()
            # Ids, fixed labels, object names and timeframes (taken from the rows).
            if key not in {
                "row_ids", "group_id", "module_id", "priority", "risk_level", "subject", "time",
            }
            for text in _strings(item)
        ]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _blocking(findings: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(finding.get("severity") == "blocking" for finding in findings.values())


def _foreign_row(target: str, module_id: str) -> bool:
    """A judgment row id (J-<module>-n) that belongs to another module."""

    return target.startswith("J-") and not target.startswith(f"J-{module_id}-")


def _rows(values: Sequence[Any]) -> list[JudgmentRow]:
    return [JudgmentRow.model_validate(value) for value in values]


def _dump_rows(rows: Sequence[JudgmentRow]) -> list[dict[str, Any]]:
    return [row.model_dump(mode="json", exclude_none=True) for row in rows]


def _ledger_text(ledger: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    lines = [
        "# 判断台账",
        "每个模块的判断行，每行一个 JSON 对象。审查、汇总和写作都以这张台账为准。",
    ]
    for module_id, rows in ledger.items():
        lines.append(f"\n## 模块 {module_id}")
        lines.extend(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
    return "\n".join(lines) + "\n"


def _supplement_lines(request: ReportRequest) -> list[str]:
    superseded = {
        old for supplement in request.user_supplements for old in supplement.supersedes
    }
    return [
        "用户补充（"
        + ("、".join(supplement.target_ids) if supplement.target_ids else "全报告")
        + f"）：{supplement.content}"
        for supplement in request.user_supplements
        if supplement.id not in superseded
    ]


def _with_layout(
    findings: dict[str, dict[str, Any]], layout: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    merged = dict(findings)
    for section_id, finding in layout.items():
        earlier = merged.get(section_id)
        if earlier is None:
            merged[section_id] = dict(finding)
            continue
        merged[section_id] = {
            **earlier,
            "problem": f"{earlier.get('problem', '')}；{finding['problem']}",
            "suggestion": f"{earlier.get('suggestion', '')}；{finding['suggestion']}",
        }
    return merged


def _with_table_terms(
    findings: dict[str, dict[str, Any]],
    sections: Mapping[str, str],
    rows_by_submodule: Mapping[str, Sequence[JudgmentRow]],
) -> dict[str, dict[str, Any]]:
    """Workflow words in the problem table the program draws from the rows.

    The table is built from row text, so the editor cannot change it: the
    finding names the rows to report it, and stays minor.
    """

    merged = dict(findings)
    for section_id in sections:
        rows = rows_by_submodule.get(section_id, ())
        leaked = {
            row.id: terms
            for row in rows
            # Photo ids in the photo column become images when rendered.
            if (terms := process_terms(problem_table([row.model_copy(update={"photo_ids": []})]) or ""))
        }
        if leaked:
            merged[section_id] = _combined(
                merged.get(section_id),
                {
                    "problem": "问题表里有内部流程用语或编号（来自判断行文字）："
                    + "；".join(f"{row_id} {'、'.join(terms)}" for row_id, terms in leaked.items()),
                    "severity": "minor",
                    "suggestion": "问题表由程序从判断行生成，正文不用改；记录下来供判断阶段改正。",
                },
            )
    return merged


def placement_findings(
    ledger: Mapping[str, Sequence[JudgmentRow]],
    unassigned_ids: Sequence[str],
    defaults: Mapping[str, str],
    unit_sections: Mapping[str, Sequence[str]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    """Program checks on where the authors placed records, and the departures.

    The knowledge base's index gives a record its default section; it is a
    default, not a rule.  An author may judge a record in another section, or
    in several, when the knowledge units say so: the row then gives a reason
    and cites a unit that serves its section.  A record with no default
    (unassigned) must be judged by some module, by a row that cites the units
    it applied.  Departures are returned for the knowledge base's maintenance.
    """

    unassigned = set(unassigned_ids)
    found: dict[str, dict[str, dict[str, Any]]] = {module_id: {} for module_id in ledger}
    departures: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for module_id, rows in ledger.items():
        for row in rows:
            placed = sorted(unassigned & set(row.evidence_ids))
            moved = [
                e for e in row.evidence_ids
                if defaults.get(e) and defaults[e] != row.submodule_id
            ] if row.placement_reason.strip() else []
            if not placed and not moved:
                continue
            claimed.update(placed)
            units = [u for ref in row.rule_refs for u in unit_ids(ref)]
            sections = sorted({s for u in units for s in unit_sections.get(u, ())})
            if moved:
                departures.append({
                    "row_id": row.id,
                    "evidence_ids": moved,
                    "default_sections": sorted({defaults[e] for e in moved}),
                    "chosen_section": row.submodule_id,
                    "reason": row.placement_reason,
                    "rule_refs": list(row.rule_refs),
                })
            if not units:
                problem = "本行把事实放在默认以外的小节或判断了待归属的事实，但没有写所依据的知识单元"
            elif sections and row.submodule_id not in sections:
                problem = (
                    f"本行所依据的知识单元对应 {'、'.join(sections)}，本行却在 {row.submodule_id}"
                )
            else:
                continue
            found[module_id][row.id] = {
                "problem": problem,
                "severity": "minor",
                "suggestion": "在 rule_refs 写覆盖本小节的知识单元；没有这样的知识单元时放回默认小节，或说明理由。",
            }
    # Keyed by the fact, like an uncovered fact: a new row citing it answers.
    for evidence_id in (e for e in unassigned_ids if e not in claimed):
        for module_id in found:
            found[module_id][evidence_id] = {
                "problem": "这条待归属的事实还没有任何模块判断",
                "severity": "blocking",
                "suggestion": (
                    "属于本模块某个小节的，新增判断行并写所依据的知识单元；"
                    "不属于本模块的，在 responses 里说明。"
                ),
            }
    return found, departures


def _section_state(rows: Sequence[JudgmentRow]) -> str:
    """How much a section has to say, counted by the program."""

    dispositions = {row.disposition for row in rows}
    if not rows:
        return "no_rows"
    if dispositions == {"no_concern"}:
        return "no_concern"
    if dispositions == {"undetermined"}:
        return "undetermined_only"
    return "mixed"


def _section_brief(
    sub: Mapping[str, Any],
    rows: Sequence[JudgmentRow],
    *,
    ledger: Mapping[str, Sequence[JudgmentRow]],
    units: Mapping[str, Mapping[str, str]],
    elsewhere: Mapping[str, Sequence[str]],
    names: Sequence[str] = (),
    slots: Mapping[str, Mapping[str, Any]] | None = None,
    fact_texts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    related = related_rows(rows, ledger, names)
    by_id = {other.id: other for others in ledger.values() for other in others}
    cited = {e for row in rows for e in row.evidence_ids} | {
        e
        for row in rows
        for relation in row.relations
        if relation.row_id in by_id
        for e in by_id[relation.row_id].evidence_ids
    }
    slot_ids = [
        *(c for row in rows for c in row.calculation_ids),
        *slots_for_evidence(cited, slots or {}),
    ]
    return {
        "submodule_id": sub["id"],
        "title": sub["title"],
        "section_state": _section_state(rows),
        "rows": [row.model_dump(mode="json", exclude_none=True) for row in rows],
        "problem_table_marker": PROBLEM_TABLE_MARKER,
        # Only concern rows make a problem table.
        "has_problem_table": any(row.disposition == "concern" for row in rows),
        "related_rows": related,
        "rule_standards": section_standards(rows, units),
        "data_elsewhere": {row.id: list(elsewhere[row.id]) for row in rows if row.id in elsewhere},
        # What each value slot holds; the prose names the slot, the program writes the value.
        "value_slots": slot_lines(slot_ids, slots or {}),
        # The recorded words of rows that rest on another author's opinion.
        "source_quotes": {
            row.id: [(fact_texts or {}).get(e, "") for e in row.evidence_ids if (fact_texts or {}).get(e)]
            for row in rows
            if row.evidence_status == "来源作者意见"
        },
    }


class LedgerTools:
    """Tool implementations bound to one project workspace."""

    def __init__(self, workspace: Path, *, global_knowledge_root: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)
        self.global_knowledge_root = global_knowledge_root

    # -- shared context ---------------------------------------------------

    def prepare_context(self, state: dict[str, Any]) -> dict[str, Any]:
        run_id = str(state["run_id"])
        request = ReportRequest.model_validate(state["request"])
        with _taxonomy(state):
            module_ids = [m for m in request.target_modules if m in REPORT_TAXONOMY]
            load_template_skill(self.workspace, state)
            index = ProjectEvidenceIndex(self.workspace, run_id)
            items = index.items()
            register_fact_sources(self.workspace, run_id, items)
            shared = shared_labels(self.workspace, run_id)
            readings = photo_reading_labels(self.workspace, run_id)
            headers = column_headers(state)
            knowledge = KnowledgeContextBuilder(
                self.workspace, run_id, global_root=self.global_knowledge_root
            )
            calculations, calculation_gaps = compute(items)
            counted, statistic_lines, statistic_lists = statistics(
                items, start=len(calculations) + 1
            )
            roles = yaml.safe_load(_ROLES_FILE.read_text(encoding="utf-8"))
            fact_blocks: list[str] = []
            knowledge_blocks: list[str] = []
            modules: list[dict[str, Any]] = []
            for module_id in module_ids:
                module = REPORT_TAXONOMY[module_id]
                # Unassigned records are shown once, below, not in every module.
                module_items = [
                    item for item in index.module_items(module_id) if item.module_id == module_id
                ]
                fact_blocks.append(
                    render_fact_packet(
                        module_items,
                        module_id,
                        shared=shared,
                        photo_readings=readings,
                        column_headers=headers,
                    )
                )
                knowledge_blocks.append(knowledge.build_module(module_id).text)
                modules.append(
                    {
                        "module_id": module_id,
                        "title": module.title,
                        "role": roles[module_id],
                        "submodules": [
                            {"id": sub.id, "title": sub.title}
                            for sub in module.submodules.values()
                        ],
                        "evidence_ids": [
                            item.id for item in module_items if item.module_id == module_id
                        ],
                    }
                )
            unassigned = [
                item for item in items
                if item.module_id is None and not is_template_skill_path(item.source.path)
            ]
            if unassigned:
                fact_blocks.append(
                    "# 待归属的事实\n"
                    "这些记录的检查点没有固定小节：知识库的字段索引没有给它们指定知识单元"
                    "（或写明要按实际检查内容匹配），或者是原表的“其他”栏、不在固定清单里的行。"
                    "内容属于你这个模块某个小节的，就在那个小节判断它，并在 rule_refs 写所依据的知识单元；"
                    "不属于本模块的不用管。每条都要有模块判断，没人判断的会退回。\n\n"
                    + render_fact_packet(
                        unassigned,
                        "未归属",
                        shared=shared,
                        photo_readings=readings,
                        column_headers=headers,
                    )
                )
            skills = skills_with_lessons(
                state.get("template_skill_text") or {},
                items=items,
                mapping_gaps=[plain_value(gap) for gap in state.get("mapping_gaps") or []],
                calculation_gaps=calculation_gaps,
            )
            # Skills are matched to this run's modules and sections by meaning;
            # a section the table does not list is left out of its Skill.
            skills, module_skills, skill_notes = skills_for_run(skills)
            self.store.write_json(
                f"Work/runs/{run_id}/ledger/skill-alignment.json", {"changes": skill_notes}
            )
            plan = state.get("special_topic_plan")
            plan_value = (
                SpecialTopicPlan.model_validate(plan).model_dump(mode="json")
                if plan
                else None
            )
            structure = "\n".join(
                f"{module.id} {module.title}："
                + "；".join(f"{sub.id} {sub.title}" for sub in module.submodules.values())
                for module in (REPORT_TAXONOMY[m] for m in module_ids)
            )
            requirements = [
                f"委托要求：{request.instruction}",
                f"缺少资料时的处理：{request.missing_evidence_policy}",
                *request.execution_requirements,
                *_supplement_lines(request),
            ]
            project = "\n\n".join(
                [
                    "# 委托与报告结构\n" + "\n".join(requirements) + "\n\n## 第二章固定小节\n" + structure,
                    *fact_blocks,
                    render_calculations(calculations, calculation_gaps),
                    render_statistics(statistic_lines),
                    self._length_reference(run_id, module_ids),
                    *knowledge_blocks,
                    *(
                        ["# 判断条件（知识库）\n"
                         "知识库对各类记录能否比较、怎样判断的适用条件。知识单元的限值、结论标准、依据状态和这里的条件"
                         "是判断的参考，通常准确有效；结论按本项目的实际情况分析，适用才照它判断。"
                         "专家版方法里的判断方法只作思路参考，与这里冲突时以这里为准。\n\n"
                         + conditions]
                        if (conditions := knowledge.judgment_conditions())
                        else []
                    ),
                    "# 专家版方法（按小节，每节分判断方法、写法、章节综合、范例、核对要点）\n"
                    + "\n\n".join(
                        f"## {skill_id}\n{skills[skill_id]}"
                        for skill_id in skill_order()
                        if skills.get(skill_id)
                    ),
                ]
            )
        self.store.write_text(f"Work/runs/{run_id}/ledger/shared-context.md", project)
        transcribed = [
            str(entry.get("text", ""))
            for reading in load_photo_readings(self.workspace, run_id).values()
            for entry in reading.transcription
            if entry.get("legible")
        ]
        known_values = known_values_from_text(
            [item.fact for item in items] + knowledge_blocks + transcribed
        )
        for item in items:
            if item.value is not None and item.unit:
                known_values.append((float(item.value), item.unit))
            for measurement in item.measurements:
                if measurement.value is not None:
                    known_values.append((float(measurement.value), measurement.unit))
        calculations = [*calculations, *counted]
        known_values.extend((calc.value, calc.unit) for calc in calculations)
        slots = slot_table(items, calculations, statistic_lists)
        subject_values: dict[str, list[tuple[float, str]]] = {}
        for item in items:
            pairs = subject_values.setdefault(item.subject, [])
            if isinstance(item.value, (int, float)) and item.unit:
                pairs.append((float(item.value), item.unit))
            pairs.extend(
                (float(m.value), m.unit) for m in item.measurements if m.value is not None
            )
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/reference-values.json",
            {
                "evidence_ids": [item.id for item in items],
                "photo_ids": [
                    asset["id"] if isinstance(asset, Mapping) else asset.id
                    for asset in state.get("photo_assets", [])
                ],
                "calculation_ids": [calc.id for calc in calculations],
                "photos": {
                    asset["id"]: {
                        "path": asset["path"],
                        "media_type": asset["media_type"],
                        "sheet": asset.get("source_sheet"),
                        "cell": asset.get("source_cell"),
                    }
                    for asset in (_plain(a) for a in state.get("photo_assets", []))
                },
                "evidence_subjects": {
                    item.id: item.subject for item in items if not item.summary_entry
                },
                "evidence_photos": {item.id: item.photo_refs for item in items if item.photo_refs},
                "known_values": sorted({(round(v, 6), u) for v, u in known_values}),
                "subject_values": {
                    subject: sorted(set(pairs)) for subject, pairs in subject_values.items() if pairs
                },
                "cited_values": self._cited_values(run_id, items, calculations),
                # What a review packet shows next to each row.
                "summary_evidence_ids": [item.id for item in items if item.summary_entry],
                "column_headers": [
                    [path, sheet, dict(columns)] for (path, sheet), columns in headers.items()
                ],
                "calculation_lines": {
                    calc.id: render_calculations([calc], []).splitlines()[3]
                    for calc in calculations
                },
                "author_skills": {
                    module_id: module_skills.get(module_id, "") for module_id in module_ids
                },
                "unassigned_evidence_ids": [item.id for item in unassigned],
                # The section each record goes to by default.
                "default_sections": {
                    item.id: item.submodule_id for item in items if item.submodule_id
                },
                # Values the program writes into the prose, by slot.
                "slots": slots,
                "object_names": object_names(item.subject for item in items),
                # The checkpoint a record's column (or summary-sheet row) declares.
                "checkpoint_of": {
                    item.id: (item.subject if item.summary_entry else item.checkpoint_field)
                    for item in items
                    if item.summary_entry or item.checkpoint_field
                },
                "fact_texts": {item.id: item.fact for item in items},
                "request_texts": requirements,
            },
        )
        return {
            "run_id": run_id,
            "shared": {"project": project},
            "modules": modules,
            "module_titles": {module["module_id"]: module["title"] for module in modules},
            "special_topic_plan": plan_value,
            "special_topic_input": {"plan": plan_value},
            "has_special_topic": plan_value is not None,
            "chief_key": CHIEF_OWNER,
        }

    def _cited_values(
        self, run_id: str, items: Sequence[Any], calculations: Sequence[Any]
    ) -> dict[str, list[tuple[float, str]]]:
        """Values with units each citable id carries, for checking a row's numbers.

        A fact carries its text, value, measurements and the readings of its
        photos; a calculation its value and formula; a photo its reading; a
        knowledge unit its own text.
        """

        readings = {
            photo_id: reading_label(reading)
            for photo_id, reading in load_photo_readings(self.workspace, run_id).items()
        }
        values: dict[str, set[tuple[float, str]]] = {}
        for item in items:
            texts = [item.fact, *(readings.get(ref, "") for ref in item.photo_refs)]
            pairs = set(known_values_from_text(texts))
            if isinstance(item.value, (int, float)) and item.unit:
                pairs.add((float(item.value), item.unit))
            pairs.update(
                (float(m.value), m.unit) for m in item.measurements if m.value is not None
            )
            values[item.id] = pairs
        for calc in calculations:
            values[calc.id] = {(float(calc.value), calc.unit), *known_values_from_text([calc.formula])}
        for photo_id, text in readings.items():
            values[photo_id] = set(known_values_from_text([text]))
        for unit_id, (_title, body) in self._unit_bodies(run_id).items():
            values[unit_id] = set(known_values_from_text([body]))
        return {key: sorted(pairs) for key, pairs in values.items() if pairs}

    def _unit_bodies(self, run_id: str) -> dict[str, tuple[str, str]]:
        try:
            documents = KnowledgeContextBuilder(
                self.workspace, run_id, global_root=self.global_knowledge_root
            ).freeze_sources()
        except (OSError, ValueError):
            return {}
        return unit_bodies(document.text for document in documents)

    def _unit_sections(self, run_id: str) -> dict[str, list[str]]:
        """Knowledge unit -> the sections it serves, as routed for this run."""

        path = self.workspace / f"Work/runs/{run_id}/knowledge-routing.json"
        if not path.is_file():
            return {}
        return dict(json.loads(path.read_text(encoding="utf-8")).get("unit_sections") or {})

    def _length_reference(self, run_id: str, module_ids: Sequence[str]) -> str:
        template, _source = resolve_report_template(self.workspace, run_id)
        titles = dict(CHIEF_SECTION_TITLES)
        for module_id in module_ids:
            for sub in REPORT_TAXONOMY[module_id].submodules.values():
                titles[sub.id] = sub.title
        lengths = matched_lengths(template_sections(template), titles.items())
        return render_length_reference(lengths, titles)

    def _reference(self, run_id: str) -> dict[str, Any]:
        path = self.workspace / f"Work/runs/{run_id}/ledger/reference-values.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    # -- ledger -------------------------------------------------------------

    def build_ledger(self, value: dict[str, Any]) -> dict[str, Any]:
        context = value["context"]
        run_id = context["run_id"]
        reference = self._reference(run_id)
        ledger: dict[str, list[JudgmentRow]] = {}
        findings: dict[str, dict[str, Any]] = {}
        for module in context["modules"]:
            module_id = module["module_id"]
            judgment = JudgmentSet.model_validate(value["judgments"][module_id])
            judgment = judgment.model_copy(update={"rows": settle_evidence_status(
                judgment.rows, reference.get("summary_evidence_ids") or ()
            )})
            ledger[module_id] = list(judgment.rows)
            findings[module_id] = ledger_findings(
                judgment,
                submodule_ids=[sub["id"] for sub in module["submodules"]],
                module_evidence_ids=module["evidence_ids"],
                known_evidence_ids=reference["evidence_ids"],
                known_photo_ids=reference["photo_ids"],
                known_calculation_ids=reference["calculation_ids"],
                evidence_subjects=reference.get("evidence_subjects"),
                cited_values=reference.get("cited_values"),
                summary_evidence_ids=reference.get("summary_evidence_ids") or (),
            )
        placed, _departures = placement_findings(
            ledger,
            reference.get("unassigned_evidence_ids") or (),
            reference.get("default_sections") or {},
            self._unit_sections(run_id),
        )
        for module_id, module_findings in placed.items():
            for target, finding in module_findings.items():
                findings[module_id][target] = _combined(findings[module_id].get(target), finding)
        dossiers = build_dossiers(self.workspace, run_id, ledger_judgments(ledger))
        see_also = see_also_by_row(dossiers, ledger)
        # Shared by every author's revision; each sees the other modules' lines.
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/relation-candidates.json",
            {"flagged": flagged_rows(ledger), "method": self._relation_method(run_id)},
        )
        readings = load_photo_readings(self.workspace, run_id)
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments-r0.json", dumped)
        del readings
        return {
            "run_id": run_id,
            "ledger": dumped,
            "snapshot": {"ledger_text": _ledger_text(dumped)},
            "program_findings": findings,
            "dossiers": dossiers,
            # Each author decides its cross-module relations before review.
            "relate_items": [
                {
                    "run_id": run_id,
                    "module_id": module["module_id"],
                    "related": {
                        row.id: others
                        for row in ledger[module["module_id"]]
                        if (others := see_also.get(row.id))
                    },
                    **self._relation_candidates(run_id, module["module_id"]),
                }
                for module in context["modules"]
            ],
            "review_items": [],
        }

    def apply_relations(self, value: dict[str, Any]) -> dict[str, Any]:
        """Put each author's relations on its rows, then build the review packets.

        Only an author's own existing rows take relations; the program keeps
        relations to existing rows of other modules under a listed candidate
        relation and records what it dropped.
        """

        context = value["context"]
        built = value["ledger"]
        run_id = context["run_id"]
        ledger = {module_id: _rows(rows) for module_id, rows in built["ledger"].items()}
        for module_id, chosen in (value.get("relations") or {}).items():
            by_row = dict((_plain(chosen) or {}).get("relations") or {})
            ledger[module_id] = [
                row.model_copy(update={"relations": by_row[row.id]})
                if row.id in by_row
                else row
                for row in ledger.get(module_id, [])
            ]
        ledger = {
            module_id: [
                JudgmentRow.model_validate(row.model_dump(mode="json")) for row in rows
            ]
            for module_id, rows in ledger.items()
        }
        names = relation_names(self._relation_method(run_id))
        ledger, dropped = checked_relations(ledger, names)
        # Candidates an author looked at and found not to hold.  The method's
        # relations are references: nothing requires every one to be decided.
        considered_not_valid = {
            module_id: [
                str(entry.get("relation", "")).strip()
                for entry in (_plain(chosen) or {}).get("considered") or []
                if str(entry.get("relation", "")).strip()
            ]
            for module_id, chosen in (value.get("relations") or {}).items()
        }
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments-r1.json", dumped)
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/relations-r1.json",
            {
                "dropped_relations": dropped,
                "considered_not_valid": considered_not_valid,
                "unlisted_relations": unlisted_relations(ledger, names),
            },
        )
        reference = self._reference(run_id)
        readings = load_photo_readings(self.workspace, run_id)
        elsewhere = self._data_elsewhere(run_id, ledger)
        packets = ReviewPackets(self, run_id, ledger, reference, elsewhere)
        return {
            "run_id": run_id,
            "ledger": dumped,
            "snapshot": {"ledger_text": _ledger_text(dumped)},
            "review_items": [
                {
                    "run_id": run_id,
                    "module_id": module["module_id"],
                    "title": module["title"],
                    "program_findings": built["program_findings"][module["module_id"]],
                    "dossiers": [
                        dossier
                        for dossier in built["dossiers"]
                        if dossier["kind"] == "conflict"
                        and module["module_id"] in dossier["module_ids"]
                    ],
                    "photos": photos_to_check(
                        ledger[module["module_id"]], reference, readings
                    ),
                    **packets.packet(module["module_id"]),
                }
                for module in context["modules"]
            ],
        }

    # -- photo re-read ------------------------------------------------------

    def plan_reread(self, value: dict[str, Any]) -> dict[str, Any]:
        """The photos a problem or a gap rests on, to be read once more, independently.

        Only photos the rows cite themselves, and only where a second reading
        can settle something: the first reading read a declared quantity (the
        numbers are compared), or left items blank or illegible (those items
        are read again on their own).  The reader gets no recorded values and
        not what the first reading said, only the date it found, so that a
        dated record's blanks after that date are not taken for missed ones.
        """

        state = value["ledger"]
        run_id = state["run_id"]
        reference = self._reference(run_id)
        assets = reference.get("photos") or {}
        headers = {
            (str(path), str(sheet)): dict(columns)
            for path, sheet, columns in reference.get("column_headers") or []
        }
        readings = load_photo_readings(self.workspace, run_id)
        ledger = {m: _rows(rows) for m, rows in state["ledger"].items()}
        cited = list(dict.fromkeys(
            photo_id
            for rows in ledger.values()
            for row in rows
            if row.disposition in {"concern", "undetermined"}
            for photo_id in row.photo_ids
        ))
        index = ProjectEvidenceIndex(self.workspace, run_id)
        items = index.items() if index.path.is_file() else []
        by_id = {item.id: item for item in items}
        planned = []
        for photo_id in cited:
            reading = readings.get(photo_id)
            asset = assets.get(photo_id)
            if reading is None or asset is None:
                continue
            labels = unread_labels(reading.transcription)
            if not reading.readings and not labels:
                continue
            bound = [by_id[e] for e in reading.evidence_ids if e in by_id]
            if not bound:
                continue
            records, quantities = photo_records(bound, items)
            sheet, cell = asset.get("sheet"), asset.get("cell")
            column = re.sub(r"\d+", "", cell or "")
            header = headers.get((bound[0].source.path.as_posix(), sheet or ""), {}).get(column)
            planned.append(
                PhotoReadingInput(
                    run_id=run_id,
                    photo_id=photo_id,
                    photo_path=asset["path"],
                    media_type=asset["media_type"],
                    photo_sheet=sheet,
                    photo_cell=cell,
                    column_header=header,
                    records=records,
                    quantities=quantities,
                    check_labels=labels,
                    photo_date=reading.photo_date,
                ).model_dump(mode="json")
            )
        return {"reread_items": planned}

    def apply_reread(self, value: dict[str, Any]) -> dict[str, Any]:
        """Compare each second reading with the first; disagreeing numbers are to be checked.

        The review packets are drawn again so a reviewer sees a photo whose
        two readings disagree.
        """

        state = dict(value["ledger"])
        run_id = state["run_id"]
        readings = load_photo_readings(self.workspace, run_id)
        changed = False
        for photo_id, result in (value.get("rereads") or {}).items():
            reading = readings.get(photo_id)
            if reading is None or not result:
                continue
            second = PhotoReadingSubmission.model_validate(_plain(result))
            agrees, reason = readings_agree(
                reading.readings, [r.model_dump(mode="json") for r in second.readings]
            )
            seen, unread = unread_agree(
                reading.transcription,
                [entry.model_dump(mode="json") for entry in second.transcription],
                unread_labels(reading.transcription),
            )
            if not seen:
                agrees, reason = False, "；".join(r for r in (reason if not agrees else "", unread) if r)
            readings[photo_id] = reading.model_copy(update={
                "reread_agrees": agrees,
                "reread_reason": reason,
                "needs_review": reading.needs_review or not agrees,
            })
            changed = True
        if changed:
            self.store.write_json(photo_readings_ref(run_id), {
                "run_id": run_id,
                "readings": [
                    r.model_dump(mode="json")
                    for r in sorted(readings.values(), key=lambda r: r.photo_id)
                ],
            })
            reference = self._reference(run_id)
            ledger = {m: _rows(rows) for m, rows in state["ledger"].items()}
            state["review_items"] = [
                {**item, "photos": photos_to_check(ledger[item["module_id"]], reference, readings)}
                for item in state.get("review_items") or []
            ]
        return state

    def merge_findings(self, value: dict[str, Any]) -> dict[str, Any]:
        """Keep program and reviewer findings side by side.

        When both name the same target, the finding carries both problems and
        is blocking if either one is.
        """

        item = value["item"]
        reviewed = dict((value.get("review") or {}).get("findings") or {})
        foreign: dict[str, Any] = {}
        if module_id := item.get("module_id"):
            # Rows of another module are revised only by that module's author;
            # its own reviewer sees the same conflict dossier.
            foreign = {t: f for t, f in reviewed.items() if _foreign_row(t, module_id)}
            reviewed = {t: f for t, f in reviewed.items() if t not in foreign}
        program = dict(item.get("program_findings") or {})
        findings = {
            target: _combined(program.get(target), reviewed.get(target))
            for target in dict.fromkeys([*program, *reviewed])
        }
        return {
            "findings": findings,
            "foreign_findings": foreign,
            "blocking": _blocking(findings),
            # Relations were settled before review; only blocking findings
            # send the author back.
            "revise": _blocking(findings),
            "revision_input": {
                "findings": {
                    target: finding
                    for target, finding in findings.items()
                    if finding.get("severity") == "blocking"
                },
                "minor": {
                    target: finding
                    for target, finding in findings.items()
                    if finding.get("severity") != "blocking"
                },
            },
        }

    def close_review(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            "owner": value["item"].get("module_id") or value["item"].get("owner"),
            "findings": value["merged"]["findings"],
            "revision": value.get("revision"),
        }

    def finalize_ledger(self, value: dict[str, Any]) -> dict[str, Any]:
        context = value["context"]
        run_id = context["run_id"]
        ledger = {
            module_id: _rows(rows) for module_id, rows in value["ledger"]["ledger"].items()
        }
        reviews = value.get("reviews") or {}
        changed: list[dict[str, Any]] = []
        removed: list[str] = []
        unanswered: dict[str, list[str]] = {}
        for module_id, review in reviews.items():
            revision = review.get("revision")
            blocking = [
                target
                for target, finding in (review.get("findings") or {}).items()
                if finding.get("severity") == "blocking"
            ]
            if revision is None:
                if blocking:
                    unanswered[module_id] = blocking
                continue
            # Relations were settled before review.  A resubmitted row that
            # drops them keeps them, unless a finding named that row (the
            # reviewer may have asked for a relation to go).
            current = {row.id: row for row in ledger[module_id]}
            named = set((review.get("findings") or {}).keys())
            revised = [
                row
                if row.relations or row.id not in current or row.id in named
                else row.model_copy(update={"relations": current[row.id].relations})
                for row in _rows(revision.get("rows") or [])
                if not _foreign_row(row.id, module_id)
            ]
            dropped = [
                row_id for row_id in revision.get("removed_row_ids") or []
                if not _foreign_row(row_id, module_id)
            ]
            reference = self._reference(run_id)
            revised = settle_evidence_status(
                revised, reference.get("summary_evidence_ids") or ()
            )
            ledger[module_id] = apply_revisions(ledger[module_id], revised, dropped)
            changed.extend({"module_id": module_id, **row} for row in _dump_rows(revised))
            removed.extend(dropped)
            answered = (
                {row.id for row in revised}
                | {e for row in revised for e in row.evidence_ids}
                | set(dropped)
                | set((revision.get("responses") or {}).keys())
            )
            if missing := [target for target in blocking if target not in answered]:
                unanswered[module_id] = missing
        dossiers = build_dossiers(self.workspace, run_id, ledger_judgments(ledger))
        names = relation_names(self._relation_method(run_id))
        ledger, dropped_relations = checked_relations(ledger, names)
        self._write_knowledge_candidates(run_id, ledger, names)
        conflicts = [d for d in dossiers if d["kind"] == "conflict"]
        chains = object_chains(dossiers, ledger)
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments.json", dumped)
        self._write_gaps(run_id, ledger)
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/cross-module.json",
            {
                "unresolved_conflicts": conflicts,
                "object_chains": chains,
                "dropped_relations": dropped_relations,
            },
        )
        units = self._knowledge_units(run_id)
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/rule-standards.json", standards_coverage(ledger, units)
        )
        elsewhere = self._data_elsewhere(run_id, ledger)
        reference = self._reference(run_id)
        groups = action_groups(ledger, reference.get("checkpoint_of") or {})
        counts = self._write_ledger_counts(run_id, ledger, groups, context["module_titles"])
        slots = {**(reference.get("slots") or {}), **counts}
        writing_items = []
        for module in context["modules"]:
            rows = ledger[module["module_id"]]
            writing_items.append(
                {
                    "module_id": module["module_id"],
                    "title": module["title"],
                    "sections": [
                        _section_brief(sub, [row for row in rows if row.submodule_id == sub["id"]],
                                       ledger=ledger, units=units, elsewhere=elsewhere,
                                       names=names, slots=slots,
                                       fact_texts=reference.get("fact_texts") or {})
                        for sub in module["submodules"]
                    ],
                }
            )
        return {
            "run_id": run_id,
            "ledger": dumped,
            "unanswered_findings": unanswered,
            "writing_items": writing_items,
            "action_groups": groups,
            "chief_input": {
                "object_chains": chains,
                "unresolved_conflicts": [conflict_summary(d) for d in conflicts],
                "changed_rows": changed,
                "removed_row_ids": removed,
                "action_row_ids": [
                    row.id for rows in ledger.values() for row in rows if row.action is not None
                ],
                # 3.2's rows, formed and ordered by the program; the chief writes each once.
                "action_groups": groups,
                # Counts over the settled ledger, and the values of the rows
                # with a problem or a gap, by slot.
                "value_slots": slot_lines(
                    [
                        *counts,
                        # Each column's counts, so no total is counted by hand.
                        *(
                            slot_id for slot_id, slot in slots.items()
                            if slot_id.startswith("K-") and str(slot.get("label", "")).endswith("_count")
                        ),
                        *(
                            c for rows in ledger.values() for row in rows
                            if row.disposition in {"concern", "undetermined"}
                            for c in row.calculation_ids
                        ),
                        *slots_for_evidence(
                            {
                                e for rows in ledger.values() for row in rows
                                if row.disposition in {"concern", "undetermined"}
                                for e in row.evidence_ids
                            },
                            slots,
                        ),
                    ],
                    slots,
                ),
                "module_titles": context["module_titles"],
            },
        }

    def _write_ledger_counts(
        self,
        run_id: str,
        ledger: Mapping[str, Sequence[JudgmentRow]],
        groups: Sequence[Mapping[str, Any]],
        titles: Mapping[str, str],
    ) -> dict[str, dict[str, Any]]:
        """Counts over the settled ledger the chief may cite, as slots after the calculations."""

        reference = self._reference(run_id)
        start = len(reference.get("calculation_ids") or ()) + 1
        counts: dict[str, dict[str, Any]] = {}

        def add(label: str, value: int) -> None:
            counts[f"K-{start + len(counts):04d}"] = {
                "text": format_value(value, "个"), "label": label, "value": float(value),
                "unit": "个", "evidence_ids": [],
            }

        scopes = [("全部维度", [r for rows in ledger.values() for r in rows])]
        scopes += [(titles.get(m, m), list(rows)) for m, rows in ledger.items()]
        for scope, rows in scopes:
            for disposition, word in (("concern", "有问题"), ("undetermined", "不能判定")):
                objects = {row.object for row in rows if row.disposition == disposition}
                add(f"{scope}{word}的对象数", len(objects))
        add("改善行动项数", len(groups))
        for priority in dict.fromkeys(group["priority"] for group in groups):
            add(f"“{priority}”行动项数", sum(1 for g in groups if g["priority"] == priority))
        self.store.write_json(f"Work/runs/{run_id}/ledger/ledger-counts.json", counts)
        return counts

    def _final_rows(self, run_id: str) -> dict[str, list[JudgmentRow]]:
        path = self.workspace / f"Work/runs/{run_id}/ledger/judgments.json"
        if not path.is_file():
            return {}
        return {
            module_id: _rows(rows)
            for module_id, rows in json.loads(path.read_text(encoding="utf-8")).items()
        }

    def _slots(self, run_id: str) -> dict[str, dict[str, Any]]:
        slots = dict(self._reference(run_id).get("slots") or {})
        path = self.workspace / f"Work/runs/{run_id}/ledger/ledger-counts.json"
        if path.is_file():
            slots.update(json.loads(path.read_text(encoding="utf-8")))
        return slots

    def _write_knowledge_candidates(
        self, run_id: str, ledger: Mapping[str, Sequence[JudgmentRow]], names: Sequence[str]
    ) -> None:
        """What this run suggests the knowledge base may be missing.

        Two kinds, both after review: relations the authors drew that the
        relation method does not list, and records the field index left
        unassigned, with the units and sections the authors chose.  They are
        candidates for a person to confirm, never applied automatically.
        """

        try:
            reference = self._reference(run_id)
        except OSError:
            reference = {}
        unassigned = set(reference.get("unassigned_evidence_ids") or ())
        index = ProjectEvidenceIndex(self.workspace, run_id)
        all_fields = (
            {item.id: item.checkpoint_field or "" for item in index.items()}
            if index.path.is_file() else {}
        )
        fields = {e: all_fields.get(e, "") for e in unassigned}
        placements = [
            {
                "field": fields.get(evidence_id, ""),
                "evidence_id": evidence_id,
                "row_id": row.id,
                "submodule_id": row.submodule_id,
                "rule_refs": list(row.rule_refs),
            }
            for rows in ledger.values()
            for row in rows
            for evidence_id in row.evidence_ids
            if evidence_id in unassigned
        ]
        _found, departures = placement_findings(
            ledger, sorted(unassigned), reference.get("default_sections") or {},
            self._unit_sections(run_id),
        )
        for departure in departures:
            departure["fields"] = sorted({all_fields.get(e, "") for e in departure["evidence_ids"]})
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/knowledge-candidates.json",
            {
                "note": (
                    "本次运行提示知识库可能缺的内容，只是候选：须经人确认，并在多个项目里重复出现，"
                    "才提议修订知识库；不自动写回。"
                ),
                "unlisted_relations": unlisted_relations(ledger, names),
                "field_placements": placements,
                "section_departures": departures,
            },
        )

    def _relation_method(self, run_id: str) -> str:
        try:
            return KnowledgeContextBuilder(
                self.workspace, run_id, global_root=self.global_knowledge_root
            ).relation_method()
        except (OSError, ValueError):
            return ""

    def _judgment_conditions(self, run_id: str) -> str:
        try:
            return KnowledgeContextBuilder(
                self.workspace, run_id, global_root=self.global_knowledge_root
            ).judgment_conditions()
        except (OSError, ValueError):
            return ""

    def _relation_candidates(self, run_id: str | None, module_id: str | None) -> dict[str, Any]:
        """Other modules' flagged rows and the relation method, for one author's revision."""

        path = self.workspace / f"Work/runs/{run_id}/ledger/relation-candidates.json"
        if not run_id or not module_id or not path.is_file():
            return {"flagged_elsewhere": [], "relation_method": "", "relation_names": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "relation_names": relation_names(data.get("method", "")),
            "flagged_elsewhere": [
                f"{entry['row_id']} {entry['line']}"
                for entry in data.get("flagged", [])
                if entry.get("module_id") != module_id
            ],
            "relation_method": data.get("method", ""),
        }

    def _knowledge_units(self, run_id: str) -> dict[str, dict[str, str]]:
        """Knowledge units of the run's frozen sources; none when there are no sources."""

        try:
            documents = KnowledgeContextBuilder(
                self.workspace, run_id, global_root=self.global_knowledge_root
            ).freeze_sources()
        except (OSError, ValueError):
            return {}
        return knowledge_units(document.text for document in documents)

    def _data_elsewhere(
        self, run_id: str, ledger: Mapping[str, Sequence[JudgmentRow]]
    ) -> dict[str, list[str]]:
        """Row id -> the subsections that own all of its facts, when it owns none of them.

        Mappers copy one observation into several subsections; the owner from
        ``observation-ownership.yaml`` presents the data, the others cite it.
        """

        observations = build_observation_ledger(self.workspace, run_id)
        if observations is None:
            return {}
        found: dict[str, list[str]] = {}
        for rows in ledger.values():
            for row in rows:
                owners = [observations.owner_for_reference(e) for e in row.evidence_ids]
                if not owners or any(owner is None for owner in owners):
                    continue
                places = sorted(
                    {owner.submodule_id for owner in owners if owner.submodule_id}
                    - {row.submodule_id}
                )
                if places:
                    found[row.id] = places
        return found

    def _write_gaps(self, run_id: str, ledger: Mapping[str, Sequence[JudgmentRow]]) -> None:
        for module_id, rows in ledger.items():
            for row in rows:
                if row.gap is None:
                    continue
                self.store.write_json(
                    f"Work/runs/{run_id}/gaps/GAP-{row.id}.json",
                    {
                        "gap": f"{row.object}：{row.gap.missing}",
                        "impact": row.gap.impact,
                        "requested_input": row.gap.requested_input,
                        "task_id": f"module-{module_id}",
                        "row_id": row.id,
                        "source_ids": list(row.evidence_ids),
                    },
                )

    # -- chief and checks --------------------------------------------------

    def check_chief(self, value: dict[str, Any]) -> dict[str, Any]:
        final = value["ledger"]
        ledger = {m: _rows(rows) for m, rows in final["ledger"].items()}
        groups = final.get("action_groups") or []
        chapter3 = ChiefChapter3.model_validate(value["chapter3"])
        chapter1 = value.get("chapter1") or {}
        findings = chief_findings(
            actions=grouped_actions(groups, chapter3.actions),
            health=[DimensionHealth.model_validate(h) for h in chapter1.get("dimension_health", [])],
            risks=[KeyRisk.model_validate(r) for r in chapter1.get("key_risks", [])],
            rows_by_module=ledger,
            # Each check reviews the chapter just written, which its revision can fix.
            texts={
                field: text
                for field, text in (chapter1 or value["chapter3"]).items()
                if isinstance(text, str)
            },
        )
        if not chapter1:
            findings.pop("1.2", None)
            findings.update(action_group_findings(groups, chapter3.actions))
            findings.update(
                cross_finding_findings(
                    chapter3.cross_findings,
                    rows_by_module=ledger,
                    overview=risk_overview_markdown(chapter3, {}),
                )
            )
        # Every value the chief writes is a slot; the structured fields too.
        run_id = final.get("run_id")
        if run_id:
            slots = self._slots(run_id)
            problems, hints = slot_check("\n".join(_strings(chapter1 or value["chapter3"])), slots)
            key = "1.2" if chapter1 else "3.1"
            if problems:
                findings[key] = _combined(
                    findings.get(key),
                    {
                        "problem": "；".join(problems),
                        "severity": "blocking",
                        "suggestion": SLOT_SUGGESTION,
                    },
                )
            if hints:
                findings[key] = _combined(
                    findings.get(key),
                    {"problem": "请核对：" + "；".join(hints), "severity": "minor",
                     "suggestion": HINT_SUGGESTION},
                )
        return {"findings": findings, "blocking": _blocking(findings)}

    def check_report(self, value: dict[str, Any]) -> dict[str, Any]:
        run_id = value["ledger"]["run_id"]
        reference = self._reference(run_id)
        slots = self._slots(run_id)
        fact_texts = reference.get("fact_texts") or {}
        ledger = {m: _rows(rows) for m, rows in value["ledger"]["ledger"].items()}
        by_submodule: dict[str, list[JudgmentRow]] = {}
        for rows in ledger.values():
            for row in rows:
                by_submodule.setdefault(row.submodule_id, []).append(row)
        elsewhere = self._data_elsewhere(run_id, ledger)
        row_sections = {row.id: row.submodule_id for rows in ledger.values() for row in rows}
        items = []
        for module_id, sections in value["written"].items():
            texts = {section["submodule_id"]: section["text"] for section in sections}
            layout = layout_findings(
                sections=texts,
                rows_by_submodule=by_submodule,
                elsewhere=elsewhere,
                slots=slots,
                row_sections=row_sections,
            )
            cited = {
                entry["row_id"]: entry
                for row in ledger.get(module_id, [])
                for entry in related_rows([row], ledger)
            }
            items.append(
                {
                    "run_id": run_id,
                    "owner": module_id,
                    "sections": texts,
                    **({"cited_rows": list(cited.values())} if cited else {}),
                    "program_findings": _with_layout(
                        _with_table_terms(
                            report_check_findings(
                                sections=texts,
                                rows_by_submodule=by_submodule,
                                slots=slots,
                                quote_sources={
                                    sid: [
                                        fact_texts.get(e, "")
                                        for row in by_submodule.get(sid, [])
                                        for e in row.evidence_ids
                                    ]
                                    for sid in texts
                                },
                            ),
                            texts,
                            by_submodule,
                        ),
                        layout,
                    ),
                }
            )
        chief = value["chief"]
        chief_texts = self._chief_texts(chief, value["ledger"])
        all_rows = [row for rows in ledger.values() for row in rows]
        groups = value["ledger"].get("action_groups") or []
        cited = {
            row_id
            for entry in [
                *groups,
                *(chief["chapter3"].get("cross_findings") or []),
                *(chief["chapter1"].get("key_risks") or []),
            ]
            for row_id in entry.get("row_ids") or []
        }
        items.append(
            {
                "run_id": run_id,
                "owner": CHIEF_OWNER,
                "sections": chief_texts,
                "cited_rows": [
                    {
                        "id": row.id,
                        "object": row.object,
                        "checkpoint": row.checkpoint,
                        "disposition": row.disposition,
                        "evidence_status": row.evidence_status,
                        "risk_level": row.risk_level,
                        "basis": row.basis,
                        "hazard": row.hazard,
                    }
                    for row in all_rows
                    if row.id in cited
                ],
                "program_findings": self._chief_text_findings(
                    report_check_findings(
                        sections=chief_texts,
                        rows_by_submodule={},
                        slots=slots,
                        quote_sources={
                            section_id: list(fact_texts.values()) for section_id in chief_texts
                        },
                    ),
                    chief_texts,
                ),
            }
        )
        return {"edit_items": items}

    @staticmethod
    def _chief_texts(chief: Mapping[str, Any], final: Mapping[str, Any]) -> dict[str, str]:
        """The chief's text sections; 3.1 is drawn from its two structured layers."""

        texts = {
            section_id: chief["chapter1"][field]
            for section_id, field in CHIEF_SECTIONS.items()
            if section_id.startswith("1.")
        }
        chapter3 = ChiefChapter3.model_validate(chief["chapter3"])
        texts["3.1"] = risk_overview_markdown(
            chapter3, (final.get("chief_input") or {}).get("module_titles") or {}
        )
        return texts

    @staticmethod
    def _chief_text_findings(
        findings: dict[str, dict[str, Any]], chief_texts: Mapping[str, str]
    ) -> dict[str, dict[str, Any]]:
        """Add, per section, the check that no generated table is written again."""

        fields = {CHIEF_SECTIONS[section_id]: section_id for section_id in chief_texts}
        tables = chief_findings(
            actions=[], health=[], risks=[], rows_by_module={},
            texts={field: chief_texts[section_id] for field, section_id in fields.items()},
        )
        merged = dict(findings)
        for field, finding in tables.items():
            section_id = fields[field]
            merged[section_id] = _combined(merged.get(section_id), finding)
        return merged

    @staticmethod
    def _revised_sections(item: Mapping[str, Any], *revisions: Mapping[str, Any] | None) -> dict[str, str]:
        sections = dict(item["sections"])
        for revision in revisions:
            sections.update(
                {
                    key: text
                    for key, text in ((revision or {}).get("sections") or {}).items()
                    if key in sections
                }
            )
        return sections

    def recheck_edit(self, value: dict[str, Any]) -> dict[str, Any]:
        """Program checks on the sections a revision changed, which nothing reviewed after it.

        Workflow words and ids in the prose, slots that do not exist and
        numerals written outside slots, and, for the chief, tables written
        into prose that the program draws itself.  A finding sends the owner
        back once.
        """

        item = value["item"]
        revision = value.get("revision") or {}
        changed = {
            key: text
            for key, text in self._revised_sections(item, revision).items()
            if key in (revision.get("sections") or {})
        }
        findings: dict[str, dict[str, Any]] = {}
        run_id = item.get("run_id")
        slots = self._slots(run_id) if run_id else {}
        for section_id, text in changed.items():
            problems: list[str] = []
            hints: list[str] = []
            if leaked := process_terms(strip_slots(text)):
                problems.append("修订后的正文里出现了内部流程用语或编号：" + "、".join(leaked))
            if run_id:
                found, hints = slot_check(text, slots)
                problems += found
            if problems:
                findings[section_id] = {
                    "problem": "；".join(problems)
                    + ("；另请核对：" + "；".join(hints) if hints else ""),
                    "severity": "blocking",
                    "suggestion": "删去这些用语；" + SLOT_SUGGESTION,
                }
            elif hints:
                findings[section_id] = {
                    "problem": "请核对：" + "；".join(hints),
                    "severity": "minor",
                    "suggestion": HINT_SUGGESTION,
                }
        if item["owner"] == CHIEF_OWNER and changed:
            findings = self._chief_text_findings(findings, changed)
        findings = {key: f for key, f in findings.items() if key in changed}
        return {
            "findings": findings,
            "blocking": _blocking(findings),
            "revision_input": {"findings": findings, "minor": {}},
        }

    def close_edit(self, value: dict[str, Any]) -> dict[str, Any]:
        item = value["item"]
        recheck = value.get("recheck") or {}
        return {
            "owner": item["owner"],
            "sections": self._revised_sections(
                item, value.get("revision"), value.get("recheck_revision")
            ),
            "findings": value["merged"]["findings"],
            **({"recheck_findings": recheck["findings"]} if recheck.get("findings") else {}),
        }

    # -- composition and delivery -----------------------------------------

    def compose_report(self, value: dict[str, Any]) -> dict[str, Any]:
        state = dict(value["state"])
        run_id = str(state["run_id"])
        ledger = {m: _rows(rows) for m, rows in value["ledger"]["ledger"].items()}
        # The program writes every value slot now, in the prose and in the
        # chief's structured fields alike.
        slots = self._slots(run_id)
        edits = {
            edit["owner"]: {
                key: fill_slots(text, slots, unresolved=UNRESOLVED)
                for key, text in edit["sections"].items()
            }
            for edit in value["edits"].values()
        }
        chief = _filled(value["chief"], slots)
        with _taxonomy(state):
            submissions = {
                module_id: self._module_submission(module_id, rows, edits[module_id])
                for module_id, rows in ledger.items()
            }
            chief_texts = edits[CHIEF_OWNER]
            chapter1 = chief["chapter1"]
            chapter3 = chief["chapter3"]
            titles = {m: REPORT_TAXONOMY[m].title for m in ledger}
            row_status = {
                row.id: row.evidence_status for rows in ledger.values() for row in rows
            }
            edited = EditedReportSubmission(
                title=REPORT_TITLE,
                assessment_background=chief_texts["1.1"],
                findings_overview=findings_overview_markdown(
                    chief_texts["1.2"],
                    [DimensionHealth.model_validate(h) for h in chapter1["dimension_health"]],
                    [KeyRisk.model_validate(r) for r in chapter1.get("key_risks", [])],
                    [Phase.model_validate(p) for p in chapter1.get("phases", [])],
                    titles,
                    row_status,
                ),
                regional_executive_summary=chief_texts["1.3"],
                module_narratives={m: s.markdown for m, s in submissions.items()},
                risk_summary=chief_texts["3.1"],
                improvement_action_plan=action_table_markdown(
                    chapter3["action_lead"],
                    grouped_actions(
                        value["ledger"].get("action_groups") or [],
                        [ActionText.model_validate(a) for a in chapter3["actions"]],
                    ),
                    row_status,
                    {
                        row.id: (
                            titles[module_id],
                            REPORT_TAXONOMY[module_id].submodules[row.submodule_id].title,
                            row.checkpoint,
                        )
                        for module_id, rows in ledger.items()
                        for row in rows
                        if row.submodule_id in REPORT_TAXONOMY[module_id].submodules
                    },
                ),
                special_topic_plan=(
                    SpecialTopicPlan.model_validate(state["special_topic_plan"])
                    if state.get("special_topic_plan")
                    else None
                ),
                special_topic_analysis=(chief.get("chapter4") or {}).get(
                    "special_topic_analysis"
                ),
                photo_ids=runtime_photo_ids(
                    [EvidenceItem.model_validate(item) for item in state.get("evidence_items", [])],
                    [PhotoAsset.model_validate(asset) for asset in state.get("photo_assets", [])],
                ),
            )
            review_ref = self.store.write_json(
                f"Work/runs/{run_id}/ledger/review-record.json",
                {
                    "judgment_reviews": value.get("reviews") or {},
                    "unanswered_findings": value["ledger"].get("unanswered_findings") or {},
                    "edit_reviews": {
                        owner: edit.get("findings") for owner, edit in value["edits"].items()
                    },
                },
            )
            state["module_submissions"] = submissions
            state["edited_report"] = edited
            state["final_review_completion_ref"] = review_ref.relative_to(
                self.workspace
            ).as_posix()
            delivery = DeliveryTools(self.workspace, self.store)
            state = delivery.prepare_composed(state)
            state = delivery.publish(state)
            state = delivery.complete(state)
        return {
            "run_id": run_id,
            "delivery_status": state.get("delivery_status"),
            "delivery_completion_ref": state.get("delivery_completion_ref"),
            "output_artifacts": state.get("output_artifacts", []),
            "render_verification": state.get("render_verification"),
        }

    def _module_submission(
        self, module_id: str, rows: Sequence[JudgmentRow], sections: Mapping[str, str]
    ) -> ModuleSubmission:
        narratives: dict[str, str] = {}
        claims: list[dict[str, Any]] = []
        assessments: dict[str, PartAssessment] = {}
        for submodule_id in REPORT_TAXONOMY[module_id].submodules:
            part_rows = [row for row in rows if row.submodule_id == submodule_id]
            prose = place_problem_table(sections.get(submodule_id, ""), part_rows)
            if not prose:
                prose = "资料未提供，见数据缺口表。"
            evidence_ids = sorted({e for row in part_rows for e in row.evidence_ids})
            claim_id = f"C-{module_id}-{submodule_id}"
            if evidence_ids:
                prose = prose.rstrip() + f"\n\n[[CLAIM:{claim_id}]]"
            narratives[submodule_id] = prose
            claims.append(
                {
                    "id": claim_id,
                    "origin": "runtime_part_binding",
                    "module_id": module_id,
                    "submodule_id": submodule_id,
                    "text": prose,
                    "claim_type": "technical_interpretation",
                    "source_ids": evidence_ids,
                    "footnote_required": bool(evidence_ids),
                }
            )
            assessments[submodule_id] = PartAssessment(
                records=[
                    ObservationAssessment(
                        evidence_ids=row.evidence_ids,
                        scope=f"{row.object}｜{row.checkpoint}",
                        disposition=row.disposition,
                        basis=row.basis,
                    )
                    for row in part_rows
                    if row.evidence_ids
                ]
            )
        return ModuleSubmission.model_validate(
            {
                "module_id": module_id,
                "submodule_narratives": narratives,
                "claims": claims,
                "source_ids": sorted({s for claim in claims for s in claim["source_ids"]}),
                "unresolved_questions": [],
                "revision": 0,
                "assessments": assessments,
            }
        )

    def implementations(self) -> dict[str, Any]:
        def plain(method: Any) -> Any:
            return lambda value: method(_plain(value))

        methods = {
            "prepare-ledger-context": self.prepare_context,
            "build-judgment-ledger": self.build_ledger,
            "apply-judgment-relations": self.apply_relations,
            "plan-photo-reread": self.plan_reread,
            "apply-photo-reread": self.apply_reread,
            "merge-review-findings": self.merge_findings,
            "close-review": self.close_review,
            "finalize-judgment-ledger": self.finalize_ledger,
            "check-chief-tables": self.check_chief,
            "check-report": self.check_report,
            "recheck-edit": self.recheck_edit,
            "close-edit": self.close_edit,
            "compose-report": self.compose_report,
        }
        return {tool_id: plain(method) for tool_id, method in methods.items()}


__all__ = ["CHIEF_OWNER", "CHIEF_SECTIONS", "LedgerTools"]
