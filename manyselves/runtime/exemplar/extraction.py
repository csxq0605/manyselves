"""Role-by-role extraction of methods from an exemplar, checked by program.

A capability declares the uses an entry can serve (for example one use per
step of its workflow that reads the Skill), the roles, and the exemplar
sections each role covers.  For one role the program builds a packet (the
role's blocks with locators, the outline's structure and the uses the role
may fill), a model returns structured entries, and the program accepts only
entries that:

- name a use the role may fill;
- cite a locator inside the role's sections, with a quote found at that
  locator;
- in a use without placeholders, carry none of the exemplar's numbers, codes
  or organisation names and none of the declared mandatory words (hard rules
  belong to the task contract, not to a method);
- in a use with placeholders, put every exemplar fact in placeholders;
- mention none of the features the capability declares unavailable;
- carry none of the exemplar's own instances, taken from its structure: the
  titles of its sections that the product has no section for.  Ordinary
  words are never refused; only the exemplar's facts are.

When the capability passes its own section headings (``targets``), every
exemplar section is aligned to the product section with the most similar
title under the same parent; a section with no counterpart folds into its
parent.  The Skill is then headed by the product's sections, not by the
exemplar's numbering.  Structure lines show the shape of a section and only
count its subheadings, which name the exemplar's own places and findings.

Every section of the role that has content is either cited or skipped with a
reason, and every use the role requires has at least one entry.  The accepted
entries render as a Markdown Skill grouped by section and then by use; quotes
stay in the provenance file, never in the Skill.  Nothing here knows the
exemplar's subject.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .outline import DocumentOutline, OutlineSection, label_sequence, resolve_section, similar

_NUMBER = re.compile(r"\d+(?:[.,．]\d+)*")
_SPACE = re.compile(r"\s+")
MIN_QUOTE_CHARS = 6


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionRef(_Model):
    number: str
    title: str = ""
    generated: str = Field(
        default="",
        description="When the product draws this section's tables itself: why, shown instead "
        "of the exemplar's table columns",
    )


class ExemplarUse(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    title: str
    description: str
    placeholders: bool = Field(
        default=False,
        description="True for examples: exemplar facts are allowed only inside placeholders",
    )


DEFAULT_USES = (
    ExemplarUse(id="method", title="方法", description="可迁移的组织方式或写法"),
    ExemplarUse(id="example", title="范例", description="范例句式，事实换成占位", placeholders=True),
)


class ExemplarRole(_Model):
    id: str
    title: str
    purpose: str
    sections: list[SectionRef] = Field(default_factory=list)
    extract: bool = True
    uses: list[str] = Field(default_factory=list, description="Uses this role may fill; empty means all")
    required_uses: list[str] = Field(default_factory=list)


class ExemplarPolicy(_Model):
    mandatory_words: list[str] = Field(default_factory=list)
    organisation_suffixes: list[str] = Field(default_factory=list)
    placeholder_open: str = "〈"
    placeholder_close: str = "〉"
    uses: list[ExemplarUse] = Field(default_factory=lambda: list(DEFAULT_USES))
    unavailable: dict[str, str] = Field(
        default_factory=dict,
        description="Words naming what the product does not produce, with the reason",
    )
    def use(self, use_id: str) -> ExemplarUse | None:
        return next((use for use in self.uses if use.id == use_id), None)

    def role_uses(self, role: ExemplarRole) -> list[ExemplarUse]:
        if not role.uses:
            return list(self.uses)
        return [use for use in self.uses if use.id in role.uses]


class ExemplarEntry(_Model):
    use: str = Field(min_length=1, description="这条内容的用途，取资料 uses 里的 id")
    text: str = Field(min_length=1, description="内容本身，一条只写一个要点")
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
class Target:
    """One of the product's own section headings."""

    number: str
    title: str

    @property
    def heading(self) -> str:
        return f"{self.number} {self.title}".strip()


