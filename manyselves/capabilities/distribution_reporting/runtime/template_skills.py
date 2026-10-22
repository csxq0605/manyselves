"""Template role Skills of this capability, on the generic exemplar mechanism.

The capability supplies what is specific to the report: the declared roles
and their template sections (``domain/template-skill-roles.yaml``), the
policy words, where Skills are published, and what a lesson trigger means on
a report run.  Outline, packets, acceptance, rendering and lesson checks are
the generic ``manyselves.runtime.exemplar`` mechanism.

Extraction writes candidates under ``Work/runs/<run>/template-skills``; they
reach ``Inputs`` only through :func:`publish_template_skills`, after a person
has read the diff.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args

import yaml

from manyselves.runtime.exemplar.compare import compare_items, render_comparison, skill_items
from manyselves.runtime.exemplar.extraction import (
    ExemplarPolicy,
    ExemplarRole,
    ExemplarSubmission,
    Target,
    accept_submission,
    acceptance_record,
    parse_roles,
    render_role_skill,
    revision_request,
    role_packet,
    role_scope,
    text_diff,
)
from manyselves.runtime.exemplar.lessons import (
    LessonBook,
    lessons_for,
    load_lessons,
    render_lessons,
)
from manyselves.runtime.exemplar.outline import DocumentOutline, document_outline, similar

from ..domain.calculations import compute
from ..domain.lesson_triage import TriggerKind
from ..domain.taxonomy import REPORT_TAXONOMY
from .template_access import FORBIDDEN_AGENT_DOCUMENT_NAME
from .template_skill_paths import TEMPLATE_SKILL_ROOT, TEMPLATE_SKILL_SOURCE, template_skill_ref

_CAPABILITY = Path(__file__).resolve().parents[1]
_DOMAIN = _CAPABILITY / "domain"
ROLES_FILE = _DOMAIN / "template-skill-roles.yaml"
LESSONS_FILE = _DOMAIN / "writing-lessons.yaml"
TRIGGER_KINDS: tuple[str, ...] = get_args(TriggerKind)
# The finished expert report the methods are learned from; rendering uses a
# separate style template, which has none of this content.
EXPERT_REPORT_REF = f"Templates/{FORBIDDEN_AGENT_DOCUMENT_NAME}"
FACT_FLAGS = ("needs_confirmation", "summary_entry")


@lru_cache(maxsize=1)
def _roles() -> tuple[tuple[ExemplarRole, ...], ExemplarPolicy]:
    roles, policy = parse_roles(yaml.safe_load(ROLES_FILE.read_text(encoding="utf-8")) or {})
    return tuple(roles), policy


@lru_cache(maxsize=1)
def _declared_targets() -> tuple[Target, ...]:
    data = yaml.safe_load(ROLES_FILE.read_text(encoding="utf-8")) or {}
    return tuple(
        Target(str(item["number"]), str(item["title"])) for item in data.get("targets", [])
    )


def skill_targets() -> tuple[Target, ...]:
    """Our own section headings the Skills are aligned to: chapters 1 and 3, and the taxonomy."""

    chapter_2 = [Target("2", "")]
    for module in REPORT_TAXONOMY.values():
        chapter_2.append(Target(module.id, module.title))
        chapter_2.extend(Target(section.id, section.title) for section in module.sections.values())
    return (*_declared_targets(), *chapter_2)


# -- aligning Skills to this run's section tree --------------------------------

_SKILL_SECTION = re.compile(r"^##\s+(2\.\d+(?:\.\d+)*)\s+(.*?)\s*$")
_SAME_SECTION = 0.5  # the bar distillation uses to align an expert section


def _best(title: str, candidates: Mapping[str, str]) -> str | None:
    """The candidate id whose title is most like ``title``, when alike enough."""

    ranked = sorted(
        ((similar(title, other), key) for key, other in candidates.items()), reverse=True
    )
    if ranked and ranked[0][0] >= _SAME_SECTION:
        return ranked[0][1]
    return None


def module_skill_roles() -> dict[str, str]:
    """This run's module id -> the author Skill whose module means the same.

    A Skill learns from one chapter 2 module of the expert report.  The run's
    table may number or word its modules differently, so the module is
    matched by title; the number alone decides only when titles agree.
    """

    modules = {module.id: module.title for module in REPORT_TAXONOMY.values()}
    chosen: dict[str, str] = {}
    for role in template_roles():
        refs = [ref for ref in role.sections if re.fullmatch(r"2\.\d+", ref.number)]
        if len(refs) != 1:
            continue
        ref = refs[0]
        if ref.number in modules and similar(ref.title, modules[ref.number]) >= _SAME_SECTION:
            module_id = ref.number
        else:
            module_id = _best(ref.title, modules)
        if module_id is not None:
            chosen.setdefault(module_id, role.id)
    return chosen


def align_skill_sections(text: str, module_id: str) -> tuple[str, list[str]]:
    """Head a module Skill's sections with this run's numbers and titles.

    Each "## number title" section is matched to the run's subsection of the
    same meaning, number first when the titles agree; a section the run's
    table does not list is left out, since nothing is written for it.
    Returns the aligned text and what was renumbered or left out.
    """

    module = REPORT_TAXONOMY[module_id]
    sections = {section.id: section.title for section in module.sections.values()}
    notes: list[str] = []
    out: list[str] = []
    keep = True
    for line in text.splitlines():
        match = _SKILL_SECTION.match(line)
        if match is None:
            if line.startswith("## "):
                keep = True
            if keep:
                out.append(line)
            continue
        number, title = match.groups()
        if number.count(".") == 1:
            # The module's own heading: the Skill's module is this run's module.
            keep = True
            out.append(f"## {module.id} {module.title}")
            continue
        if number in sections and similar(title, sections[number]) >= _SAME_SECTION:
            target = number
        else:
            target = _best(title, sections)
        keep = target is not None
        if target is None:
            notes.append(f"{number} {title}：本次评估信息汇总表没有这一节，略去")
            continue
        if target != number:
            notes.append(f"{number} {title} → 本次 {target} {sections[target]}")
        out.append(f"## {target} {sections[target]}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), notes


def skills_for_run(skills: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Skills as this run reads them, and each module's author Skill.

    Returns (Skills by role, with module Skills aligned to the run's tree;
    the author Skill text per run module; what alignment changed).
    """

    aligned = dict(skills)
    by_module: dict[str, str] = {}
    notes: list[str] = []
    for module_id, role_id in module_skill_roles().items():
        if not skills.get(role_id):
            continue
        text, changed = align_skill_sections(skills[role_id], module_id)
        aligned[role_id] = text
        by_module[module_id] = text
        notes.extend(f"{role_id}（本次 {module_id}）：{note}" for note in changed)
    matched = set(module_skill_roles().values())
    for role in template_roles():
        refs = [ref for ref in role.sections if re.fullmatch(r"2\.\d+", ref.number)]
        if len(refs) == 1 and role.id not in matched and skills.get(role.id):
            # Its module is not in this run's table: nothing is written for it.
            aligned.pop(role.id, None)
            notes.append(f"{role.id}：本次评估信息汇总表没有对应模块，略去")
    return aligned, by_module, notes


