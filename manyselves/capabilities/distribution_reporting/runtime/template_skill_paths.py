"""Capability-owned project paths for reusable template role Skills."""

from pathlib import Path

TEMPLATE_SKILL_ROOT = Path("Inputs/report-template-role-skills")
TEMPLATE_SKILL_BOUNDARY = TEMPLATE_SKILL_ROOT / "boundary.json"
TEMPLATE_SKILL_SOURCE = TEMPLATE_SKILL_ROOT / "source.json"


def template_skill_ref(skill_id: str) -> Path:
    """Return the project-relative Markdown file for one role Skill."""

    return TEMPLATE_SKILL_ROOT / skill_id / "SKILL.md"


__all__ = [
    "TEMPLATE_SKILL_BOUNDARY",
    "TEMPLATE_SKILL_ROOT",
    "TEMPLATE_SKILL_SOURCE",
    "template_skill_ref",
]
