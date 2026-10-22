"""Triage a finished report run's review findings into lessons and other routes.

The findings come from the run's own reviewers and editors (their agent
results), plus optional notes a person wrote.  The program checks each lesson
draft (no object of the run, no numbers, no mandatory words, a known role, a
valid trigger, not a duplicate of an adopted lesson) and writes the drafts
that pass as candidates beside the triage run.  Adopting a candidate into
``domain/writing-lessons.yaml`` is a person's step; the fixed-structure
variant test then checks every adopted trigger.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from manyselves.runtime.exemplar.lessons import (
    LessonBook,
    lesson_problems,
    next_lesson_ids,
    specific_terms,
)

from ..domain.lesson_triage import ROUTE_HELP, TRIGGER_HELP, LessonTriage
from ..domain.taxonomy import REPORT_TAXONOMY
from .models.reporting import EvidenceItem
from .template_skills import (
    plain_value,
    template_roles,
    trigger_fires,
    trigger_problem,
    writing_lessons,
)

_SPACE = re.compile(r"\s+")
FINDING_ACTIONS = ("review", "edit")


def lesson_root(run_id: str) -> str:
    return f"Work/runs/{run_id}/lessons"


def reporting_run_id(workspace: Path, source_run_id: str) -> str:
    """The report state's run id, which may differ from the workflow run id."""

    path = workspace / f"Work/runs/{source_run_id}/runtime-state.json"
    if path.is_file():
        variables = json.loads(path.read_text(encoding="utf-8")).get("variables") or {}
        for name in ("reporting-state", "state"):
            value = variables.get(name)
            if isinstance(value, Mapping) and value.get("run_id"):
                return str(value["run_id"])
    return source_run_id


def collect_findings(workspace: Path, source_run_id: str) -> list[dict[str, Any]]:
    """Reviewer and editor findings of one run, in a stable order."""

    root = workspace / f"Work/runs/{source_run_id}/agent-results"
    found: list[dict[str, Any]] = []
    for conversation in sorted(root.iterdir()) if root.is_dir() else ():
        for action in FINDING_ACTIONS:
            # Result files are named by the action id, plus "--<task id>"
            # when the task id differs from it.
            paths = [conversation / f"{action}.json"]
            paths += sorted(conversation.glob(f"{action}--*.json"))
            path = next((p for p in paths if p.is_file()), None)
            if path is None:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            result = record.get("result") or {}
            source = record.get("input") or {}
            owner = str(source.get("module_id") or source.get("owner") or conversation.name)
            for target, finding in sorted((result.get("findings") or {}).items()):
                if not isinstance(finding, Mapping) or not finding.get("problem"):
                    continue
                found.append(
                    {
                        "origin": "审查" if action == "review" else "编辑",
                        "owner": owner,
                        "target": str(target),
                        "severity": str(finding.get("severity") or ""),
                        "problem": str(finding["problem"]),
                        "suggestion": str(finding.get("suggestion") or ""),
                    }
                )
    return [{"id": f"F-{index:03d}", **item} for index, item in enumerate(found, start=1)]


def _notes(workspace: Path, notes_ref: str) -> list[dict[str, Any]]:
    if not notes_ref:
        return []
    path = (workspace / notes_ref).resolve()
    if not path.is_relative_to(workspace) or not path.is_file():
        raise ValueError(f"notes must be a text file inside the project: {notes_ref}")
    lines = [line.strip(" -*\t") for line in path.read_text(encoding="utf-8").splitlines()]
    return [
        {"id": f"N-{index:03d}", "origin": "人工评审", "problem": line}
        for index, line in enumerate((line for line in lines if line), start=1)
    ]