def template_roles() -> tuple[ExemplarRole, ...]:
    return _roles()[0]


def template_policy() -> ExemplarPolicy:
    return _roles()[1]


def skill_order() -> tuple[str, ...]:
    """The role Skills the report loads, in the declared order."""

    return tuple(role.id for role in template_roles())


def _role(role_id: str) -> ExemplarRole:
    role = next((role for role in template_roles() if role.id == role_id), None)
    if role is None:
        raise ValueError(f"unknown template role: {role_id}")
    return role


def run_root(run_id: str) -> str:
    return f"Work/runs/{run_id}/template-skills"


# -- lesson triggers --------------------------------------------------------


def trigger_problem(trigger: Mapping[str, Any]) -> str | None:
    kind = trigger.get("kind")
    if kind not in TRIGGER_KINDS:
        return "触发条件只能是：" + "、".join(TRIGGER_KINDS)
    value = str(trigger.get("value") or "")
    if kind == "mapping_gap" and not value:
        return "mapping_gap 触发条件要写缺口代码"
    if kind == "fact_flag" and value not in FACT_FLAGS:
        return "fact_flag 只能是：" + "、".join(FACT_FLAGS)
    return None


def trigger_fires(
    trigger: Mapping[str, Any],
    *,
    items: Sequence[Any],
    mapping_gaps: Sequence[Mapping[str, Any]],
    calculation_gaps: Sequence[Any] | None = None,
) -> bool:
    """Whether a lesson applies to a run, judged on its prepared data only."""

    kind = trigger.get("kind")
    value = str(trigger.get("value") or "")
    if kind == "always":
        return True
    if kind == "mapping_gap":
        return any(str(gap.get("code")) == value for gap in mapping_gaps)
    if kind == "fact_flag":
        return any(bool(getattr(item, value, False)) for item in items)
    if kind == "calculation_gap":
        gaps = calculation_gaps if calculation_gaps is not None else compute(items)[1]
        return bool(gaps)
    return False


