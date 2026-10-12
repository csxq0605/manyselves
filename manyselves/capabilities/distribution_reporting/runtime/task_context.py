"""Authoritative reporting inputs and current durable drafts for Provider dispatch."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.runtime.loops.context_rebase import TaskContextRebuilder

from .collaboration_tools import ListResultPartsTool
from .models.agentic import TaskEnvelope
from .research.fact_context import render_fact_packet
from .research.project_evidence import ProjectEvidenceIndex
from .storage import ReportingStore


def task_fact_packet(workspace: Path, envelope: TaskEnvelope, module_id: str) -> str | None:
    """Load the declared finite scope once, without an LLM search round.

    Missing snapshots do not erase an already prepared inline input or turn
    an unavailable source into an empty complete evidence list.
    """
    index = ProjectEvidenceIndex(workspace, envelope.run_id)
    if not index.path.is_file():
        return None
    scope = set(envelope.target_submodule_ids) or None
    return render_fact_packet(index.module_items(module_id, scope), module_id)


def replace_inline_fact_packet(inline: str | None, packet: str) -> str:
    """Replace the prepared leading packet, leaving knowledge/instructions intact."""
    text = inline or ""
    if text.startswith("<project_facts ") and "</project_facts>" in text:
        text = text.split("</project_facts>", 1)[1].lstrip()
    return '<project_facts delivery_mode="inline">\n' + packet + "</project_facts>\n\n" + text


def bind_task_context(
    loop: Any,
    workspace: Path,
    envelope: TaskEnvelope,
    *,
    output_contract: str,
    render_prompt: Callable[[], str],
) -> TaskContextRebuilder | None:
    """Bind the existing Runtime hook; keep draft selection Capability-owned."""
    setter = getattr(loop, "set_context_rebuilder", None)
    if not callable(setter):
        return None
    store = ReportingStore(workspace)
    # Reuse the result tools' existing path/identity rules and revision namespace.
    parts = ListResultPartsTool(envelope.run_id, envelope.task_id, envelope.revision, store)
    root = store.workspace / parts.relative_root

    def render() -> str:
        drafts = []
        for path in sorted(root.glob("*.md")):
            binding = root / "_evidence" / f"{path.stem}.json"
            drafts.append({
                "part_id": path.stem,
                "artifact_ref": path.relative_to(store.workspace).as_posix(),
                "content": path.read_text(encoding="utf-8"),
                "evidence_binding": json.loads(binding.read_text(encoding="utf-8")) if binding.is_file() else None,
            })
        return render_prompt() + "\n\n" + json.dumps({
            "current_draft_parts": drafts,
            "draft_status": "Persisted working prose, not accepted conclusions; retain unresolved findings from the task input.",
        }, ensure_ascii=False, separators=(",", ":"))

    key = json.dumps([envelope.run_id, envelope.task_id, envelope.revision, output_contract], separators=(",", ":"))
    rebuilder = TaskContextRebuilder(key, render)
    setter(rebuilder)
    return rebuilder