def run_terms(workspace: Path, reporting_run_id: str) -> list[str]:
    """Object names of the run that a general lesson must not mention."""

    path = workspace / f"Work/runs/{reporting_run_id}/preparation/evidence.jsonl"
    subjects = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                subjects.append(EvidenceItem.model_validate_json(line).subject)
    vocabulary = [
        *(module.title for module in REPORT_TAXONOMY.values()),
        *(sub.title for module in REPORT_TAXONOMY.values() for sub in module.submodules.values()),
        *(role.title for role in template_roles()),
    ]
    return sorted(specific_terms(subjects, vocabulary))


class LessonTriageTools:
    """Tool implementations for ``triage-writing-lessons`` in one workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

    def _write(self, ref: str, text: str) -> None:
        path = self.workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def prepare(self, value: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(value["run_id"])
        request = value.get("request") or {}
        source = str(request.get("source_run_id") or "").strip()
        if not source or not (self.workspace / f"Work/runs/{source}").is_dir():
            raise ValueError(f"source run not found: {source}")
        reporting = reporting_run_id(self.workspace, source)
        findings = [
            *collect_findings(self.workspace, source),
            *_notes(self.workspace, str(request.get("notes_ref") or "")),
        ]
        terms = run_terms(self.workspace, reporting)
        root = lesson_root(run_id)
        self._write(f"{root}/run-terms.json", json.dumps(terms, ensure_ascii=False, indent=2))
        book = writing_lessons()
        packet = {
            "source_run_id": source,
            "findings": findings,
            "roles": [{"id": role.id, "title": role.title} for role in template_roles()],
            "adopted_lessons": [
                {"id": lesson.id, "error_type": lesson.error_type, "rule": lesson.rule}
                for lesson in book.lessons
                if lesson.status == "active"
            ],
            "routes": ROUTE_HELP,
            "triggers": TRIGGER_HELP,
        }
        return {
            "run_id": run_id,
            "source_run_id": source,
            "reporting_run_id": reporting,
            "packet": packet,
            "terms_ref": f"{root}/run-terms.json",
        }

    def _check(self, context: Mapping[str, Any], triage: Any) -> dict[str, Any]:
        parsed = LessonTriage.model_validate(triage or {})
        terms = json.loads((self.workspace / context["terms_ref"]).read_text(encoding="utf-8"))
        known_ids = {finding["id"] for finding in context["packet"]["findings"]}
        role_ids = [role.id for role in template_roles()]
        adopted = {
            _SPACE.sub("", lesson.error_type): lesson.id
            for lesson in writing_lessons().lessons
            if lesson.status == "active"
        }
        routed: set[str] = set()
        problems: list[dict[str, Any]] = []
        lessons: list[dict[str, Any]] = []
        for index, item in enumerate(parsed.items):
            reasons: list[str] = []
            unknown = [fid for fid in item.finding_ids if fid not in known_ids]
            if unknown:
                reasons.append("意见编号不存在：" + "、".join(unknown))
            routed.update(item.finding_ids)
            if item.lesson is not None:
                draft = item.lesson
                reasons.extend(
                    lesson_problems(
                        roles=draft.roles,
                        error_type=draft.error_type,
                        rule=draft.rule,
                        trigger=draft.trigger.model_dump(),
                        known_roles=role_ids,
                        run_terms=terms,
                        trigger_problem=trigger_problem,
                    )
                )
                same = adopted.get(_SPACE.sub("", draft.error_type))
                if same:
                    reasons.append(f"与已采用的教训 {same} 重复")
                if not reasons:
                    lessons.append({"index": index, **item.model_dump(mode="json")})
            if reasons:
                problems.append({"index": index, "finding_ids": item.finding_ids, "reasons": reasons})
        unrouted = sorted(known_ids - routed)
        return {
            "passed": not problems and not unrouted,
            "revision_input": {"problems": problems, "unrouted_findings": unrouted},
            "record": {
                "items": [item.model_dump(mode="json") for item in parsed.items],
                "lessons": lessons,
                "problems": problems,
                "unrouted": unrouted,
            },
        }

    def check(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return self._check(value["context"], value.get("triage"))

    def close(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Write the routes and the lesson candidates that passed every check."""

        context = value["context"]
        revision = value.get("revision")
        checked = value["checked"] if revision is None else self._check(context, revision)
        record = checked["record"]
        run_id = str(context["run_id"])
        root = lesson_root(run_id)
        drafts = record["lessons"]
        ids = next_lesson_ids(writing_lessons(), len(drafts))
        candidates = LessonBook.model_validate(
            {
                "lessons": [
                    {
                        "id": lesson_id,
                        "roles": draft["lesson"]["roles"],
                        "error_type": draft["lesson"]["error_type"],
                        "rule": draft["lesson"]["rule"],
                        "trigger": {
                            key: val
                            for key, val in draft["lesson"]["trigger"].items()
                            if val not in ("", None)
                        },
                        "source": {
                            "run": context["source_run_id"],
                            "finding": "、".join(draft["finding_ids"]),
                            "date": date.today().isoformat(),
                        },
                    }
                    for lesson_id, draft in zip(ids, drafts, strict=True)
                ]
            }
        )
        candidates_ref = f"{root}/candidates.yaml"
        self._write(
            candidates_ref,
            yaml.safe_dump(candidates.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        )
        routes: dict[str, list[dict[str, Any]]] = {}
        findings = {finding["id"]: finding for finding in context["packet"]["findings"]}
        for item in record["items"]:
            routes.setdefault(item["route"], []).append(
                {
                    "findings": [findings.get(fid, {"id": fid}) for fid in item["finding_ids"]],
                    "reason": item["reason"],
                }
            )
        triage_ref = f"{root}/triage.json"
        summary = {
            "run_id": run_id,
            "source_run_id": context["source_run_id"],
            "candidates_ref": candidates_ref,
            "triage_ref": triage_ref,
            "lesson_candidates": len(candidates.lessons),
            "routes": {route: len(entries) for route, entries in routes.items()},
            "refused": record["problems"],
            "unrouted": record["unrouted"],
            "revised": revision is not None,
        }
        self._write(
            triage_ref,
            json.dumps({**summary, "detail": routes}, ensure_ascii=False, indent=2) + "\n",
        )
        return summary

    def implementations(self) -> dict[str, Any]:
        def plain(method: Any) -> Any:
            return lambda value: method(plain_value(value))

        return {
            "prepare-lesson-triage": plain(self.prepare),
            "check-lesson-triage": plain(self.check),
            "close-lesson-triage": plain(self.close),
        }


def lesson_admission(
    book: LessonBook,
    variants: Mapping[str, tuple[Sequence[Any], Sequence[Mapping[str, Any]]]],
    *,
    run_terms: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    """For every active lesson: its problems and the variants its trigger fires on.

    ``variants`` maps a fixed-structure variant name to its mapped evidence
    and mapping gaps.  A lesson is admissible when it has no problem and its
    trigger fires on at least one variant, so the rule still means something
    on data other than the run it came from.
    """

    role_ids = [role.id for role in template_roles()]
    report: dict[str, dict[str, Any]] = {}
    for lesson in book.lessons:
        if lesson.status != "active":
            continue
        problems = lesson_problems(
            roles=lesson.roles,
            error_type=lesson.error_type,
            rule=lesson.rule,
            trigger=lesson.trigger,
            known_roles=role_ids,
            run_terms=run_terms,
            trigger_problem=trigger_problem,
        )
        fires = [
            name
            for name, (items, gaps) in variants.items()
            if trigger_fires(lesson.trigger, items=items, mapping_gaps=gaps)
        ]
        if not fires:
            problems.append("触发条件在任何固定结构变体上都不成立")
        report[lesson.id] = {"admissible": not problems, "problems": problems, "fires_on": fires}
    return report


__all__ = [
    "LessonTriageTools",
    "collect_findings",
    "lesson_admission",
    "lesson_root",
    "run_terms",
]
