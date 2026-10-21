"""Role-by-role extraction of writing methods from an exemplar, checked by program.

A capability declares roles and the exemplar sections each role covers.  For
one role the program builds a packet (the role's blocks with locators and the
outline's structure), a model returns structured entries, and the program
accepts only entries that:

- cite a locator inside the role's sections, with a quote found at that
  locator;
- as a method, carry none of the exemplar's numbers, codes or organisation
  names and none of the declared mandatory words (hard rules belong to the
  task contract, not to a method);
- as an example, put every exemplar fact in placeholders.

Every section of the role that has content is either cited or skipped with a
reason.  The accepted entries render as a Markdown Skill; quotes stay in the
provenance file, never in the Skill.  Nothing here knows the exemplar's
subject.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .outline import DocumentOutline, OutlineSection, label_sequence, resolve_section

_NUMBER = re.compile(r"\d+(?:[.,．]\d+)*")
_SPACE = re.compile(r"\s+")
MIN_QUOTE_CHARS = 6


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionRef(_Model):
    number: str
    title: str = ""


class ExemplarRole(_Model):
    id: str
    title: str
    purpose: str
    sections: list[SectionRef] = Field(default_factory=list)
    extract: bool = True


class ExemplarPolicy(_Model):
    mandatory_words: list[str] = Field(default_factory=list)
    organisation_suffixes: list[str] = Field(default_factory=list)
    placeholder_open: str = "〈"
    placeholder_close: str = "〉"


class ExemplarEntry(_Model):
    kind: Literal["method", "example"] = Field(
        description="method=可迁移的组织方式或写法；example=范例句式，事实换成〈〉占位"
    )
    text: str = Field(min_length=1, description="方法或范例本身")
    applies_when: str = Field(
        min_length=1, description="什么情况下用这条写法；源文没有说明就写“未明确”"
    )
    locator: str = Field(min_length=1, description="来源坐标，取自资料里方括号中的编号，如 p-75")
    quote: str = Field(min_length=1, description="该坐标处的一段原文，逐字摘录")


class SkippedSection(_Model):
    section: str = Field(min_length=1, description="小节编号")
    reason: str = Field(min_length=1, description="为什么这一节没有可迁移的写法")


class ExemplarSubmission(_Model):
    entries: list[ExemplarEntry] = Field(default_factory=list)
    skipped: list[SkippedSection] = Field(default_factory=list)


@dataclass(frozen=True)
class RoleScope:
    role: ExemplarRole
    sections: tuple[OutlineSection, ...]
    missing: tuple[str, ...] = ()

    @property
    def numbers(self) -> set[str]:
        return {section.number for section in self.sections}


@dataclass
class Rejection:
    index: int
    entry: ExemplarEntry
    reasons: list[str]


@dataclass
class Acceptance:
    accepted: list[ExemplarEntry] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    skipped: list[SkippedSection] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.accepted) and not self.rejected and not self.uncovered


def role_scope(outline: DocumentOutline, role: ExemplarRole) -> RoleScope:
    """The outline sections a role covers: each declared section and its subtree."""

    found: dict[str, OutlineSection] = {}
    missing: list[str] = []
    for ref in role.sections:
        head = resolve_section(outline, ref.number, ref.title)
        if head is None:
            missing.append(ref.number)
            continue
        for section in outline.subtree(head.number):
            found.setdefault(section.number, section)
    ordered = tuple(s for s in outline.sections if s.number in found)
    return RoleScope(role, ordered, tuple(missing))


def role_packet(outline: DocumentOutline, scope: RoleScope) -> dict[str, Any]:
    """What the model reads for one role: structure and every located block."""

    sections = []
    for section in scope.sections:
        blocks = [outline.block(locator) for locator in section.blocks]
        sections.append(
            {
                "number": section.number,
                "title": section.title,
                "structure": structure_line(outline, section),
                "blocks": [
                    {
                        "locator": block.locator,
                        "kind": block.kind,
                        "text": block.text,
                        **({"label": block.label} if block.label else {}),
                        **({"list_item": True} if block.list_item else {}),
                        **({"images": block.images} if block.images else {}),
                    }
                    for block in blocks
                    if block is not None
                ],
            }
        )
    return {
        "role_id": scope.role.id,
        "title": scope.role.title,
        "purpose": scope.role.purpose,
        "sections": sections,
    }


def structure_line(
    outline: DocumentOutline,
    section: OutlineSection,
    *,
    policy: ExemplarPolicy | None = None,
) -> str:
    """The program-counted shape of one section, in words.

    With a ``policy`` (for a published Skill) row counts are left out and a
    table's columns are shown only when they carry no number and no
    organisation name, since a first row can hold the exemplar's own facts.
    """

    parts: list[str] = []
    labels = label_sequence(section.labels)
    if labels:
        parts.append("分段小标题：" + " → ".join(labels))
    blocks = [b for b in (outline.block(locator) for locator in section.blocks) if b is not None]
    names = organisation_names(outline, policy.organisation_suffixes) if policy else set()
    for table in (block for block in blocks if block.kind == "table"):
        columns = [c for c in table.columns if c]
        if policy is None:
            parts.append(f"表（{table.rows} 行，列：" + "、".join(columns) + "）")
            continue
        header = "".join(columns)
        if columns and not _NUMBER.search(header) and not any(n in _normal(header) for n in names):
            parts.append("表（列：" + "、".join(columns) + "）")
        else:
            parts.append("表")
    if section.images:
        parts.append("配图" if policy else f"图片 {section.images} 张")
    lists = sum(1 for block in blocks if block.list_item)
    if lists:
        parts.append("用列表" if policy else f"列表项 {lists} 条")
    return "；".join(parts)


def _normal(text: str) -> str:
    return _SPACE.sub("", text)


def organisation_names(outline: DocumentOutline, suffixes: Sequence[str]) -> set[str]:
    """Organisation names in the exemplar: two to four characters before a suffix."""

    texts = [*outline.front_matter, *(block.text for block in outline.blocks)]
    names: set[str] = set()
    for suffix in suffixes:
        pattern = re.compile(r"([一-鿿A-Za-z]{2,4})" + re.escape(suffix))
        for text in texts:
            names.update(match.group(0) for match in pattern.finditer(_normal(text)))
    return names


def _strip_placeholders(text: str, policy: ExemplarPolicy) -> str:
    pattern = re.escape(policy.placeholder_open) + r"[^" + re.escape(policy.placeholder_close) + r"]*" + re.escape(policy.placeholder_close)
    return re.sub(pattern, "", text)


def _numbers(text: str, allowed: set[str]) -> list[str]:
    return [token for token in dict.fromkeys(_NUMBER.findall(text)) if token not in allowed]


def accept_submission(
    outline: DocumentOutline,
    scope: RoleScope,
    submission: ExemplarSubmission,
    policy: ExemplarPolicy,
) -> Acceptance:
    """Accept the entries that pass every program check; name what fails."""

    numbers_allowed = outline.section_numbers()
    organisations = organisation_names(outline, policy.organisation_suffixes)
    in_scope = scope.numbers
    result = Acceptance()
    seen: set[tuple[str | None, str, str]] = set()
    for index, entry in enumerate(submission.entries):
        reasons: list[str] = []
        block = outline.block(entry.locator.strip())
        if block is None:
            reasons.append(f"来源坐标 {entry.locator} 不存在")
        elif block.section not in in_scope:
            reasons.append(f"来源坐标 {entry.locator} 不在本角色的小节里")
        elif len(_normal(entry.quote)) < MIN_QUOTE_CHARS or _normal(entry.quote) not in _normal(
            block.text
        ):
            reasons.append(f"原文片段在 {entry.locator} 处找不到，要逐字摘录")
        if entry.kind == "method":
            checked = f"{entry.text}\n{entry.applies_when}"
            numbers = _numbers(checked, numbers_allowed)
            if numbers:
                reasons.append("方法里有模板的数值或编号：" + "、".join(numbers) + "；方法不带具体数值")
            words = [word for word in policy.mandatory_words if word in checked]
            if words:
                reasons.append(
                    "方法里有强制词：" + "、".join(words) + "；改成建议写法，硬规定只由任务合同给出"
                )
        else:
            checked = _strip_placeholders(f"{entry.text}\n{entry.applies_when}", policy)
            numbers = _numbers(checked, numbers_allowed)
            if numbers:
                reasons.append(
                    "范例里的数值或编号没有换成"
                    + policy.placeholder_open
                    + policy.placeholder_close
                    + "占位："
                    + "、".join(numbers)
                )
        names = [name for name in organisations if name in _normal(checked)]
        if names:
            reasons.append("含模板里的单位名称：" + "、".join(sorted(names)))
        # The same method may hold in several sections; repeat it only once per section.
        key = (block.section if block is not None else None, entry.kind, _normal(entry.text))
        if not reasons and key in seen:
            continue
        if reasons:
            result.rejected.append(Rejection(index, entry, reasons))
        else:
            seen.add(key)
            result.accepted.append(entry)
    cited = {
        block.section
        for block in (outline.block(entry.locator.strip()) for entry in result.accepted)
        if block is not None
    }
    result.skipped = [
        skip for skip in submission.skipped if skip.section in in_scope and skip.section not in cited
    ]
    skipped = {skip.section for skip in result.skipped}
    result.uncovered = [
        section.number
        for section in scope.sections
        if section.blocks and section.number not in cited and section.number not in skipped
    ]
    return result


def revision_request(acceptance: Acceptance) -> dict[str, Any]:
    """The program's findings, for the same conversation to correct."""

    return {
        "rejected": [
            {
                "index": rejection.index,
                "locator": rejection.entry.locator,
                "kind": rejection.entry.kind,
                "text": rejection.entry.text,
                "reasons": rejection.reasons,
            }
            for rejection in acceptance.rejected
        ],
        "uncovered_sections": acceptance.uncovered,
        "accepted_count": len(acceptance.accepted),
    }


