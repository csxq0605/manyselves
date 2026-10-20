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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from ..domain.calculations import compute, render_calculations, render_statistics, statistics
from ..domain.judgment_ledger import (
    PROBLEM_TABLE_MARKER,
    ChiefAction,
    DimensionHealth,
    JudgmentRow,
    JudgmentSet,
    KeyRisk,
    Phase,
    action_table_markdown,
    apply_revisions,
    chief_findings,
    findings_overview_markdown,
    known_values_from_text,
    ledger_findings,
    ledger_judgments,
    place_problem_table,
    report_check_findings,
)
from ..domain.taxonomy import (
    REPORT_TAXONOMY,
    activate_report_taxonomy,
    reset_report_taxonomy,
)
from .assets import runtime_photo_ids
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
from .observation_ledger import shared_labels
from .photo_reading import load_photo_readings, photo_reading_labels, reading_label
from .research.fact_context import register_fact_sources, render_fact_packet
from .research.knowledge_context import KnowledgeContextBuilder
from .research.project_evidence import ProjectEvidenceIndex
from .storage import ReportingStore
from .template_lengths import matched_lengths, render_length_reference, template_sections
from .template_resolver import resolve_report_template

_ROLES_FILE = Path(__file__).resolve().parents[1] / "domain/module-roles.yaml"
_SKILL_ORDER = (
    "author-2.1",
    "author-2.2",
    "author-2.3",
    "author-2.4",
    "author-2.5",
    "chief-editor-chapter-1",
    "chief-editor-chapter-3",
    "chief-editor-chapter-4",
)
CHIEF_OWNER = "chief"
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
    snapshot = state.get("report_taxonomy")
    if not isinstance(snapshot, Mapping):
        yield
        return
    token = activate_report_taxonomy(snapshot)
    try:
        yield
    finally:
        reset_report_taxonomy(token)


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

    A photo is sent when the program found its copied numbers differ from the
    record, or the reader asked for a person to look at it.
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
        if reading.record_match != "conflicts" and not reading.needs_review:
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


