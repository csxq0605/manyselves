"""Python Tools and runtime binding for the checklist-review Capability.

The Tools are deterministic: read a project text file, search it, and tally
the findings.  Reading and judging are Agent tasks; ordering, fan-out and the
finding table are declared in the workflow files.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

from .. import CAPABILITY_FILE

_MAX_DOCUMENT_CHARS = 200_000
_MAX_MATCHES = 40


class DocumentAccessError(ValueError):
    """Raised when a requested document is not a readable project text file."""


def _project_file(workspace: Path, relative: str) -> Path:
    root = Path(workspace).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise DocumentAccessError(f"document is not a project file: {relative}")
    return path


def _read_text(workspace: Path, relative: str) -> str:
    path = _project_file(workspace, relative)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentAccessError(f"document is not UTF-8 text: {relative}") from exc
    if len(text) > _MAX_DOCUMENT_CHARS:
        raise DocumentAccessError(
            f"document has {len(text)} characters; the limit is {_MAX_DOCUMENT_CHARS}"
        )
    return text


def tool_implementations(workspace: Path) -> dict[str, Callable[[Any], Any]]:
    """Bind the Capability Tools to one project workspace."""

    def load_document(request: dict[str, Any]) -> dict[str, Any]:
        text = _read_text(workspace, request["document_path"])
        return {"path": request["document_path"], "text": text, "chars": len(text)}

    def search_document(query: dict[str, Any]) -> dict[str, Any]:
        text = _read_text(workspace, query["path"])
        needle = query["query"].casefold()
        matches = [
            {"line": number, "text": line.strip()}
            for number, line in enumerate(text.splitlines(), start=1)
            if needle in line.casefold()
        ]
        return {"matches": matches[:_MAX_MATCHES]}

    def summarize_findings(values: dict[str, Any]) -> dict[str, Any]:
        findings = values["findings"]
        errors = values["errors"]
        counts = {"met": 0, "not_met": 0, "insufficient": 0, "failed": 0}
        items = []
        for entry in values["checklist"]:
            item_id = entry["id"]
            finding = findings.get(item_id)
            if finding is None:
                counts["failed"] += 1
                items.append(
                    {
                        "item_id": item_id,
                        "question": entry["question"],
                        "verdict": "failed",
                        "evidence": [],
                        "note": errors.get(item_id, "no finding was produced"),
                    }
                )
                continue
            counts[finding["verdict"]] += 1
            items.append(
                {
                    "item_id": item_id,
                    "question": entry["question"],
                    "verdict": finding["verdict"],
                    "evidence": finding["evidence"],
                    "note": finding["note"],
                }
            )
        return {"counts": counts, "open_items": values["open_items"], "items": items}

    return {
        "load-document": load_document,
        "search-document": search_document,
        "summarize-findings": summarize_findings,
    }


def build_runtime_binding(
    *,
    workspace: Path,
    services: Any,
    **options: Any,
) -> GenericCapabilityRuntimeBinding:
    return GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=workspace,
        services=services,
        tool_implementations=tool_implementations(Path(workspace)),
        **options,
    )


__all__ = ["DocumentAccessError", "build_runtime_binding", "tool_implementations"]
