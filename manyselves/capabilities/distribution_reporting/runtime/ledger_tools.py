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
from ..domain.copied_text import COPIED_SUGGESTION, CopiedText, copied_problem, skill_parts
from ..domain.judgment_ledger import (
    HINT_SUGGESTION,
    PROBLEM_TABLE_MARKER,
    SLOT_SUGGESTION,
    ActionText,
    ChiefChapter3,
    ChiefFix,
    DimensionHealth,
    JudgmentRow,
    JudgmentSet,
    KeyRisk,
    Phase,
    action_group_findings,
    action_groups,
    action_table_markdown,
    apply_revisions,
    apply_row_patches,
    chief_findings,
    cross_finding_findings,
    drop_mirrored_relations,
    findings_overview_markdown,
    grouped_actions,
    known_values_from_text,
    layout_findings,
    ledger_findings,
    ledger_judgments,
    place_problem_table,
    problem_table,
    process_terms,
    report_check_findings,
    risk_overview_markdown,
    settle_evidence_status,
    slot_check,
)
from ..domain.taxonomy import (
    REPORT_TAXONOMY,
)
from ..domain.value_slots import (
    UNRESOLVED,
    fill_slots,
    slot_repairs,
    format_value,
    object_names,
    slot_lines,
    slot_table,
    slots_for_evidence,
    strip_slots,
)
from .assets import runtime_photo_ids
from .chief_fact_packets import apply_fact_findings, chief_fact_packets
from .context_packs import (
    PROJECT_LEVEL,
    GROUP_ROW_SPAN,
    JUDGE_GROUP_RECORDS,
    Sites,
    brief_row,
    compact_row,
    linker_packs,
    object_batches,
    project_map,
    section_groups,
)
from .cross_module import (
    checked_relations,
    conflict_summary,
    flagged_rows,
    object_chains,
    related_rows,
    relation_names,
    same_observation_findings,
    same_observation_rows,
    shared_observation_groups,
    see_also_by_row,
    unlisted_relations,
    unresolved_conflicts,
)
from .delivery_tools import DeliveryTools
from .grounds import grounded
from .joint_dossiers import build_dossiers
from .ledger_stage_tools import (
    LINK_PACK_CHARS,
    OBJECT_BATCH_CHARS,
    LedgerStageTools,
)
from .models.agentic import (
    EditedReportSubmission,
    ModuleSubmission,
    ObservationAssessment,
    PartAssessment,
    PhotoReadingSubmission,
)
from .models.photo_reading import PhotoReadingInput
from .models.reporting import (
    EvidenceItem,
    PhotoAsset,
    ReportRequest,
    ReportRevision,
    SpecialTopicPlan,
)
from .module_authoring_preparation import load_template_skill
from .observation_ledger import build_observation_ledger, shared_labels
from .photo_reading import (
    compare_readings,
    load_photo_readings,
    photo_expected_ref,
    photo_reading_labels,
    photo_readings_ref,
    photo_records,
    reading_label,
    readings_agree,
    rests_on_photo,
    unread_agree,
    unread_labels,
    zoom_tiles,
)
from .report_revision import (
    SECTION_NOTE,
    base_dir,
    chief_revision,
    module_revision_brief,
    plan_revision,
    splice_kept_rows,
)
from .research.fact_context import register_fact_sources, render_fact_packet
from .research.knowledge_context import KnowledgeContextBuilder
from .research.project_evidence import ProjectEvidenceIndex
from .rule_standards import (
    knowledge_units,
    section_standards,
    standards_coverage,
    unit_bodies,
    unit_ids,
)
from .run_taxonomy import run_taxonomy
from .storage import ReportingStore
from .template_lengths import matched_lengths, render_length_reference, template_sections
from .template_resolver import resolve_report_template
from .template_skill_paths import is_template_skill_path
from .template_skills import (
    plain_value,
    skill_order,
    skills_for_run,
    skills_with_lessons,
    template_policy,
)

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
        **({"grounds": reviewed["grounds"]} if reviewed.get("grounds") else {}),
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


# The Skill uses written for the writers (template-skill-roles.yaml): how a
# section is written, how chapters 1 and 3 draw it together, what an editor
# checks.  The judging method and the examples are not instructions to leave
# out of the prose.
_WRITING_USES = ("write", "synthesis", "check")
_TASKS_DIR = Path(__file__).resolve().parents[1] / "tasks"
# The tasks whose output the reader sees, directly or through a printed field.
_WRITING_TASKS = (
    "judge-module", "revise-judgments", "write-section", "revise-sections",
    "chief-chapter-1", "chief-chapter-3", "revise-chief-actions", "revise-chief-overview",
    "revise-chief-facts", "chief-chapter-4",
)


def task_requirements() -> list[str]:
    """The objective and requirements of the tasks that write what the reader sees."""

    texts = []
    for task_id in _WRITING_TASKS:
        path = _TASKS_DIR / f"{task_id}.yaml"
        if not path.is_file():
            continue
        task = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        texts.append(str(task.get("objective") or ""))
        texts.extend(str(line) for line in task.get("constraints") or ())
    # Wording a task gives in quotes is wording to use ("可参照〈标准名〉的相关要求"),
    # not an instruction to leave out of the prose.
    return [_QUOTED.sub("\n", text) for text in texts]


_QUOTED = re.compile(r"“[^”]*”|「[^」]*」|‘[^’]*’")