@dataclass(frozen=True)
class RoleScope:
    role: ExemplarRole
    sections: tuple[OutlineSection, ...]
    missing: tuple[str, ...] = ()
    generated: Mapping[str, str] = field(default_factory=dict)
    # Exemplar section number -> the product section it is written under.
    aligned: Mapping[str, Target] = field(default_factory=dict)
    # Exemplar sections that have no product counterpart and fold into their parent.
    folded: tuple[str, ...] = ()
    product_titles: tuple[str, ...] = ()

    @property
    def numbers(self) -> set[str]:
        return {section.number for section in self.sections}

    def target(self, section: OutlineSection) -> Target:
        return self.aligned.get(section.number) or Target(section.number, section.title)


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
    missing_uses: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            bool(self.accepted)
            and not self.rejected
            and not self.uncovered
            and not self.missing_uses
        )


ALIGN_SIMILARITY = 0.5  # the same bar resolve_section uses for a declared title


def _parent_number(number: str) -> str:
    return number.rpartition(".")[0]


def role_scope(
    outline: DocumentOutline,
    role: ExemplarRole,
    targets: Sequence[Target] = (),
) -> RoleScope:
    """The outline sections a role covers: each declared section and its subtree.

    With ``targets`` (the product's own headings) each section is aligned to
    the product section with the most similar title among the children of its
    parent's product section; one without a counterpart folds into its parent.
    """

    found: dict[str, OutlineSection] = {}
    generated: dict[str, str] = {}
    missing: list[str] = []
    aligned: dict[str, Target] = {}
    folded: list[str] = []
    by_number = {target.number: target for target in targets}
    for ref in role.sections:
        head = resolve_section(outline, ref.number, ref.title)
        if head is None:
            missing.append(ref.number)
            continue
        for section in outline.subtree(head.number):
            found.setdefault(section.number, section)
            if ref.generated:
                generated.setdefault(section.number, ref.generated)
            if not targets or section.number in aligned:
                continue
            if section.number == head.number:
                aligned[section.number] = by_number.get(ref.number) or Target(
                    ref.number, ref.title or head.title
                )
                continue
            parent = aligned.get(_parent_number(section.number)) or aligned[head.number]
            children = [
                target for target in targets
                if _parent_number(target.number) == parent.number
            ]
            best = max(children, key=lambda t: similar(t.title, section.title), default=None)
            if best is not None and similar(best.title, section.title) >= ALIGN_SIMILARITY:
                aligned[section.number] = best
            else:
                aligned[section.number] = parent
                folded.append(section.number)
    ordered = tuple(s for s in outline.sections if s.number in found)
    return RoleScope(
        role, ordered, tuple(missing), generated, aligned, tuple(folded),
        tuple(target.title for target in targets),
    )


def instance_terms(outline: DocumentOutline, scope: RoleScope) -> dict[str, str]:
    """The exemplar's own instances an entry may not carry, each with where it came from.

    Taken from the exemplar's structure alone, with no word list: the titles
    of its sections that have no product counterpart (its own places, objects
    or topics).  Ordinary words are not refused; numbers, codes and
    organisation names are checked separately.  A term inside a product
    section title is the product's own word and is not refused.
    """

    terms: dict[str, str] = {}
    for number in scope.folded:
        section = outline.section(number)
        if section is not None:
            terms.setdefault(_normal(section.title), "专家版独有的小节标题")
    own = [_normal(title) for title in scope.product_titles]
    return {
        term: why for term, why in terms.items()
        if len(term) >= 2 and not _NUMBER.fullmatch(term) and not any(term in title for title in own)
    }