def acceptance_record(acceptance: Acceptance) -> dict[str, Any]:
    return {
        "passed": acceptance.passed,
        "accepted": [entry.model_dump(mode="json") for entry in acceptance.accepted],
        "rejected": [
            {
                "index": rejection.index,
                "entry": rejection.entry.model_dump(mode="json"),
                "reasons": rejection.reasons,
            }
            for rejection in acceptance.rejected
        ],
        "uncovered": acceptance.uncovered,
        "skipped": [skip.model_dump(mode="json") for skip in acceptance.skipped],
    }


def render_role_skill(
    outline: DocumentOutline,
    scope: RoleScope,
    entries: Iterable[ExemplarEntry],
    *,
    source: str,
    policy: ExemplarPolicy,
    skipped: Iterable[SkippedSection] = (),
) -> str:
    """The accepted entries as a Markdown Skill, grouped by exemplar section."""

    by_section: dict[str, list[ExemplarEntry]] = {}
    for entry in entries:
        block = outline.block(entry.locator.strip())
        if block is not None and block.section is not None:
            by_section.setdefault(block.section, []).append(entry)
    reasons = {skip.section: skip.reason for skip in skipped}
    role = scope.role
    lines = [
        "---",
        f"name: {role.id}",
        f"description: {role.title}",
        f"source: {source}",
        "---",
        f"# {role.title}",
        "",
        role.purpose,
        "",
        "以下写法由程序按小节从专家版模板提炼并核对出处：结构是程序数出来的，"
        "方法和范例是建议，不是规定；本轮必须做的事只看任务合同。"
        "范例里〈〉处是要换成本项目事实的位置，不能照抄模板的数值和对象。",
    ]
    for section in scope.sections:
        found = by_section.get(section.number, [])
        structure = structure_line(outline, section, policy=policy)
        if not found and not structure:
            continue
        lines.extend(["", f"## {section.number} {section.title}"])
        if structure:
            lines.append(f"结构：{structure}")
        for entry in found:
            if entry.kind == "method":
                condition = entry.applies_when.strip()
                lines.append(
                    f"- 方法：{entry.text.strip()}"
                    + (f"（适用：{condition}）" if condition and condition != "未明确" else "")
                    + f" 〔模板 {entry.locator.strip()}〕"
                )
            else:
                lines.append(f"- 范例：{entry.text.strip()} 〔模板 {entry.locator.strip()}〕")
        if not found and section.number in reasons:
            lines.append(f"- 本节无可迁移写法：{reasons[section.number]}")
    return "\n".join(lines) + "\n"


def text_diff(old: str, new: str, *, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{name}（现用）",
            tofile=f"{name}（新提炼）",
        )
    )


def parse_roles(value: Mapping[str, Any]) -> tuple[list[ExemplarRole], ExemplarPolicy]:
    roles = [ExemplarRole.model_validate(role) for role in value.get("roles", [])]
    ids = [role.id for role in roles]
    if len(ids) != len(set(ids)):
        raise ValueError("exemplar role ids must be unique")
    return roles, ExemplarPolicy.model_validate(value.get("policy", {}))


__all__ = [
    "Acceptance",
    "ExemplarEntry",
    "ExemplarPolicy",
    "ExemplarRole",
    "ExemplarSubmission",
    "Rejection",
    "RoleScope",
    "SectionRef",
    "SkippedSection",
    "accept_submission",
    "acceptance_record",
    "organisation_names",
    "parse_roles",
    "render_role_skill",
    "revision_request",
    "role_packet",
    "role_scope",
    "structure_line",
    "text_diff",
]