def _groups(context: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Judging groups by id; a context without groups judges each module whole."""

    if context.get("groups"):
        return {gid: dict(group) for gid, group in context["groups"].items()}
    return {
        module["module_id"]: {
            "module_id": module["module_id"],
            "submodule_ids": [sub["id"] for sub in module["submodules"]],
            "evidence_ids": list(module.get("evidence_ids") or ()),
            "first": True,
        }
        for module in context["modules"]
    }


def _judge_item(
    module: Mapping[str, Any], group: Mapping[str, Any], materials: str
) -> dict[str, Any]:
    """One judge's brief: the whole module, or one group of its sections."""

    item = {**module, "group_id": group["group_id"], "materials": materials}
    if not group["split"]:
        return item
    first = f"J-{module['module_id']}-{group['row_start']:02d}"
    return {
        **item,
        "submodules": [sub for sub in module["submodules"] if sub["id"] in group["submodule_ids"]],
        "evidence_ids": list(group["evidence_ids"]),
        "group_note": (
            f"本模块分几组判断：你负责 submodules 所列的小节，行编号从 {first} 起连续编号；"
            f"本模块其余小节（{'、'.join(group['others'])}）由另一位作者在别的对话里判断，"
            "它们的记录不在你的事实清单里，不用补判。"
        ),
    }


def _with_instruction(brief: dict[str, Any], instructions: Sequence[str] | None) -> dict[str, Any]:
    """Add a revision's wording change to one section's writing brief."""

    if not instructions:
        return brief
    return {**brief, "revision_note": SECTION_NOTE, "revision_instruction": "；".join(instructions)}


class LedgerTools(LedgerStageTools):
    """Tool implementations bound to one project workspace."""

    def __init__(self, workspace: Path, *, global_knowledge_root: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)
        self.global_knowledge_root = global_knowledge_root

    # -- shared context ---------------------------------------------------

    def prepare_context(self, value: dict[str, Any]) -> dict[str, Any]:
        state = value["state"]
        revision = (
            ReportRevision.model_validate(value["revision"]) if value.get("revision") else None
        )
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
            sites = Sites(items)
            fact_blocks: dict[str, str] = {}
            knowledge_blocks: list[str] = []
            knowledge_by_module: dict[str, str] = {}
            modules: list[dict[str, Any]] = []
            # A large module is judged in groups of its sections, each in a
            # conversation of its own, so no one judge writes the whole module
            # at once.  A revision keeps each module whole.
            judge_groups: list[dict[str, Any]] = []
            for module_id in module_ids:
                module = REPORT_TAXONOMY[module_id]
                module_items = [
                    item for item in index.module_items(module_id) if item.module_id == module_id
                ]
                section_ids = list(module.submodules)
                parts = (
                    [section_ids]
                    if revision is not None
                    else section_groups(
                        section_ids,
                        {
                            sid: sum(1 for item in module_items if item.submodule_id == sid)
                            for sid in section_ids
                        },
                        budget_records=JUDGE_GROUP_RECORDS,
                    )
                )
                for number, part in enumerate(parts):
                    group_id = module_id if len(parts) == 1 else f"{module_id}#{number + 1}"
                    # Records with a section outside the tree stay with the first group.
                    own = [
                        item for item in module_items
                        if item.submodule_id in part
                        or (number == 0 and item.submodule_id not in section_ids)
                    ]
                    # A judge reads its own records, every other module's
                    # records on the same objects and the project-level
                    # records, in one list: each source's header and reading
                    # notes appear once.
                    fact_blocks[group_id] = render_fact_packet(
                        [
                            *own,
                            *sites.same_object_items(module_id, own),
                            *sites.project_items(module_id),
                        ],
                        module_id,
                        shared=shared,
                        photo_readings=readings,
                        column_headers=headers,
                    )
                    judge_groups.append({
                        "group_id": group_id,
                        "module_id": module_id,
                        "first": number == 0,
                        "submodule_ids": list(part),
                        "evidence_ids": [item.id for item in own],
                        "row_start": number * GROUP_ROW_SPAN + 1,
                        "others": [sid for sid in section_ids if sid not in part],
                        "split": len(parts) > 1,
                    })
                knowledge_by_module[module_id] = knowledge.build_module(module_id).text
                knowledge_blocks.append(knowledge_by_module[module_id])
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
            unassigned_block = ""
            if unassigned:
                unassigned_block = (
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
            section_titles = {
                node_id: node.title
                for module_id in module_ids
                for node_id, node in [
                    (module_id, REPORT_TAXONOMY[module_id]),
                    *REPORT_TAXONOMY[module_id].submodules.items(),
                ]
            }
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
            conditions = knowledge.judgment_conditions()
            conditions_block = (
                "# 判断条件（知识库）\n"
                "知识库对各类记录能否比较、怎样判断的适用条件。知识单元的限值、结论标准、依据状态和这里的条件"
                "是判断的参考，通常准确有效；结论按本项目的实际情况分析，适用才照它判断。"
                "专家版方法里的判断方法只作思路参考，与这里冲突时以这里为准。\n\n"
                + conditions
                if conditions
                else ""
            )
            assignment = "# 委托与报告结构\n" + "\n".join(requirements) + "\n\n## 第二章固定小节\n" + structure
            fact_map = project_map(sites)
            length_reference = self._length_reference(run_id, module_ids)
            numbers = [
                render_calculations(calculations, calculation_gaps),
                render_statistics(statistic_lines),
            ]
            # What every judge of a module reads comes first and the group's
            # own records last, so the groups of one module (and, for the
            # first blocks, every judge) start with the same text.
            judge_materials = {
                group["group_id"]: "\n\n".join(
                    block
                    for block in [
                        assignment,
                        fact_map,
                        *numbers,
                        length_reference,
                        knowledge_by_module[group["module_id"]],
                        conditions_block,
                        "# 专家版方法（本模块，分判断方法、写法、范例、核对要点）\n"
                        + (module_skills.get(group["module_id"]) or "（本模块没有专家版方法）"),
                        (
                            "# 事实清单\n本模块的全部记录；module_id 不是本模块的，是别的模块对同一对象的记录，"
                            if not group["split"]
                            else "# 事实清单\n本模块中你负责的小节的全部记录；module_id 不是本模块的，"
                            "是别的模块对同一对象的记录，"
                        )
                        + "和不属于某个配电房的项目级记录（汇总表、收资资料），供对照，不由你判断。"
                        "别的对象上的记录只在项目图谱里列出位置和条数。\n\n"
                        + fact_blocks[group["group_id"]],
                        unassigned_block if group["first"] else "",
                    ]
                    if block
                )
                for group in judge_groups
            }
            chief_skill = "\n\n".join(
                f"## {skill_id}\n{skills[skill_id]}"
                for skill_id in skill_order()
                if skills.get(skill_id) and skills[skill_id] not in module_skills.values()
            )
        revision_plan = None
        if revision is not None:
            base = base_dir(self.workspace, run_id, revision.base_version)
            with _taxonomy(state):
                revision_plan = plan_revision(
                    revision,
                    base=base,
                    items=items,
                    sections={
                        module["module_id"]: [sub["id"] for sub in module["submodules"]]
                        for module in modules
                    },
                )
                changed = set(revision_plan["changed_evidence_ids"])
                for module in modules:
                    module_id = module["module_id"]
                    own = [
                        item for item in items
                        if item.id in changed and item.module_id in (module_id, None)
                    ]
                    brief = module_revision_brief(
                        revision_plan,
                        module_id,
                        render_fact_packet(
                            own, module_id, shared=shared, photo_readings=readings,
                            column_headers=headers,
                        ) if own else "",
                    )
                    if brief is not None:
                        module["revision"] = brief
            for module_id in module_ids:
                if module_id not in judge_materials:
                    continue
                base_materials = base / "ledger" / f"judge-materials-{module_id}.md"
                if base_materials.is_file():
                    # Keeping the base version's text lets unchanged steps be
                    # reused.  What changed reaches the reopened modules in
                    # their briefs.
                    judge_materials[module_id] = base_materials.read_text(encoding="utf-8")
            revision_plan = {k: v for k, v in revision_plan.items() if k != "base_rows"}
            self.store.write_json(f"Work/runs/{run_id}/ledger/revision-plan.json", revision_plan)
        for module_id, text in judge_materials.items():
            self.store.write_text(f"Work/runs/{run_id}/ledger/judge-materials-{module_id}.md", text)
        self.store.write_text(f"Work/runs/{run_id}/ledger/project-map-facts.md", fact_map)
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
        # A count has no unit; in prose it is written with a measure word (个).
        known_values.extend((calc.value, calc.unit or "个") for calc in calculations)
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
                "section_titles": section_titles,
                "request_texts": requirements,
                # What the chief and the story-line planner read of the expert method.
                "chief_skill": chief_skill,
                "assignment": assignment,
                "length_reference": length_reference,
                "numbers": "\n\n".join(numbers),
            },
        )
        return {
            "run_id": run_id,
            "modules": modules,
            # Each judge starts a conversation of its own from these materials.
            "judge_items": [
                _judge_item(
                    next(m for m in modules if m["module_id"] == group["module_id"]),
                    group,
                    judge_materials[group["group_id"]],
                )
                for group in judge_groups
            ],
            # Which group judges which sections and records, for routing later steps.
            "groups": {
                group["group_id"]: {
                    "module_id": group["module_id"],
                    "submodule_ids": group["submodule_ids"],
                    "evidence_ids": group["evidence_ids"],
                    "first": group["first"],
                }
                for group in judge_groups
            },
            "module_titles": {module["module_id"]: module["title"] for module in modules},
            "special_topic_plan": plan_value,
            "special_topic_input": {"plan": plan_value},
            "has_special_topic": plan_value is not None,
            "chief_key": CHIEF_OWNER,
            **({"revision": revision_plan} if revision_plan is not None else {}),
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
            values[calc.id] = {(float(calc.value), calc.unit or "个"), *known_values_from_text([calc.formula])}
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
        groups = _groups(context)
        row_groups: dict[str, str] = {}
        copied = self._copied(run_id)
        for module in context["modules"]:
            module_id = module["module_id"]
            # A module judged in groups is one judgment set again.
            submitted = {"module_id": module_id, "rows": []}
            for group_id, group in groups.items():
                if group["module_id"] != module_id or group_id not in value["judgments"]:
                    continue
                part = _plain(value["judgments"][group_id])
                for row in part.get("rows") or []:
                    row_groups.setdefault(str(row.get("id", "")), group_id)
                submitted["rows"].extend(part.get("rows") or [])
            plan = context.get("revision")
            if plan is not None:
                previous = (module.get("revision") or {}).get("previous_rows") or []
                plan = {**plan, "base_rows": {module_id: previous}}
            submitted["rows"] = splice_kept_rows(
                module_id, submitted.get("rows") or [], plan
            )
            judgment = JudgmentSet.model_validate(submitted)
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
                copied=copied,
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
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments-r0.json", dumped)
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/groups.json",
            {
                "groups": groups,
                "row_groups": row_groups,
                "modules": {
                    module["module_id"]: {
                        "submodule_ids": [sub["id"] for sub in module["submodules"]],
                        "evidence_ids": list(module["evidence_ids"]),
                    }
                    for module in context["modules"]
                },
            },
        )
        sites = self._sites(run_id)
        render = self._renderer(run_id)
        judged_map = project_map(sites, ledger)
        # The object lens reads the rows as judged, while the rows are related
        # and the photos read again: it needs neither.
        object_items = [
            {"run_id": run_id, **batch}
            for batch in object_batches(ledger, sites, render, budget_chars=OBJECT_BATCH_CHARS)
        ]
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/review-lenses.json",
            {"objects": [
                {"batch_id": b["batch_id"], "objects": b["objects"]} for b in object_items
            ]},
        )
        self.store.write_text(f"Work/runs/{run_id}/ledger/project-map-judged.md", judged_map)
        method = self._relation_method(run_id)
        return {
            "run_id": run_id,
            "ledger": dumped,
            "program_findings": findings,
            "dossiers": dossiers,
            # The cross-module linker reads every flagged row in full.
            "link_items": [
                {
                    "run_id": run_id,
                    "map": judged_map,
                    "relation_method": method or "（知识库没有关联方法）",
                    "relation_names": relation_names(method),
                    **pack,
                }
                for pack in linker_packs(ledger, sites, render, budget_chars=LINK_PACK_CHARS)
            ],
            # Each author first relates its rows on its own, while the linker
            # works; the linker's proposals come to it afterwards.
            "relate_items": [
                {
                    "run_id": run_id,
                    "module_id": group["module_id"],
                    "group_id": group_id,
                    "related": {
                        row.id: others
                        for row in ledger[group["module_id"]]
                        if row_groups.get(row.id, group_id) == group_id
                        and (others := see_also.get(row.id))
                    },
                    **self._relation_candidates(run_id, group["module_id"]),
                }
                for group_id, group in groups.items()
                if group["module_id"] in ledger
            ],
            "object_items": object_items,
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
        chosen_by_module = self._relation_sets(run_id, value)
        for module_id, chosen in chosen_by_module.items():
            by_row = dict(chosen.get("relations") or {})
            ledger[module_id] = [
                JudgmentRow.model_validate(
                    {**row.model_dump(mode="json"), "relations": _plain(by_row[row.id])}
                )
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
        observation_of, owner_sections = self._observation_map(run_id)
        ledger, dropped = checked_relations(ledger, names, observation_of)
        self._record_linker_overlap(run_id, ledger)
        program_findings = {
            module_id: dict(found) for module_id, found in built["program_findings"].items()
        }
        for module_id, found in same_observation_findings(
            ledger, observation_of, owner_sections
        ).items():
            for target, finding in found.items():
                program_findings.setdefault(module_id, {})[target] = _combined(
                    program_findings.get(module_id, {}).get(target), finding
                )
        # Candidates an author looked at and found not to hold.  The method's
        # relations are references: nothing requires every one to be decided.
        considered_not_valid = {
            module_id: [
                str(entry.get("relation", "")).strip()
                for entry in chosen.get("considered") or []
                if str(entry.get("relation", "")).strip()
            ]
            for module_id, chosen in chosen_by_module.items()
        }
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments-r1.json", dumped)
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/relations-r1.json",
            {
                "dropped_relations": dropped,
                "considered_not_valid": considered_not_valid,
                # The linker's proposals an author did not take, with why.
                "declined_proposals": {
                    module_id: list(chosen.get("declined_proposals") or [])
                    for module_id, chosen in chosen_by_module.items()
                    if chosen.get("declined_proposals")
                },
                "unlisted_relations": unlisted_relations(ledger, names),
            },
        )
        # The review lenses are built after the photos are read again
        # (plan-review), so they show the final readings.
        return {
            "run_id": run_id,
            "ledger": dumped,
            "program_findings": program_findings,
            "dossiers": built.get("dossiers") or [],
        }

    def _record_linker_overlap(
        self, run_id: str, ledger: Mapping[str, Sequence[JudgmentRow]]
    ) -> None:
        """What the linker added beyond what the authors were given, by kind.

        For each relation the authors took from a proposal: whether the other
        end was already among that group's candidates with its records (same
        object or same room), and how the two rows stand (same object, same
        distribution room, different rooms, or a project-level record).  The
        next run decides from this whether the linker is still needed.
        """

        base = self.workspace / f"Work/runs/{run_id}/ledger"
        proposals_path, candidates_path = base / "link-proposals.json", base / "relate-candidates.json"
        if not proposals_path.is_file() or not candidates_path.is_file():
            return
        kept = json.loads(proposals_path.read_text(encoding="utf-8")).get("kept") or []
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        proposed = {frozenset((p["row_id"], p["other_row_id"])) for p in kept}
        row_groups = self._groups_file(run_id).get("row_groups") or {}
        index = {row.id: (module_id, row) for module_id, rows in ledger.items() for row in rows}
        sites = self._sites(run_id)

        def kind(a: JudgmentRow, b: JudgmentRow) -> str:
            if set(sites.row_subjects(a)) & set(sites.row_subjects(b)):
                return "同对象"
            pa = set(sites.row_places(a)) - {PROJECT_LEVEL}
            pb = set(sites.row_places(b)) - {PROJECT_LEVEL}
            if not pa or not pb:
                return "项目级"
            return "同配电房" if pa & pb else "不同配电房"

        taken: dict[frozenset[str], dict[str, Any]] = {}
        own: dict[str, int] = {}
        for module_id, rows in ledger.items():
            for row in rows:
                for relation in row.relations:
                    if relation.row_id not in index:
                        continue
                    other = index[relation.row_id][1]
                    pair = frozenset((row.id, other.id))
                    if pair not in proposed:
                        k = kind(row, other)
                        own[k] = own.get(k, 0) + 1
                        continue
                    group = candidates.get(row_groups.get(row.id, module_id)) or {}
                    given = set(group.get("related") or []) | set(group.get("same_room") or [])
                    entry = taken.setdefault(pair, {
                        "rows": sorted(pair), "kind": kind(row, other), "in_candidates": False,
                    })
                    entry["in_candidates"] = entry["in_candidates"] or other.id in given
        summary: dict[str, dict[str, int]] = {}
        for entry in taken.values():
            counts = summary.setdefault(entry["kind"], {"taken": 0, "in_candidates": 0})
            counts["taken"] += 1
            counts["in_candidates"] += int(entry["in_candidates"])
        self.store.write_json(f"Work/runs/{run_id}/ledger/linker-overlap.json", {
            "proposals_kept": len(kept),
            "taken_by_kind": summary,
            "authors_own_by_kind": own,
            "taken": sorted(taken.values(), key=lambda e: e["rows"]),
        })

    def _relation_sets(self, run_id: str, value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Each module's relations: its groups' own linking, then what they took of the proposals.

        Both rounds are keyed by judging group; a proposal round adds to the
        rows' relations (a relation to the same row under the same name is
        kept once).  ``relations`` keyed by module is still read for a run
        planned before the two rounds.
        """

        groups = self._groups_file(run_id).get("groups") or {}

        def module_of(key: str) -> str:
            return str((groups.get(key) or {}).get("module_id") or key)

        merged: dict[str, dict[str, Any]] = {}
        for source in ("relations", "own", "proposals"):
            for key, result in (value.get(source) or {}).items():
                chosen = _plain(result) or {}
                into = merged.setdefault(
                    module_of(key), {"relations": {}, "considered": [], "declined_proposals": []}
                )
                for row_id, relations in (chosen.get("relations") or {}).items():
                    kept = into["relations"].setdefault(row_id, [])
                    seen = {
                        (_plain(r).get("row_id"), _plain(r).get("relation")) for r in kept
                    }
                    for relation in relations or []:
                        mark = (_plain(relation).get("row_id"), _plain(relation).get("relation"))
                        if mark not in seen:
                            kept.append(relation)
                            seen.add(mark)
                into["considered"].extend(chosen.get("considered") or [])
                into["declined_proposals"].extend(chosen.get("declined_proposals") or [])
        return merged

    # -- photo re-read ------------------------------------------------------

    def plan_reread(self, value: dict[str, Any]) -> dict[str, Any]:
        """The photos a problem or a gap rests on, to be read once more, independently.

        The table record is the fact and a photo backs it, so a photo is read
        again only where a judgment rests on the photo itself (see
        ``rests_on_photo``): a row the author marked 照片待核, or a number the
        photo shows that the table does not hold.  The second reading also
        gets the photo's four quarters enlarged.  The reader gets no recorded
        values and not what the first reading said, only the date it found,
        so that a dated record's blanks after that date are not taken for
        missed ones.
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
        expected = self._photo_expected(run_id)
        ledger = {m: _rows(rows) for m, rows in state["ledger"].items()}
        cited: dict[str, list[JudgmentRow]] = {}
        for rows in ledger.values():
            for row in rows:
                if row.disposition in {"concern", "undetermined"}:
                    for photo_id in row.photo_ids:
                        cited.setdefault(photo_id, []).append(row)
        index = ProjectEvidenceIndex(self.workspace, run_id)
        items = index.items() if index.path.is_file() else []
        by_id = {item.id: item for item in items}
        planned = []
        for photo_id, rows in cited.items():
            reading = readings.get(photo_id)
            asset = assets.get(photo_id)
            if reading is None or asset is None:
                continue
            bound_values = (expected.get(photo_id) or {}).get("bound") or {}
            if not rests_on_photo(rows, reading, bound_values):
                continue
            bound = [by_id[e] for e in reading.evidence_ids if e in by_id]
            if not bound:
                continue
            records, quantities = photo_records(bound, items)
            sheet, cell = asset.get("sheet"), asset.get("cell")
            column = re.sub(r"\d+", "", cell or "")
            header = headers.get((bound[0].source.path.as_posix(), sheet or ""), {}).get(column)
            images = [{"path": asset["path"], "media_type": asset["media_type"], "part": "整张照片"}]
            images += zoom_tiles(
                self.workspace, asset["path"], f"Work/runs/{run_id}/ledger/photo-tiles", photo_id
            )
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
                    check_labels=unread_labels(reading.transcription),
                    photo_date=reading.photo_date,
                    images=images,
                ).model_dump(mode="json")
            )
        return {"reread_items": planned}

    def _photo_expected(self, run_id: str) -> dict[str, Any]:
        path = self.workspace / photo_expected_ref(run_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def apply_reread(self, value: dict[str, Any]) -> dict[str, Any]:
        """Compare each second reading with the first; disagreeing numbers are to be checked.

        The review packets are drawn again so a reviewer sees a photo whose
        two readings disagree.
        """

        state = dict(value["ledger"])
        run_id = state["run_id"]
        readings = load_photo_readings(self.workspace, run_id)
        expected = self._photo_expected(run_id)
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
            # Either reading showing the recorded value is enough: the photo
            # shows the record.
            match, match_reason = reading.record_match, reading.reason
            if match == "conflicts":
                bound = expected.get(photo_id) or {}
                again, again_reason = compare_readings(
                    [r.model_dump(mode="json") for r in second.readings],
                    bound.get("bound") or {}, bound.get("same_subject") or {},
                )
                if again == "matches":
                    match, match_reason = "matches", f"复读：{again_reason}"
                    agrees, reason = True, "首读与记录不同，复读与记录一致"
            readings[photo_id] = reading.model_copy(update={
                "record_match": match,
                "reason": match_reason,
                "reread_agrees": agrees,
                "reread_reason": reason,
                "needs_review": not agrees or match == "conflicts",
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
        if item.get("owner") and item.get("run_id"):
            # An editor's finding is held to the ledger it was read against,
            # and names the owner's other sections holding the same values.
            reviewed = grounded(reviewed, self._ledger_material(str(item["run_id"])))
            sections = item.get("sections") or {}
            for target, finding in reviewed.items():
                slots = [g for g in finding.get("grounds") or {} if g[:2] in {"V-", "K-", "S-"}]
                elsewhere = [
                    section_id for section_id, text in sections.items()
                    if section_id != target and any(f"〔{slot}" in str(text) for slot in slots)
                ]
                if elsewhere:
                    finding["same_elsewhere"] = elsewhere
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

    def recheck_judgments(self, value: dict[str, Any]) -> dict[str, Any]:
        """The program's row checks again, on the rows a revision changed or added.

        A patch that cannot be applied, or a changed row the checks send back
        (an id it cannot cite, a value it does not hold, copied instructions),
        is blocking and goes back to the author once more.
        """

        item = value["item"]
        run_id = str(item["run_id"])
        module_id = str(item["module_id"])
        revision = _plain(value.get("revision")) or {}
        rows = _rows(item.get("rows") or [])
        changed, _removed, problems = apply_row_patches(rows, revision)
        changed = [row for row in changed if not _foreign_row(row.id, module_id)]
        module = (self._groups_file(run_id).get("modules") or {}).get(module_id) or {}
        reference = self._reference(run_id)
        found: dict[str, Any] = {}
        if changed:
            checked = ledger_findings(
                JudgmentSet(module_id=module_id, rows=changed),
                submodule_ids=module.get("submodule_ids") or [row.submodule_id for row in changed],
                module_evidence_ids=(),
                known_evidence_ids=reference["evidence_ids"],
                known_photo_ids=reference["photo_ids"],
                known_calculation_ids=reference["calculation_ids"],
                evidence_subjects=reference.get("evidence_subjects"),
                cited_values=reference.get("cited_values"),
                summary_evidence_ids=reference.get("summary_evidence_ids") or (),
                copied=self._copied(run_id),
            )
            ids = {row.id for row in changed}
            found = {
                target: finding
                for target, finding in checked.items()
                if target in ids and finding.get("severity") == "blocking"
            }
        for row_id, problem in problems.items():
            found[row_id] = _combined(
                found.get(row_id),
                {"problem": problem, "severity": "blocking",
                 "suggestion": "按字段名重交这一行的改动；新增的行写进 rows"},
            )
        return {
            "blocking": bool(found),
            "revision_input": {"findings": found, "minor": {}, "recheck": True},
        }

    def close_review(self, value: dict[str, Any]) -> dict[str, Any]:
        """The settled review, with the revisions' patches applied: the changed rows in full."""

        item = value["item"]
        revision = None
        submitted = [
            _plain(r) for r in (value.get("revision"), value.get("second_revision")) if r
        ]
        if submitted:
            rows = _rows(item.get("rows") or [])
            changed: dict[str, JudgmentRow] = {}
            removed: list[str] = []
            responses: dict[str, Any] = {}
            for part in submitted:
                base = [changed.get(row.id, row) for row in rows] + [
                    row for row_id, row in changed.items() if row_id not in {r.id for r in rows}
                ]
                new_rows, dropped, _problems = apply_row_patches(base, part)
                changed.update({row.id: row for row in new_rows})
                removed.extend(dropped)
                responses.update(part.get("responses") or {})
            revision = {
                "rows": _dump_rows(list(changed.values())),
                "removed_row_ids": list(dict.fromkeys(removed)),
                "responses": responses,
            }
        return {
            "owner": item.get("module_id") or item.get("owner"),
            "module_id": item.get("module_id"),
            "group_id": item.get("group_id"),
            "findings": value["merged"]["findings"],
            "revision": revision if item.get("module_id") else value.get("revision"),
        }

    def finalize_ledger(self, value: dict[str, Any]) -> dict[str, Any]:
        context = value["context"]
        run_id = context["run_id"]
        ledger = {
            module_id: _rows(rows) for module_id, rows in value["ledger"]["ledger"].items()
        }
        reviews = value.get("reviews") or {}
        settled_before = dict(ledger)
        changed: list[dict[str, Any]] = []
        removed: list[str] = []
        unanswered: dict[str, list[str]] = {}
        for key, review in reviews.items():
            module_id = review.get("module_id") or key
            revision = review.get("revision")
            blocking = [
                target
                for target, finding in (review.get("findings") or {}).items()
                if finding.get("severity") == "blocking"
            ]
            if revision is None:
                if blocking:
                    unanswered.setdefault(module_id, []).extend(blocking)
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
                unanswered.setdefault(module_id, []).extend(missing)
        lens_path = self.workspace / f"Work/runs/{run_id}/ledger/review-lens-findings.json"
        lens_findings = (
            json.loads(lens_path.read_text(encoding="utf-8")) if lens_path.is_file() else {}
        )
        ledger, mirrored = drop_mirrored_relations(
            settled_before,
            ledger,
            [
                row_id
                for row_id, by_lens in (lens_findings.get("by_row") or {}).items()
                if (by_lens.get("relation") or {}).get("severity") == "blocking"
            ],
        )
        dossiers = build_dossiers(self.workspace, run_id, ledger_judgments(ledger))
        names = relation_names(self._relation_method(run_id))
        observation_of, _owners = self._observation_map(run_id)
        ledger, dropped_relations = checked_relations(ledger, names, observation_of)
        self._write_knowledge_candidates(run_id, ledger, names)
        conflicts = unresolved_conflicts(dossiers, lens_findings.get("by_row") or {})
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
                # Relations one end gave up in revision, dropped from the other end.
                "mirrored_removals": mirrored,
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
        revision = context.get("revision") or {}
        text_instructions = revision.get("text_instructions") or {}
        writing_items = []
        for module in context["modules"]:
            rows = ledger[module["module_id"]]
            writing_items.append(
                {
                    "module_id": module["module_id"],
                    "title": module["title"],
                    "sections": [
                        _with_instruction(
                            _section_brief(
                                sub, [row for row in rows if row.submodule_id == sub["id"]],
                                ledger=ledger, units=units, elsewhere=elsewhere,
                                names=names, slots=slots,
                                fact_texts=reference.get("fact_texts") or {},
                            ),
                            text_instructions.get(sub["id"]),
                        )
                        for sub in module["submodules"]
                    ],
                }
            )
        chief_input_revision = (
            chief_revision(
                revision,
                base=base_dir(self.workspace, run_id, revision["base_version"]),
                final_ledger=dumped,
            )
            if revision
            else None
        )
        same_observation = [
            {
                "row_ids": [row.id for row in rows],
                "sections": list(dict.fromkeys(row.submodule_id for row in rows)),
                "object": rows[0].object,
            }
            for rows in shared_observation_groups(ledger, observation_of)
            if any(row.disposition in {"concern", "undetermined"} for row in rows)
        ]
        story_input = self.story_input(
            run_id, ledger, chains=chains, same_observation=same_observation
        )
        self.store.write_text(f"Work/runs/{run_id}/ledger/project-map-final.md", story_input["map"])
        return {
            "run_id": run_id,
            "ledger": dumped,
            "unanswered_findings": unanswered,
            "writing_items": writing_items,
            "action_groups": groups,
            "story_input": story_input,
            "chief_input": {
                # The chief starts a conversation of its own: the map, the
                # expert method for chapters 1 and 3, the assignment and the
                # length reference come as text ahead of the task.
                "map": story_input["map"],
                "skill": reference.get("chief_skill") or "（没有专家版方法）",
                "assignment": "\n\n".join(
                    block for block in (
                        reference.get("assignment") or "",
                        reference.get("numbers") or "",
                        reference.get("length_reference") or "",
                    ) if block
                ),
                # Every flagged row, briefly; the rows judged without a
                # problem are counted per module.
                "rows": story_input["rows"],
                "module_counts": {
                    module_id: {
                        disposition: sum(1 for row in rows if row.disposition == disposition)
                        for disposition in ("concern", "undetermined", "no_concern", "not_applicable")
                    }
                    for module_id, rows in ledger.items()
                },
                "object_chains": chains,
                # One source cell judged in several sections: one record.
                "same_observation": same_observation,
                "unresolved_conflicts": [conflict_summary(d) for d in conflicts],
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
                **({"revision": chief_input_revision} if chief_input_revision else {}),
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
            # A count is a bare number: the prose adds its own measure word.
            counts[f"K-{start + len(counts):04d}"] = {
                "text": format_value(value, ""), "label": label, "value": float(value),
                "unit": "", "evidence_ids": [],
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

    def _ledger_material(self, run_id: str) -> dict[str, str]:
        """The run's final rows, source records and value slots by id, for grounds."""

        material: dict[str, str] = {
            row.id: "｜".join(
                text for text in (
                    row.object, row.checkpoint, row.disposition, row.evidence_status,
                    row.risk_level or "", row.basis,
                ) if text
            )
            for rows in self._final_rows(run_id).values()
            for row in rows
        }
        material.update(
            {e: str(text) for e, text in (self._reference(run_id).get("fact_texts") or {}).items()}
        )
        slots = self._slots(run_id)
        material.update(
            {key.strip("〔〕"): line for key, line in slot_lines(slots, slots).items()}
        )
        return material

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

    def _copied(self, run_id: str) -> CopiedText:
        """The instruction texts of this run, to hold printed text against (see copied_text).

        Instructions: the expert method of every module and the chief, the
        knowledge base's relation method and the writing tasks' requirements.
        The run's own records and knowledge units may share wording with them
        and pass.
        """

        cached = getattr(self, "_copied_cache", {})
        if run_id in cached:
            return cached[run_id]
        reference = self._reference(run_id)
        titles = [
            use.title for use in template_policy().uses if use.id in _WRITING_USES
        ]
        instructions = [
            *(
                skill_parts(text, titles)
                for text in [
                    *(reference.get("author_skills") or {}).values(),
                    str(reference.get("chief_skill") or ""),
                ]
            ),
            self._relation_method(run_id),
            *task_requirements(),
        ]
        allowed = [
            *(str(text) for text in (reference.get("fact_texts") or {}).values()),
            *(f"{title}\n{body}" for title, body in self._unit_bodies(run_id).values()),
            # Section titles are the report's own headings.
            *CHIEF_SECTION_TITLES.values(),
            *(f"{sid} {title}" for sid, title in (reference.get("section_titles") or {}).items()),
        ]
        cached[run_id] = CopiedText(instructions, allowed)
        self._copied_cache = cached
        return cached[run_id]

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

    def _observation_map(self, run_id: str) -> tuple[dict[str, str], dict[str, str]]:
        """(routed copy -> its observation, observation -> owning section).

        Mappers copy one source cell into several sections; each copy is the
        same observation.  The owner is the ownership rule's, else the first
        route.
        """

        observations = build_observation_ledger(self.workspace, run_id)
        if observations is None:
            return {}, {}
        observation_of = {
            route.evidence_id: group.observation_id
            for group in observations.groups
            for route in group.routes
        }
        owners = {
            group.observation_id: str(group.owner.submodule_id) for group in observations.groups
        }
        return observation_of, owners

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
            # Uncertain hints go to the delivery report, not to the chief.
            self._record_hints(run_id, f"chief_{key}", {key: hints} if hints else {})
            copied = self._copied(run_id)
            if spans := copied.spans("\n".join(_strings(chapter1 or value["chapter3"]))):
                findings[key] = _combined(
                    findings.get(key),
                    {"problem": copied_problem(spans), "severity": "blocking",
                     "suggestion": COPIED_SUGGESTION},
                )
        return {"findings": findings, "blocking": _blocking(findings)}

    def plan_chief_facts(self, value: dict[str, Any]) -> dict[str, Any]:
        """Chapters 1 and 3 cut into small packets for a fact check (see chief_fact_packets)."""

        final = value["ledger"]
        run_id = final["run_id"]
        ledger = {m: _rows(rows) for m, rows in final["ledger"].items()}
        packets = chief_fact_packets(
            chapter1=_plain(value.get("chapter1")),
            chapter3=_plain(value.get("chapter3")),
            ledger=ledger,
            groups=final.get("action_groups") or [],
            slots=self._slots(run_id),
            fact_texts=self._reference(run_id).get("fact_texts") or {},
            run_id=run_id,
        )
        self.store.write_json(f"Work/runs/{run_id}/ledger/chief-fact-packets.json", packets)
        return {"fact_items": packets}

    def apply_chief_facts(self, value: dict[str, Any]) -> dict[str, Any]:
        """The fact check's findings and the program's certain text problems, for one fix.

        The chief has no edit pass of its own: what the program finds certain
        in the drawn sections (process words, slots it cannot fill, copied
        instructions, tables written again) joins the fact check, so the chief
        fixes everything on the one version that was checked.
        """

        checked = apply_fact_findings(
            value.get("packets") or [], _plain(value.get("results") or {})
        )
        final = value.get("ledger")
        if final and value.get("chapter1") and value.get("chapter3"):
            run_id = final["run_id"]
            chief = {"chapter1": _plain(value["chapter1"]), "chapter3": _plain(value["chapter3"])}
            texts = self._chief_texts(chief, final)
            hints: dict[str, list[str]] = {}
            program = self._chief_program_findings(run_id, texts, hints_out=hints)
            self._record_hints(run_id, "chief_texts", hints)
            findings = checked["check"]["findings"]
            for section_id, finding in program.items():
                findings[section_id] = _combined(findings.get(section_id), finding)
            checked["check"]["texts"] = texts
            blocking = _blocking(findings)
            checked["check"]["blocking"] = blocking
            checked["blocking"] = blocking
        checked["row_fix_items"] = self._row_fix_items(final, checked.pop("row_fixes", None) or {})
        return checked

    def _row_fix_items(
        self, final: Mapping[str, Any] | None, row_fixes: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """The fact check's findings on rows, one revision item per judging group."""

        if not final or not row_fixes:
            return []
        run_id = final["run_id"]
        ledger = {m: _rows(rows) for m, rows in final["ledger"].items()}
        module_of = {row.id: m for m, rows in ledger.items() for row in rows}
        row_groups = self._groups_file(run_id).get("row_groups") or {}
        items: dict[str, dict[str, Any]] = {}
        for row_id, finding in row_fixes.items():
            if row_id not in module_of:
                continue
            module_id = module_of[row_id]
            group_id = row_groups.get(row_id, module_id)
            item = items.setdefault(group_id, {
                "run_id": run_id,
                "module_id": module_id,
                "group_id": group_id,
                "findings": {},
                "minor": {},
                "fact_check": True,
            })
            item["findings"][row_id] = dict(finding)
        return list(items.values())

    def apply_row_fixes(self, value: dict[str, Any]) -> dict[str, Any]:
        """The fact check's row fixes on the final ledger, and the sections to bring in line.

        A changed row's own section, and every section whose rows relate to it,
        is sent back to its writer with the row before and after.
        """

        final = dict(value["ledger"])
        ledger = {m: _rows(rows) for m, rows in final["ledger"].items()}
        before = {row.id: row for rows in ledger.values() for row in rows}
        changed: dict[str, JudgmentRow] = {}
        for result in (value.get("fixes") or {}).values():
            patch = _plain(result) or {}
            if not patch:
                continue
            new_rows, _removed, _problems = apply_row_patches(
                [changed.get(r.id, r) for r in before.values()], patch
            )
            changed.update({row.id: row for row in new_rows})
        changed = {
            row_id: row for row_id, row in changed.items()
            if row_id in before and row != before[row_id]
        }
        if not changed:
            return {"ledger": final, "section_fix_items": [], "changed_rows": []}
        ledger = {
            m: [changed.get(row.id, row) for row in rows] for m, rows in ledger.items()
        }
        final["ledger"] = {m: _dump_rows(rows) for m, rows in ledger.items()}
        owners = {
            section["submodule_id"]: group["group_id"]
            for group in final.get("writing_items") or []
            for section in group.get("sections") or []
        }
        written = {
            owner: set((edit or {}).get("sections") or {})
            for owner, edit in (value.get("edits") or {}).items()
        }
        per_owner: dict[str, dict[str, Any]] = {}

        def send(section_id: str, row: JudgmentRow) -> None:
            owner = owners.get(section_id)
            if owner is None or section_id not in written.get(owner, set()):
                return
            old = before[row.id]
            fields = [
                name for name in ("finding", "hazard", "risk_level", "action", "basis")
                if getattr(old, name) != getattr(row, name)
            ]
            item = per_owner.setdefault(owner, {"findings": {}, "minor": {}})
            note = (
                f"判断行 {row.id}（{row.object}｜{row.checkpoint}）按事实核对改了"
                + ("、".join(fields) or "措辞")
                + f"。原来的发现：{old.finding}；现在：{row.finding}。"
            )
            earlier = item["findings"].get(section_id)
            item["findings"][section_id] = {
                "problem": f"{earlier['problem']}；{note}" if earlier else note,
                "severity": "blocking",
                "suggestion": "本节正文和风险分析里沿用了原来说法的，改成与现在的判断行一致；没有沿用的原样提交。",
            }

        for row in changed.values():
            send(row.submodule_id, row)
            for rows in ledger.values():
                for other in rows:
                    if other.submodule_id != row.submodule_id and any(
                        r.row_id == row.id for r in other.relations
                    ):
                        send(other.submodule_id, row)
        run_id = final["run_id"]
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/fact-row-fixes.json",
            {
                "rows": _dump_rows(list(changed.values())),
                "sections": {owner: list(item["findings"]) for owner, item in per_owner.items()},
            },
        )
        return {
            "ledger": final,
            "changed_rows": list(changed),
            "section_fix_items": [
                {"owner": owner, "run_id": run_id, **item} for owner, item in per_owner.items()
            ],
        }

    def merge_section_fixes(self, value: dict[str, Any]) -> dict[str, Any]:
        """The writers' revised sections, put into the edited sections."""

        edits = {owner: dict(edit) for owner, edit in (value.get("edits") or {}).items()}
        for owner, revision in (value.get("fixes") or {}).items():
            sections = (_plain(revision) or {}).get("sections") or {}
            if owner not in edits or not sections:
                continue
            merged = dict(edits[owner].get("sections") or {})
            if isinstance(sections, list):
                sections = {s["submodule_id"]: s["text"] for s in sections if isinstance(s, Mapping)}
            for section_id, text in sections.items():
                if section_id in merged:
                    merged[section_id] = text
            edits[owner]["sections"] = merged
        return {"edits": edits}

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
        group_modules = {
            group["group_id"]: group["module_id"]
            for group in value["ledger"].get("writing_items") or []
            if "group_id" in group
        }
        skills = reference.get("author_skills") or {}
        copied = self._copied(run_id)
        edit_hints: dict[str, list[str]] = {}
        for owner, sections in value["written"].items():
            module_id = group_modules.get(owner, owner)
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
                if row.submodule_id in texts
                for entry in related_rows([row], ledger)
            }
            items.append(
                {
                    "run_id": run_id,
                    "owner": owner,
                    "module_id": module_id,
                    "sections": texts,
                    # The editor reads in a conversation of its own: the
                    # settled rows of these sections and the expert method.
                    "rows": [
                        compact_row(row)
                        for row in ledger.get(module_id, [])
                        if row.submodule_id in texts
                    ],
                    "method": skills.get(module_id) or "（本模块没有专家版方法）",
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
                                copied=copied,
                                hints_out=edit_hints,
                            ),
                            texts,
                            by_submodule,
                        ),
                        layout,
                    ),
                }
            )
        # A chief that has been through its own edit pass (ledger-chief: edit,
        # then fact check) is not edited again.
        # Uncertain hints reach the delivery report, not the editor.
        self._record_hints(run_id, "edit_sections", edit_hints)
        chief = value.get("chief")
        if chief and not chief.get("texts"):
            items.append(self._chief_item(run_id, chief, value["ledger"]))
        return {"edit_items": items}

    def _chief_item(
        self, run_id: str, chief: Mapping[str, Any], final: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The chief's text sections for the edit pass, with their program findings."""

        slots = self._slots(run_id)
        fact_texts = self._reference(run_id).get("fact_texts") or {}
        chief_texts = self._chief_texts(chief, final)
        reference = self._reference(run_id)
        return {
            "run_id": run_id,
            "owner": CHIEF_OWNER,
            "sections": chief_texts,
            # Chapters 1 and 3 are read against every flagged row, briefly.
            "rows": [
                brief_row(row)
                for rows in self._final_rows(run_id).values()
                for row in rows
                if row.disposition in {"concern", "undetermined"}
            ],
            "method": reference.get("chief_skill") or "（没有专家版方法）",
            "program_findings": self._chief_text_findings(
                report_check_findings(
                    sections=chief_texts,
                    rows_by_submodule={},
                    slots=slots,
                    quote_sources={
                        section_id: list(fact_texts.values()) for section_id in chief_texts
                    },
                    copied=self._copied(run_id),
                    hints_out={},
                ),
                chief_texts,
            ),
        }

    def apply_chief_fix(self, value: dict[str, Any]) -> dict[str, Any]:
        """Fold the chief's answer to the fact check into chapters 1 and 3.

        The answer holds only what changes: whole text sections (1.1-1.3,
        3.1), chapter 1 in full when one of its tables changes, and the 3.2
        groups that change, by group id.  Everything else is drawn again from
        the chapters.  The program's checks run once more on the result; what
        they find is recorded for the delivery report, nothing goes back.
        """

        final = value["ledger"]
        run_id = final["run_id"]
        fix = ChiefFix.model_validate(_plain(value.get("fix") or {}))
        chapter1 = fix.chapter1.model_dump(mode="json") if fix.chapter1 else dict(value["chapter1"])
        chapter3 = dict(_plain(value["chapter3"]))
        patches = {action.group_id: action.model_dump(mode="json") for action in fix.actions}
        if patches:
            chapter3["actions"] = [
                patches.get(str(action.get("group_id")), action)
                for action in chapter3.get("actions") or []
            ]
        if fix.action_lead.strip():
            chapter3["action_lead"] = fix.action_lead
        drawn = self._chief_texts({"chapter1": chapter1, "chapter3": chapter3}, final)
        texts = {**drawn, **{k: v for k, v in fix.sections.items() if k in drawn}}
        for section_id, text in texts.items():
            if section_id.startswith("1."):
                chapter1[CHIEF_SECTIONS[section_id]] = text
        unknown = sorted(set(patches) - {str(a.get("group_id")) for a in chapter3.get("actions") or []})
        after = self._chief_program_findings(run_id, texts, hints_out=None)
        tables = self.check_chief({"ledger": final, "chapter3": chapter3})["findings"]
        tables.update(self.check_chief({"ledger": final, "chapter3": chapter3, "chapter1": chapter1})["findings"])
        self._record_hints(
            run_id,
            "chief_after_fix",
            {
                **{key: f["problem"] for key, f in {**after, **tables}.items()},
                **({"3.2": "主编返修提交了不存在的行动组：" + "、".join(unknown)} if unknown else {}),
            },
        )
        return {
            "chapter1": chapter1,
            "chapter3": chapter3,
            "texts": texts,
            "declined": dict(fix.declined),
        }

    def _chief_program_findings(
        self, run_id: str, texts: Mapping[str, str], *, hints_out: dict[str, list[str]] | None
    ) -> dict[str, dict[str, Any]]:
        """The program's certain problems in the chief's text sections (see report_check_findings)."""

        fact_texts = self._reference(run_id).get("fact_texts") or {}
        hints: dict[str, list[str]] = {} if hints_out is None else hints_out
        found = self._chief_text_findings(
            report_check_findings(
                sections=texts,
                rows_by_submodule={},
                slots=self._slots(run_id),
                quote_sources={section_id: list(fact_texts.values()) for section_id in texts},
                copied=self._copied(run_id),
                hints_out=hints,
            ),
            dict(texts),
        )
        return {k: f for k, f in found.items() if f.get("severity") == "blocking"}

    def _record_hints(self, run_id: str, scope: str, hints: Mapping[str, Any]) -> None:
        """Keep the program's uncertain hints for the delivery report; they reach no author."""

        path = self.workspace / f"Work/runs/{run_id}/ledger/check-hints.json"
        stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if hints:
            stored[scope] = {
                key: (list(value) if isinstance(value, (list, tuple)) else value)
                for key, value in hints.items()
            }
        else:
            stored.pop(scope, None)
        self.store.write_json(f"Work/runs/{run_id}/ledger/check-hints.json", stored)

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
        copied = self._copied(run_id) if run_id else None
        hints: dict[str, list[str]] = {}
        for section_id, text in changed.items():
            problems: list[str] = []
            if leaked := process_terms(strip_slots(text)):
                problems.append("修订后的正文里出现了内部流程用语或编号：" + "、".join(leaked))
            if copied is not None and (spans := copied.spans(text)):
                problems.append(copied_problem(spans))
            if run_id:
                # A slot was sent back once already (in the revision); what is
                # still off is filled at composition and listed in the delivery report.
                found, uncertain = slot_check(text, slots)
                if found or uncertain:
                    hints[section_id] = [*found, *uncertain]
            if problems:
                findings[section_id] = {
                    "problem": "；".join(problems),
                    "severity": "blocking",
                    "suggestion": "删去这些用语；" + COPIED_SUGGESTION,
                }
        if run_id:
            self._record_hints(str(run_id), f"recheck_{item['owner']}", hints)
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
            **({"module_id": item["module_id"]} if item.get("module_id") else {}),
            "sections": self._revised_sections(
                item, value.get("revision"), value.get("recheck_revision")
            ),
            "findings": value["merged"]["findings"],
            **({"recheck_findings": recheck["findings"]} if recheck.get("findings") else {}),
            **({"declined": declined} if (declined := {
                **((value.get("revision") or {}).get("declined") or {}),
                **((value.get("recheck_revision") or {}).get("declined") or {}),
            }) else {}),
        }

    # -- composition and delivery -----------------------------------------

    def compose_report(self, value: dict[str, Any]) -> dict[str, Any]:
        state = dict(value["state"])
        run_id = str(state["run_id"])
        ledger = {m: _rows(rows) for m, rows in value["ledger"]["ledger"].items()}
        with _taxonomy(state):
            state["evidence_items"] = self._placed_evidence(
                run_id, state.get("evidence_items", []), state.get("photo_assets", []), ledger
            )
        # A later revision starts its chapters 1 and 3 from these.
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/chief-outputs.json", _plain(value["chief"])
        )
        # The program writes every value slot now, in the prose and in the
        # chief's structured fields alike.
        slots = self._slots(run_id)
        # Slots still off after the one revision are filled here (an existing
        # id with its plain value, a missing one as 数值待核) and listed for
        # the delivery report; they block nothing.
        repairs: dict[str, list[str]] = {}

        def note_repairs(owner: str, key: str, text: str) -> None:
            if found := slot_repairs(str(text), slots):
                repairs.setdefault(f"{owner} {key}", []).extend(found)

        for edit in value["edits"].values():
            for key, text in edit["sections"].items():
                note_repairs(edit.get("module_id") or edit["owner"], key, text)
        for key, text in (value["chief"].get("texts") or {}).items():
            note_repairs(CHIEF_OWNER, key, text)
        for key in ("chapter1", "chapter3", "chapter4"):
            for text in _strings(value["chief"].get(key) or {}):
                note_repairs(CHIEF_OWNER, key, text)
        hints_path = self.workspace / f"Work/runs/{run_id}/ledger/check-hints.json"
        check_hints = (
            json.loads(hints_path.read_text(encoding="utf-8")) if hints_path.is_file() else {}
        )
        delivery_hints = {"slot_repairs": repairs, "check_hints": check_hints}
        self.store.write_json(f"Work/runs/{run_id}/ledger/delivery-hints.json", delivery_hints)
        # A module written in several groups is put back together here.
        edits: dict[str, dict[str, str]] = {}
        for edit in value["edits"].values():
            edits.setdefault(edit.get("module_id") or edit["owner"], {}).update(
                {
                    key: fill_slots(text, slots, unresolved=UNRESOLVED)
                    for key, text in edit["sections"].items()
                }
            )
        # The chief's sections as the chief left them; an editor's revision of
        # the chief (runs before the chief lost its edit pass) wins.
        chief_sections = value["chief"].get("texts") or self._chief_texts(
            value["chief"], value["ledger"]
        )
        edits[CHIEF_OWNER] = {
            **{
                key: fill_slots(text, slots, unresolved=UNRESOLVED)
                for key, text in chief_sections.items()
            },
            **edits.get(CHIEF_OWNER, {}),
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
                    "delivery_hints": delivery_hints,
                    "edit_reviews": {
                        owner: edit.get("findings") for owner, edit in value["edits"].items()
                    },
                    "declined": {
                        owner: edit["declined"]
                        for owner, edit in value["edits"].items()
                        if edit.get("declined")
                    },
                    **(
                        {"chief_reviews": {
                            key: value["chief"][key]
                            for key in ("fact_check", "declined")
                            if value["chief"].get(key)
                        }}
                        if value["chief"].get("texts") else {}
                    ),
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

    def _placed_evidence(
        self,
        run_id: str,
        items: Sequence[Any],
        photos: Sequence[Any],
        ledger: Mapping[str, Sequence[JudgmentRow]],
    ) -> list[dict[str, Any]]:
        """Evidence with the sections the authors placed unassigned records in.

        The knowledge base leaves some records without a section; the authors
        judge each in a section of their choice (``placement_findings`` checks
        that).  The first row citing such a record gives it its section, so
        its photos are shown there.  The program only checks that the section
        is in this report's tree; photos of records no row placed are left
        out and listed in ``ledger/unplaced-photos.json``.
        """

        placed: dict[str, tuple[str, str]] = {}
        for module_id, rows in ledger.items():
            submodules = REPORT_TAXONOMY[module_id].submodules
            for row in rows:
                if row.submodule_id not in submodules:
                    continue
                for evidence_id in row.evidence_ids:
                    placed.setdefault(evidence_id, (module_id, row.submodule_id))
        result: list[dict[str, Any]] = []
        for raw in items:
            item = _plain(raw)
            if item.get("submodule_id") is None and item.get("id") in placed:
                module_id, submodule_id = placed[item["id"]]
                item = {**item, "module_id": module_id, "submodule_id": submodule_id}
            result.append(item)
        with_section = {
            photo_id
            for item in result
            if item.get("submodule_id") is not None
            for photo_id in item.get("photo_refs") or ()
        }
        unplaced = [
            {
                "photo_id": photo_id,
                "evidence_ids": [
                    item["id"] for item in result if photo_id in (item.get("photo_refs") or ())
                ],
            }
            for photo_id in (_plain(photo).get("id") for photo in photos)
            if photo_id not in with_section
        ]
        self.store.write_json(
            f"Work/runs/{run_id}/ledger/unplaced-photos.json",
            {
                "note": "这些照片对应的记录没有任何判断行放进小节，报告里不出现，需人工确认。",
                "photos": unplaced,
            },
        )
        return result

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
            "plan-judgment-relate": self.plan_relate,
            "apply-judgment-relations": self.apply_relations,
            "plan-judgment-review": self.plan_review,
            "gather-judgment-reviews": self.gather_reviews,
            "apply-story-lines": self.apply_story,
            "plan-photo-reread": self.plan_reread,
            "apply-photo-reread": self.apply_reread,
            "merge-review-findings": self.merge_findings,
            "recheck-judgments": self.recheck_judgments,
            "close-review": self.close_review,
            "finalize-judgment-ledger": self.finalize_ledger,
            "check-chief-tables": self.check_chief,
            "plan-chief-facts": self.plan_chief_facts,
            "apply-chief-fix": self.apply_chief_fix,
            "apply-chief-facts": self.apply_chief_facts,
            "apply-row-fixes": self.apply_row_fixes,
            "merge-section-fixes": self.merge_section_fixes,
            "check-report": self.check_report,
            "recheck-edit": self.recheck_edit,
            "close-edit": self.close_edit,
            "compose-report": self.compose_report,
        }
        return {tool_id: plain(method) for tool_id, method in methods.items()}


__all__ = ["CHIEF_OWNER", "CHIEF_SECTIONS", "LedgerTools"]