def _blocking(findings: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(finding.get("severity") == "blocking" for finding in findings.values())


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
            counted, statistic_lines = statistics(items, start=len(calculations) + 1)
            roles = yaml.safe_load(_ROLES_FILE.read_text(encoding="utf-8"))
            fact_blocks: list[str] = []
            knowledge_blocks: list[str] = []
            modules: list[dict[str, Any]] = []
            for module_id in module_ids:
                module = REPORT_TAXONOMY[module_id]
                module_items = index.module_items(module_id)
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
            skills = state.get("template_skill_text") or {}
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
                    "# 写作方法与专家版范例\n"
                    + "\n\n".join(
                        f"## {skill_id}\n{skills[skill_id]}"
                        for skill_id in _SKILL_ORDER
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
                    asset["id"]: {"path": asset["path"], "media_type": asset["media_type"]}
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
            ledger[module_id] = list(judgment.rows)
            findings[module_id] = ledger_findings(
                judgment,
                submodule_ids=[sub["id"] for sub in module["submodules"]],
                module_evidence_ids=module["evidence_ids"],
                known_evidence_ids=reference["evidence_ids"],
                known_photo_ids=reference["photo_ids"],
                known_calculation_ids=reference["calculation_ids"],
                evidence_subjects=reference.get("evidence_subjects"),
            )
        dossiers = build_dossiers(self.workspace, run_id, ledger_judgments(ledger))
        readings = load_photo_readings(self.workspace, run_id)
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments-r0.json", dumped)
        return {
            "run_id": run_id,
            "ledger": dumped,
            "snapshot": {"ledger_text": _ledger_text(dumped)},
            "review_items": [
                {
                    "module_id": module["module_id"],
                    "title": module["title"],
                    "program_findings": findings[module["module_id"]],
                    "dossiers": [
                        dossier
                        for dossier in dossiers
                        if module["module_id"] in dossier["module_ids"]
                    ],
                    "photos": photos_to_check(
                        ledger[module["module_id"]], reference, readings
                    ),
                }
                for module in context["modules"]
            ],
        }

    def merge_findings(self, value: dict[str, Any]) -> dict[str, Any]:
        """Keep program and reviewer findings side by side.

        When both name the same target, the finding carries both problems and
        is blocking if either one is.
        """

        item = value["item"]
        reviewed = dict((value.get("review") or {}).get("findings") or {})
        program = dict(item.get("program_findings") or {})
        findings = {
            target: _combined(program.get(target), reviewed.get(target))
            for target in dict.fromkeys([*program, *reviewed])
        }
        return {
            "findings": findings,
            "blocking": _blocking(findings),
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
            revised = _rows(revision.get("rows") or [])
            dropped = list(revision.get("removed_row_ids") or [])
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
        dumped = {module_id: _dump_rows(rows) for module_id, rows in ledger.items()}
        self.store.write_json(f"Work/runs/{run_id}/ledger/judgments.json", dumped)
        self._write_gaps(run_id, ledger)
        writing_items = []
        for module in context["modules"]:
            rows = ledger[module["module_id"]]
            writing_items.append(
                {
                    "module_id": module["module_id"],
                    "title": module["title"],
                    "sections": [
                        {
                            "submodule_id": sub["id"],
                            "title": sub["title"],
                            "rows": [
                                row.model_dump(mode="json", exclude_none=True)
                                for row in rows
                                if row.submodule_id == sub["id"]
                            ],
                            "problem_table_marker": PROBLEM_TABLE_MARKER,
                        }
                        for sub in module["submodules"]
                    ],
                }
            )
        return {
            "run_id": run_id,
            "ledger": dumped,
            "unanswered_findings": unanswered,
            "writing_items": writing_items,
            "chief_input": {
                "changed_rows": changed,
                "removed_row_ids": removed,
                "action_row_ids": [
                    row.id for rows in ledger.values() for row in rows if row.action is not None
                ],
                "module_titles": context["module_titles"],
            },
        }

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
        ledger = {m: _rows(rows) for m, rows in value["ledger"]["ledger"].items()}
        chapter3 = value["chapter3"]
        chapter1 = value.get("chapter1") or {}
        findings = chief_findings(
            actions=[ChiefAction.model_validate(a) for a in chapter3["actions"]],
            health=[DimensionHealth.model_validate(h) for h in chapter1.get("dimension_health", [])],
            risks=[KeyRisk.model_validate(r) for r in chapter1.get("key_risks", [])],
            rows_by_module=ledger,
            # Each check reviews the chapter just written, which its revision can fix.
            texts={
                field: text
                for field, text in (chapter1 or chapter3).items()
                if isinstance(text, str)
            },
        )
        if not chapter1:
            findings.pop("1.2", None)
        return {"findings": findings, "blocking": _blocking(findings)}

    def check_report(self, value: dict[str, Any]) -> dict[str, Any]:
        run_id = value["ledger"]["run_id"]
        reference = self._reference(run_id)
        known = [tuple(pair) for pair in reference["known_values"]]
        ledger = {m: _rows(rows) for m, rows in value["ledger"]["ledger"].items()}
        by_submodule: dict[str, list[JudgmentRow]] = {}
        for rows in ledger.values():
            for row in rows:
                by_submodule.setdefault(row.submodule_id, []).append(row)
        items = []
        for module_id, sections in value["written"].items():
            texts = {section["submodule_id"]: section["text"] for section in sections}
            items.append(
                {
                    "owner": module_id,
                    "sections": texts,
                    "program_findings": report_check_findings(
                        sections=texts,
                        rows_by_submodule=by_submodule,
                        known_values=known,
                        subject_values=reference.get("subject_values"),
                    ),
                }
            )
        chief = value["chief"]
        chief_texts = {
            section_id: chief["chapter1" if section_id.startswith("1.") else "chapter3"][field]
            for section_id, field in CHIEF_SECTIONS.items()
        }
        # Chapter 1 and 3 count across the whole ledger and their own actions.
        all_rows = [row for rows in ledger.values() for row in rows]
        actions = chief["chapter3"].get("actions") or []
        counts = {
            float(len(actions)),
            float(len(chief["chapter1"].get("key_risks") or [])),
            *(
                float(len([a for a in actions if a.get("priority") == p]))
                for p in {a.get("priority") for a in actions}
            ),
        }
        for scope in [all_rows, *ledger.values()]:
            for disposition in ("concern", "undetermined"):
                picked = [row for row in scope if row.disposition == disposition]
                counts.update({float(len(picked)), float(len({row.object for row in picked}))})
        cited = {
            row_id
            for entry in [*actions, *(chief["chapter1"].get("key_risks") or [])]
            for row_id in entry.get("row_ids") or []
        }
        items.append(
            {
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
                "program_findings": report_check_findings(
                    sections=chief_texts,
                    rows_by_submodule={},
                    known_values=[*known, *((count, "个") for count in counts)],
                    subject_values=reference.get("subject_values"),
                ),
            }
        )
        return {"edit_items": items}

    def close_edit(self, value: dict[str, Any]) -> dict[str, Any]:
        item = value["item"]
        sections = dict(item["sections"])
        revision = value.get("revision") or {}
        sections.update(
            {key: text for key, text in (revision.get("sections") or {}).items() if key in sections}
        )
        return {
            "owner": item["owner"],
            "sections": sections,
            "findings": value["merged"]["findings"],
        }

    # -- composition and delivery -----------------------------------------

    def compose_report(self, value: dict[str, Any]) -> dict[str, Any]:
        state = dict(value["state"])
        run_id = str(state["run_id"])
        ledger = {m: _rows(rows) for m, rows in value["ledger"]["ledger"].items()}
        edits = {edit["owner"]: edit["sections"] for edit in value["edits"].values()}
        chief = value["chief"]
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
                    [ChiefAction.model_validate(a) for a in chapter3["actions"]],
                    row_status,
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
            "merge-review-findings": self.merge_findings,
            "close-review": self.close_review,
            "finalize-judgment-ledger": self.finalize_ledger,
            "check-chief-tables": self.check_chief,
            "check-report": self.check_report,
            "close-edit": self.close_edit,
            "compose-report": self.compose_report,
        }
        return {tool_id: plain(method) for tool_id, method in methods.items()}


__all__ = ["CHIEF_OWNER", "CHIEF_SECTIONS", "LedgerTools"]
