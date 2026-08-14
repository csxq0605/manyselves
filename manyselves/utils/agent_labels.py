"""Shared agent display labels for UI and queue summaries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..interfaces.types import AgentId, AgentType, normalize_agent_id

if TYPE_CHECKING:
    from PyQt6.QtGui import QIcon
else:
    QIcon = Any


def _get_qicon(agent_type: str, color: str | None = None, size: int = 16) -> QIcon:
    """Lazy import of icon function to avoid circular imports."""
    from ..gui.icons import get_agent_icon as get_agent_qicon
    return get_agent_qicon(agent_type, color, size)


AGENT_LABELS: dict[str, dict[str, str]] = {
    "main": {"name": "Main"},
    "template-distiller": {"name": "Template Distiller"},
    "evidence-auditor": {"name": "Evidence Auditor"},
    "cross-module-reviewer": {"name": "Cross-module Reviewer"},
    "chief-editor": {"name": "Chief Editor"},
    "chief-editor-auditor": {"name": "Final Report Auditor"},
    "report-renderer": {"name": "Render"},
    "render": {"name": "Render"},
    # Compatibility labels for historical task/message records; these are not
    # offered by the current Main-only GUI selector.
    "data_analysis": {"name": "Data Analysis"},
    "plotting": {"name": "Plotting"},
    "theory": {"name": "Theory"},
    "report": {"name": "Report"},
    "sub": {"name": "Select"},
}


def normalize_agent_type(agent_type: AgentId | AgentType) -> str:
    return normalize_agent_id(agent_type or "").strip()


def get_agent_icon(agent_type: AgentId | AgentType, color: str | None = None, size: int = 24) -> QIcon:
    """Get QIcon for an agent type.

    Args:
        agent_type: The agent type
        color: Optional color override. If None, uses agent's theme color.
        size: Icon size in pixels (default 24 for higher resolution).
    """
    return _get_qicon(normalize_agent_type(agent_type), color, size)


def get_agent_name(agent_type: AgentId | AgentType) -> str:
    agent_key = normalize_agent_type(agent_type)
    role_key, separator, session_id = agent_key.partition("--session-")
    if role_key in AGENT_LABELS:
        name = AGENT_LABELS[role_key]["name"]
    elif role_key.startswith("module-") and role_key.endswith("-specialist"):
        module_id = role_key.removeprefix("module-").removesuffix("-specialist")
        name = f"Module {module_id} Specialist"
    elif role_key.startswith("module-auditor-2."):
        name = f"Module {role_key.removeprefix('module-auditor-')} Auditor"
    elif role_key.startswith("cross-owner-2."):
        name = f"Cross {role_key.removeprefix('cross-owner-')} Owner"
    elif role_key.startswith("chief-chapter-"):
        name = f"Chief Chapter {role_key.removeprefix('chief-chapter-')}"
    elif role_key.startswith("final-chapter-"):
        name = f"Final Chapter {role_key.removeprefix('final-chapter-')} Auditor"
    else:
        name = role_key.replace("_", " ").replace("-", " ").title() or "Agent"
    if separator:
        module_lane = session_id.split("--", 1)[0]
        if role_key in {"cross-module-reviewer", "evidence-auditor"} and re.fullmatch(
            r"2\.[1-5]", module_lane
        ):
            return f"{name} {module_lane}"
        chapter_match = re.fullmatch(r"chapter-([134])", module_lane)
        if role_key in {"chief-editor", "chief-editor-auditor"} and chapter_match:
            return f"{name} Chapter {chapter_match.group(1)}"
        return f"{name} · {session_id[-4:]}"
    return name


def get_agent_badge(agent_type: AgentId | AgentType) -> str:
    """Get text badge for an agent type (no icon, just name)."""
    return get_agent_name(agent_type)


def get_agent_title(agent_type: AgentId | AgentType) -> str:
    """Get full title for an agent type."""
    name = get_agent_name(agent_type)
    role, separator, instance = name.partition(" · ")
    if separator:
        return f"{role} Agent · {instance}"
    return f"{name} Agent"


def get_agent_badge_with_icon(agent_type: AgentId | AgentType) -> tuple[QIcon, str]:
    """Get (icon, name) tuple for an agent type."""
    return get_agent_icon(agent_type), get_agent_name(agent_type)