def writing_lessons() -> LessonBook:
    return load_lessons(LESSONS_FILE)


def skills_with_lessons(
    skills: Mapping[str, str],
    *,
    items: Sequence[Any],
    mapping_gaps: Sequence[Mapping[str, Any]],
    calculation_gaps: Sequence[Any] | None = None,
    book: LessonBook | None = None,
) -> dict[str, str]:
    """Each role Skill followed by the active lessons whose trigger fires on this run."""

    book = book if book is not None else writing_lessons()
    merged = dict(skills)
    for role_id in skill_order():
        chosen = lessons_for(
            book,
            role_id,
            lambda trigger: trigger_fires(
                trigger,
                items=items,
                mapping_gaps=mapping_gaps,
                calculation_gaps=calculation_gaps,
            ),
        )
        if chosen and merged.get(role_id):
            merged[role_id] = merged[role_id].rstrip("\n") + "\n" + render_lessons(chosen)
    return merged


# -- extraction tools -------------------------------------------------------


class TemplateSkillTools:
    """Tool implementations for ``extract-template-skills`` in one workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

    def _write(self, ref: str, text: str) -> None:
        path = self.workspace / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_json(self, ref: str, value: Any) -> None:
        self._write(ref, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _outline(self, template_ref: str) -> DocumentOutline:
        return document_outline(self.workspace / template_ref)

    def prepare(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Freeze the template, outline it and build one packet per extracted role."""

        run_id = str(value["run_id"])
        request = value.get("request") or {}
        requested = str(request.get("template_ref") or "").strip() or EXPERT_REPORT_REF
        source = (self.workspace / requested).resolve()
        if not source.is_relative_to(self.workspace) or not source.is_file():
            raise ValueError(
                f"the expert report must be a DOCX file inside the project: {requested}"
            )
        origin = requested
        wanted = [str(role) for role in request.get("roles") or []]
        unknown = sorted(set(wanted) - {role.id for role in template_roles() if role.extract})
        if unknown:
            raise ValueError("unknown or non-extracted template roles: " + ", ".join(unknown))
        root = run_root(run_id)
        template_ref = f"{root}/template.docx"
        target = self.workspace / template_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        outline = self._outline(template_ref)
        packets: list[dict[str, Any]] = []
        missing: dict[str, list[str]] = {}
        for role in template_roles():
            if not role.extract or (wanted and role.id not in wanted):
                continue
            scope = role_scope(outline, role, skill_targets())
            if scope.missing:
                missing[role.id] = list(scope.missing)
            if scope.sections:
                packets.append(role_packet(outline, scope, template_policy()))
        self._write_json(f"{root}/packets.json", packets)
        return {
            "run_id": run_id,
            "template_ref": template_ref,
            "template_origin": origin,
            "roles": packets,
            "missing_sections": missing,
        }

    def _acceptance(self, template_ref: str, role_id: str, submission: Any):
        outline = self._outline(template_ref)
        scope = role_scope(outline, _role(role_id), skill_targets())
        parsed = ExemplarSubmission.model_validate(submission or {})
        return outline, scope, accept_submission(outline, scope, parsed, template_policy())

    def accept(self, value: Mapping[str, Any]) -> dict[str, Any]:
        role_id = str(value["item"]["role_id"])
        _outline, _scope, acceptance = self._acceptance(
            str(value["template_ref"]), role_id, value.get("submission")
        )
        return {
            "role_id": role_id,
            "passed": acceptance.passed,
            "revision_input": revision_request(acceptance),
            "record": acceptance_record(acceptance),
        }

    def close(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """The role's final entries: the revision when there is one, else the first answer."""

        accepted = value["accepted"]
        role_id = str(accepted["role_id"])
        revision = value.get("revision")
        if revision is None:
            return {"role_id": role_id, "revised": False, **accepted["record"]}
        _outline, _scope, acceptance = self._acceptance(
            str(value["template_ref"]), role_id, revision
        )
        return {"role_id": role_id, "revised": True, **acceptance_record(acceptance)}

    def render(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Render candidate Skills and a diff against the published ones."""

        context = value["context"]
        run_id = str(context["run_id"])
        template_ref = str(context["template_ref"])
        outline = self._outline(template_ref)
        root = run_root(run_id)
        extractions = value.get("extractions") or {}
        candidates: dict[str, str] = {}
        roles: dict[str, Any] = {}
        diffs: list[str] = []
        for role in template_roles():
            result = extractions.get(role.id)
            if not role.extract or result is None:
                continue
            scope = role_scope(outline, role, skill_targets())
            entries = ExemplarSubmission.model_validate(
                {"entries": result.get("accepted", []), "skipped": result.get("skipped", [])}
            )
            text = render_role_skill(
                outline,
                scope,
                entries.entries,
                source=f"专家版模板（{context.get('template_origin', '')}），提炼运行 {run_id}",
                policy=template_policy(),
                skipped=entries.skipped,
            )
            ref = f"{root}/{role.id}/SKILL.md"
            self._write(ref, text)
            self._write_json(f"{root}/{role.id}/entries.json", result)
            candidates[role.id] = ref
            published = self.workspace / template_skill_ref(role.id)
            old = published.read_text(encoding="utf-8") if published.is_file() else ""
            diffs.append(text_diff(old, text, name=role.id) or f"{role.id}：与现用版本相同\n")
            roles[role.id] = {
                "accepted": len(result.get("accepted", [])),
                "rejected": len(result.get("rejected", [])),
                "uncovered": result.get("uncovered", []),
                "skipped": [skip["section"] for skip in result.get("skipped", [])],
                "revised": bool(result.get("revised")),
            }
        errors = {
            str(key): str(error) for key, error in (value.get("errors") or {}).items()
        }
        diff_ref = f"{root}/diff.md"
        self._write(diff_ref, "\n".join(diffs))
        summary = {
            "run_id": run_id,
            "template_ref": template_ref,
            "template_origin": context.get("template_origin", ""),
            "candidates": candidates,
            "roles": roles,
            "missing_sections": context.get("missing_sections", {}),
            "failed_roles": errors,
            "diff_ref": diff_ref,
            "published": False,
        }
        summary_ref = f"{root}/summary.json"
        self._write_json(summary_ref, summary)
        comparison = compare_with_published(self.workspace, run_id)
        summary.update({"compare_ref": comparison["compare_ref"], "compare_counts": comparison["counts"]})
        self._write_json(summary_ref, summary)
        return {**summary, "summary_ref": summary_ref}

    def implementations(self) -> dict[str, Any]:
        def plain(method: Any) -> Any:
            return lambda value: method(plain_value(value))

        return {
            "prepare-template-extraction": plain(self.prepare),
            "accept-template-entries": plain(self.accept),
            "close-template-role": plain(self.close),
            "render-template-skills": plain(self.render),
        }


def plain_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_value(item) for item in value]
    return value


BOUNDARY_STATEMENT = (
    "程序从专家版报告提炼的方法资料：只迁移判断、写法、章节综合和核对的做法，"
    "每条带专家版出处坐标；不迁移专家版项目的客户、地点、对象、数值、阈值、发现和结论，"
    "这些只能来自本项目的表格、照片和知识库。任务合同决定范围，Skill 只是参考。"
)


def skill_boundary() -> dict[str, Any]:
    """The boundary every published package carries: method only, no exemplar facts."""

    from .models.agentic import (
        TEMPLATE_SKILL_EXCLUSION_CATEGORIES,
        TEMPLATE_SKILL_TRANSFER_CATEGORIES,
    )

    return {
        "policy_version": 1,
        "transferred_categories": sorted(TEMPLATE_SKILL_TRANSFER_CATEGORIES),
        "excluded_categories": sorted(TEMPLATE_SKILL_EXCLUSION_CATEGORIES),
        "boundary_statement": BOUNDARY_STATEMENT,
    }


def publish_template_skills(workspace: Path, run_id: str) -> dict[str, Any]:
    """Replace the published package with exactly the declared roles.

    The Skills directory is emptied first, so nothing a past flow or a manual
    edition left behind stays in it; what was there is moved beside the run.
    Each declared role takes this run's candidate, or, when this run redid
    only some roles, the copy published before (itself a program output).
    """

    workspace = Path(workspace).resolve()
    root = workspace / run_root(run_id)
    summary_path = root / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    candidates: dict[str, str] = summary["candidates"]
    target_root = workspace / TEMPLATE_SKILL_ROOT
    previous = root / "previous"
    texts: dict[str, str] = {}
    origins: dict[str, str] = {}
    old_source: dict[str, Any] = {}
    if (workspace / TEMPLATE_SKILL_SOURCE).is_file():
        old_source = json.loads((workspace / TEMPLATE_SKILL_SOURCE).read_text(encoding="utf-8"))
    missing: list[str] = []
    for role_id in skill_order():
        if role_id in candidates:
            texts[role_id] = (workspace / candidates[role_id]).read_text(encoding="utf-8")
            origins[role_id] = run_id
            continue
        kept = workspace / template_skill_ref(role_id)
        origin = (old_source.get("extracted_roles") or {}).get(role_id, {}).get("run_id")
        if kept.is_file() and origin:
            texts[role_id] = kept.read_text(encoding="utf-8")
            origins[role_id] = origin
        else:
            missing.append(role_id)
    if missing:
        raise ValueError(
            "这些角色既没有本次提炼的结果，也没有程序提炼过的现用版本：" + "、".join(missing)
        )
    if target_root.exists():
        # A second publish of the same run keeps the first backup too.
        number = 1
        while previous.exists():
            number += 1
            previous = root / f"previous-{number}"
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target_root), str(previous))
    for role_id, text in texts.items():
        target = workspace / template_skill_ref(role_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (target_root / "boundary.json").write_text(
        json.dumps(skill_boundary(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    today = date.today().isoformat()
    source = {
        "source": "program-extraction",
        "skill_root": TEMPLATE_SKILL_ROOT.as_posix(),
        "boundary_ref": (TEMPLATE_SKILL_ROOT / "boundary.json").as_posix(),
        "roles": list(texts),
        "extracted_roles": {
            role_id: (
                {"run_id": run_id, "date": today}
                if origin == run_id
                else (old_source.get("extracted_roles") or {})[role_id]
            )
            for role_id, origin in origins.items()
        },
        "last_extraction": {
            "run_id": run_id,
            "template": summary.get("template_origin", ""),
            "roles": sorted(candidates),
        },
    }
    (workspace / TEMPLATE_SKILL_SOURCE).write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    published = {role_id: template_skill_ref(role_id).as_posix() for role_id in texts}
    summary["published"] = sorted(candidates)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "published": published,
        "from_this_run": sorted(candidates),
        "backup": previous.relative_to(workspace).as_posix(),
    }


# -- comparison with the Skills in use -------------------------------------

# The report steps whose task contracts state rules a Skill no longer needs to repeat.
_RULE_WORKFLOWS = ("report-ledger.yaml", "ledger-*.yaml")
_RULE_AGENT = "report-consultant"


def stated_rules() -> list[tuple[str, str]]:
    """(source, rule) for every objective and constraint the report steps already state."""

    workflows = _CAPABILITY / "workflows"
    tasks: list[str] = []
    for pattern in _RULE_WORKFLOWS:
        for path in sorted(workflows.glob(pattern)):
            for action in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("actions", []):
                if action.get("task") and action["task"] not in tasks:
                    tasks.append(action["task"])
    rules: list[tuple[str, str]] = []
    for task_id in tasks:
        task = yaml.safe_load((_CAPABILITY / "tasks" / f"{task_id}.yaml").read_text(encoding="utf-8"))
        rules.extend((f"合同 {task_id}", text) for text in [task.get("objective", ""), *task.get("constraints", [])] if text)
    agent = (_CAPABILITY / "agents" / f"{_RULE_AGENT}.md").read_text(encoding="utf-8")
    rules.extend((f"Agent {_RULE_AGENT}", item) for _heading, item in skill_items(agent))
    return rules


def compare_with_published(
    workspace: Path, run_id: str, *, threshold: float = 0.4
) -> dict[str, Any]:
    """Match every item of the Skills in use against a run's candidates and the stated rules.

    The Skills in use are the published files, or their backups when the run
    has already been published.  Writes ``compare.md`` and ``compare.json``
    beside the candidates and changes nothing else.
    """

    workspace = Path(workspace).resolve()
    root = workspace / run_root(run_id)
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    candidates = {
        role_id: (workspace / ref).read_text(encoding="utf-8")
        for role_id, ref in summary["candidates"].items()
    }
    pool = [(role_id, item) for role_id, text in candidates.items() for _h, item in skill_items(text)]
    rules = stated_rules()
    by_role = {}
    for role_id in [r for r in skill_order() if r in candidates]:
        backup = root / "previous" / role_id / "SKILL.md"
        current = backup if backup.is_file() else workspace / template_skill_ref(role_id)
        if not current.is_file():
            continue
        by_role[role_id] = compare_items(skill_items(current.read_text(encoding="utf-8")), pool, rules)
    text, counts = render_comparison(by_role, threshold=threshold)
    (root / "compare.md").write_text(text, encoding="utf-8")
    detail = {
        role_id: [match.__dict__ for match in matches] for role_id, matches in by_role.items()
    }
    (root / "compare.json").write_text(
        json.dumps({"threshold": threshold, "counts": counts, "items": detail}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return {
        "compare_ref": (root / "compare.md").relative_to(workspace).as_posix(),
        "counts": counts,
    }


__all__ = [
    "EXPERT_REPORT_REF",
    "FACT_FLAGS",
    "compare_with_published",
    "stated_rules",
    "plain_value",
    "LESSONS_FILE",
    "ROLES_FILE",
    "TRIGGER_KINDS",
    "TemplateSkillTools",
    "publish_template_skills",
    "skill_order",
    "align_skill_sections",
    "module_skill_roles",
    "skill_targets",
    "skills_for_run",
    "skills_with_lessons",
    "template_policy",
    "template_roles",
    "trigger_fires",
    "trigger_problem",
    "writing_lessons",
]
