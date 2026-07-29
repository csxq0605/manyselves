"""Deterministic loading and task-scoped routing for packaged module Skills."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .config import ConfigurationError
from .taxonomy import REPORT_TAXONOMY, resolve_submodule


@dataclass(frozen=True, slots=True)
class ModuleSkill:
    """One versioned, packaged instruction fragment owned by a report module."""

    id: str
    version: str
    title: str
    module_id: str
    submodules: tuple[str, ...]
    content: str
    source_path: Path
    scope: Literal["packaged", "product", "project"] = "packaged"
    sha256: str = ""


def _frontmatter(path: Path) -> tuple[dict, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ConfigurationError(f"{path}: missing YAML frontmatter")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ConfigurationError(f"{path}: unterminated YAML frontmatter") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path}: invalid YAML: {exc}") from exc
    return data, "\n".join(lines[closing + 1 :]).strip()


class ModuleSkillLibrary:
    """Load module Skills once and expose the minimum scope needed by each Agent."""

    _SPECIALIST = re.compile(r"^module-(2\.[1-5])-specialist$")

    def __init__(self, skills: tuple[ModuleSkill, ...]):
        self._skills = skills

    @property
    def skills(self) -> tuple[ModuleSkill, ...]:
        return self._skills

    @classmethod
    def packaged(cls) -> "ModuleSkillLibrary":
        root = Path(__file__).resolve().parents[2] / "templates" / "reporting" / "skills"
        return cls.load(root)

    @classmethod
    def load(cls, root: Path) -> "ModuleSkillLibrary":
        root = Path(root)
        skills: list[ModuleSkill] = []
        seen_ids: set[str] = set()
        for path in sorted(root.glob("*/*.md")):
            module_id = path.parent.name
            if module_id not in REPORT_TAXONOMY:
                raise ConfigurationError(f"{path}: unknown module directory {module_id}")
            data, content = _frontmatter(path)
            required = {"id", "version", "title", "submodules"}
            missing = sorted(required - set(data))
            if missing:
                raise ConfigurationError(f"{path}: missing fields: {', '.join(missing)}")
            skill_id = str(data["id"]).strip()
            if not skill_id or skill_id in seen_ids:
                raise ConfigurationError(f"{path}: duplicate or empty skill id {skill_id!r}")
            raw_submodules = data["submodules"]
            if not isinstance(raw_submodules, list) or not raw_submodules:
                raise ConfigurationError(f"{path}: submodules must be a non-empty list")
            submodules = tuple(str(value) for value in raw_submodules)
            for submodule_id in submodules:
                if resolve_submodule(submodule_id).module_id != module_id:
                    raise ConfigurationError(
                        f"{path}: submodule {submodule_id} does not belong to module {module_id}"
                    )
            if not content:
                raise ConfigurationError(f"{path}: skill body cannot be empty")
            seen_ids.add(skill_id)
            skills.append(
                ModuleSkill(
                    id=skill_id,
                    version=str(data["version"]),
                    title=str(data["title"]),
                    module_id=module_id,
                    submodules=submodules,
                    content=content,
                    source_path=path,
                    scope="packaged",
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
        if not skills:
            raise ConfigurationError(f"{root}: no module Skills found")
        missing_modules = sorted(set(REPORT_TAXONOMY) - {skill.module_id for skill in skills})
        if missing_modules:
            raise ConfigurationError(
                f"{root}: modules without Skills: {', '.join(missing_modules)}"
            )
        return cls(tuple(skills))

    def for_agent(
        self,
        agent_id: str,
        *,
        module_id: str | None = None,
        submodule_ids: set[str] | None = None,
    ) -> list[ModuleSkill]:
        match = self._SPECIALIST.fullmatch(agent_id)
        if match:
            module_id = match.group(1)
        elif agent_id != "evidence-auditor":
            return []
        if module_id not in REPORT_TAXONOMY:
            raise ConfigurationError(
                f"{agent_id}: a fixed module_id is required for module Skill routing"
            )
        skills = [
            skill
            for skill in self._skills
            if skill.module_id in {module_id, "all"}
        ]
        if agent_id == "evidence-auditor" and submodule_ids:
            skills = [
                skill
                for skill in skills
                if set(skill.submodules).intersection(submodule_ids)
            ]
        return skills

    def index_text(self) -> str:
        """Return metadata only for routing diagnostics without instruction leakage."""

        lines: list[str] = []
        for module_id in REPORT_TAXONOMY:
            entries = [
                f"{skill.id} v{skill.version} ({skill.title}; {', '.join(skill.submodules)})"
                for skill in self._skills
                if skill.module_id in {module_id, "all"}
            ]
            lines.append(f"{module_id}: " + "; ".join(entries))
        return "\n".join(lines)
