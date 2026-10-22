"""Reporting-owned isolation policy for the expert template source."""

from __future__ import annotations

import unicodedata
from pathlib import Path

FORBIDDEN_AGENT_DOCUMENT_NAME = "配电安全专家咨询报告(专家优化版).docx"
ISOLATED_DISTILLATION_SNAPSHOT_NAME = "template-for-skill.docx"
FORBIDDEN_AGENT_ACCESS_ERROR = "EXPERT_TEMPLATE_AGENT_ACCESS_FORBIDDEN"


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def is_isolated_distillation_document(path_or_ref: str | Path) -> bool:
    """Return whether a value names the expert source or its private snapshot."""

    value = str(path_or_ref).replace("\\", "/")
    name = _normalized(Path(value).name)
    return name in {
        _normalized(FORBIDDEN_AGENT_DOCUMENT_NAME),
        _normalized(ISOLATED_DISTILLATION_SNAPSHOT_NAME),
    }


def reject_forbidden_agent_document(path_or_ref: str | Path) -> None:
    """Prevent every Reporting Agent from opening the expert report.

    Methods are learned from it only by the program-run extraction, which
    hands each role its located blocks; no agent reads the file itself.
    """

    if is_isolated_distillation_document(path_or_ref):
        raise PermissionError(
            f"{FORBIDDEN_AGENT_ACCESS_ERROR}: this document is outside every Reporting "
            "Agent's read, search, inspection, and analysis scope"
        )


__all__ = [
    "FORBIDDEN_AGENT_ACCESS_ERROR",
    "FORBIDDEN_AGENT_DOCUMENT_NAME",
    "ISOLATED_DISTILLATION_SNAPSHOT_NAME",
    "is_isolated_distillation_document",
    "reject_forbidden_agent_document",
]