def role_packet(
    outline: DocumentOutline, scope: RoleScope, policy: ExemplarPolicy | None = None
) -> dict[str, Any]:
    """What the model reads for one role: uses, structure and every located block."""

    policy = policy or ExemplarPolicy()
    sections = []
    for section in scope.sections:
        blocks = [outline.block(locator) for locator in section.blocks]
        target = scope.target(section)
        sections.append(
            {
                "number": section.number,
                "title": section.title,
                **(
                    {"product_section": target.heading}
                    if scope.aligned and target.heading != f"{section.number} {section.title}"
                    else {}
                ),
                "structure": structure_line(outline, section, generated=scope.generated.get(section.number, "")),
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
        "uses": [
            {
                "id": use.id,
                "title": use.title,
                "description": use.description,
                "placeholders": use.placeholders,
                "required": use.id in scope.role.required_uses,
            }
            for use in policy.role_uses(scope.role)
        ],
        "unavailable": dict(policy.unavailable),
        "sections": sections,
    }


def structure_line(
    outline: DocumentOutline,
    section: OutlineSection,
    *,
    policy: ExemplarPolicy | None = None,
    generated: str = "",
) -> str:
    """The program-counted shape of one section, in words.

    With a ``policy`` (for a published Skill) row counts are left out, a
    table's columns are shown only when they carry no number and no
    organisation name, since a first row can hold the exemplar's own facts,
    and subheadings are only counted: they carry the exemplar's own places
    and conclusions.
    When the product draws the section's tables itself (``generated``), the
    exemplar's tables are named but their columns are not shown.
    """

    parts: list[str] = []
    labels = label_sequence(section.labels)
    if labels and policy is None:
        parts.append("分段小标题：" + " → ".join(labels))
    elif labels:
        # Subheadings name the exemplar's own places and findings; only their count is shape.
        parts.append(f"分段小标题 {len(labels)} 个（按本项目的发现自拟）")
    blocks = [b for b in (outline.block(locator) for locator in section.blocks) if b is not None]
    names = organisation_names(outline, policy.organisation_suffixes) if policy else set()
    tables = [block for block in blocks if block.kind == "table"]
    if tables and generated:
        parts.append(f"表（{generated}）")
        tables = []
    for table in tables:
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
    instances = instance_terms(outline, scope)
    in_scope = scope.numbers
    allowed_uses = {use.id: use for use in policy.role_uses(scope.role)}
    result = Acceptance()
    seen: set[tuple[str | None, str, str]] = set()
    for index, entry in enumerate(submission.entries):
        reasons: list[str] = []
        use = allowed_uses.get(entry.use.strip())
        if use is None:
            reasons.append(
                f"用途 {entry.use} 不是本角色可用的用途：" + "、".join(allowed_uses)
            )
        block = outline.block(entry.locator.strip())
        if block is None:
            reasons.append(f"来源坐标 {entry.locator} 不存在")
        elif block.section not in in_scope:
            reasons.append(f"来源坐标 {entry.locator} 不在本角色的小节里")
        elif len(_normal(entry.quote)) < MIN_QUOTE_CHARS or _normal(entry.quote) not in _normal(
            block.text
        ):
            reasons.append(f"原文片段在 {entry.locator} 处找不到，要逐字摘录")
        if use is None or not use.placeholders:
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
        found = sorted(term for term in instances if term in _normal(checked))
        # Report the longest names only: a tail of one found name adds nothing.
        found = [term for term in found if not any(term != other and term in other for other in found)]
        if found:
            reasons.append(
                "含专家版项目的实际："
                + "、".join(f"{term}（{instances[term]}）" for term in found)
                + "；改成通用说法"
                + ("，或换成" + policy.placeholder_open + policy.placeholder_close + "占位" if use and use.placeholders else "")
            )
        for word, why in policy.unavailable.items():
            if word in entry.text or word in entry.applies_when:
                reasons.append(f"提到了“{word}”：{why}")
        # The same method may hold in several sections; repeat it only once per section.
        key = (block.section if block is not None else None, entry.use.strip(), _normal(entry.text))
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
    filled = {entry.use.strip() for entry in result.accepted}
    result.missing_uses = [use for use in scope.role.required_uses if use not in filled]
    return result


def revision_request(acceptance: Acceptance) -> dict[str, Any]:
    """The program's findings, for the same conversation to correct."""

    return {
        "rejected": [
            {
                "index": rejection.index,
                "locator": rejection.entry.locator,
                "use": rejection.entry.use,
                "text": rejection.entry.text,
                "reasons": rejection.reasons,
            }
            for rejection in acceptance.rejected
        ],
        "uncovered_sections": acceptance.uncovered,
        "missing_uses": acceptance.missing_uses,
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
        "missing_uses": acceptance.missing_uses,
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
    """The accepted entries as a Markdown Skill, by exemplar section and then by use."""

    by_section: dict[str, list[ExemplarEntry]] = {}
    for entry in entries:
        block = outline.block(entry.locator.strip())
        if block is not None and block.section is not None:
            by_section.setdefault(block.section, []).append(entry)
    reasons = {skip.section: skip.reason for skip in skipped}
    role = scope.role
    uses = policy.role_uses(role)
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
        "以下内容由程序按小节从专家版模板提炼并核对出处，每节按用途分块："
        + "；".join(f"{use.title}（{use.description}）" for use in uses)
        + "。结构是程序数出来的，其余是建议，不是规定；本轮必须做的事只看任务合同。"
        f"{policy.placeholder_open}{policy.placeholder_close}处是要换成本项目事实的位置，"
        "不能照抄模板的数值和对象。",
    ]
    # Exemplar sections written under the same product section share one heading.
    groups: dict[str, list[OutlineSection]] = {}
    headings: dict[str, str] = {}
    for section in scope.sections:
        target = scope.target(section)
        groups.setdefault(target.number, []).append(section)
        headings.setdefault(target.number, target.heading)
    for number, sections in groups.items():
        found = [entry for section in sections for entry in by_section.get(section.number, [])]
        structures = list(
            dict.fromkeys(
                text
                for section in sections
                if (
                    text := structure_line(
                        outline, section, policy=policy,
                        generated=scope.generated.get(section.number, ""),
                    )
                )
            )
        )
        if not found and not structures:
            continue
        lines.extend(["", f"## {headings[number]}"])
        lines.extend(f"结构：{text}" for text in structures)
        for use in uses:
            chosen = [entry for entry in found if entry.use.strip() == use.id]
            if not chosen:
                continue
            lines.append(f"{use.title}：")
            for entry in chosen:
                condition = entry.applies_when.strip()
                show = not use.placeholders and condition and condition != "未明确"
                lines.append(
                    f"- {entry.text.strip()}"
                    + (f"（适用：{condition}）" if show else "")
                    + f" 〔模板 {entry.locator.strip()}〕"
                )
        if not found:
            lines.extend(
                f"- 本节无可迁移内容：{reasons[section.number]}"
                for section in sections
                if section.number in reasons
            )
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
    policy = ExemplarPolicy.model_validate(value.get("policy", {}))
    known = {use.id for use in policy.uses}
    if len(known) != len(policy.uses):
        raise ValueError("exemplar use ids must be unique")
    for role in roles:
        unknown = sorted(set(role.uses) - known)
        if unknown:
            raise ValueError(f"role {role.id} names unknown uses: {', '.join(unknown)}")
        allowed = {use.id for use in policy.role_uses(role)}
        if not set(role.required_uses) <= allowed:
            raise ValueError(f"role {role.id} requires a use it may not fill")
    return roles, policy


__all__ = [
    "Acceptance",
    "ExemplarEntry",
    "ExemplarPolicy",
    "ExemplarRole",
    "ExemplarSubmission",
    "ExemplarUse",
    "Rejection",
    "RoleScope",
    "SectionRef",
    "SkippedSection",
    "Target",
    "accept_submission",
    "acceptance_record",
    "instance_terms",
    "organisation_names",
    "parse_roles",
    "render_role_skill",
    "revision_request",
    "role_packet",
    "role_scope",
    "structure_line",
    "text_diff",
]
