"""Authoritative reporting inputs and current durable drafts for Provider dispatch."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.runtime.loops.context_rebase import TaskContextRebuilder, atomic_tool_units
from manyselves.runtime.providers.base import Message

from .collaboration_tools import ListResultPartsTool
from .models.agentic import TaskEnvelope
from .observation_ledger import reference_owners
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
    return render_fact_packet(
        index.module_items(module_id, scope), module_id, submodule_ids=scope,
        owners=reference_owners(workspace, envelope.run_id),
    )


def replace_inline_fact_packet(inline: str | None, packet: str) -> str:
    """Replace the prepared leading packet, leaving knowledge/instructions intact."""
    text = inline or ""
    if text.startswith("<project_facts ") and "</project_facts>" in text:
        text = text.split("</project_facts>", 1)[1].lstrip()
    return '<project_facts delivery_mode="inline">\n' + packet + "</project_facts>\n\n" + text


def _project_persisted_writes(messages: list[Message], drafts: list[dict]) -> list[Message]:
    """Omit only complete successful writes reproduced exactly in current state.

    No placeholder is substituted into tool arguments. Mixed tool units, failed
    writes, superseded prose and assistant reasoning remain verbatim.
    """
    by_part = {draft["part_id"]: draft for draft in drafts}
    redundant: set[int] = set()
    for unit in atomic_tool_units(messages):
        if unit.assistant.content or getattr(unit.assistant, "thinking", None):
            continue
        results = {result.tool_call_id: result for result in unit.results}
        matched = True
        for call in unit.assistant.tool_calls:
            draft = by_part.get(call.arguments.get("part_id"))
            if call.name != "write_result_part" or draft is None:
                matched = False
                break
            try:
                result = json.loads(results[call.id].content)
            except (TypeError, ValueError, KeyError):
                matched = False
                break
            binding = draft["evidence_binding"]
            expected_ids = binding.get("evidence_ids") if isinstance(binding, dict) else None
            expected_assessments = binding.get("assessments") if isinstance(binding, dict) else None
            if not (
                isinstance(result, dict) and result.get("persisted") is True
                and result.get("part_id") == draft["part_id"]
                and result.get("artifact_ref") == draft["artifact_ref"]
                and call.arguments.get("content") == draft["content"]
                and call.arguments.get("evidence_ids") == expected_ids
                and result.get("evidence_ids") == expected_ids
                and call.arguments.get("assessments") == expected_assessments
                and result.get("assessments") == expected_assessments
            ):
                matched = False
                break
        if matched:
            redundant.update(id(message) for message in (unit.assistant, *unit.results))
    return [message for message in messages if id(message) not in redundant]


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
    current_drafts: list[dict] = []

    def render() -> str:
        nonlocal current_drafts
        drafts = []
        for path in sorted(root.glob("*.md")):
            binding = root / "_evidence" / f"{path.stem}.json"
            drafts.append({
                "part_id": path.stem,
                "artifact_ref": path.relative_to(store.workspace).as_posix(),
                "content": path.read_text(encoding="utf-8"),
                "evidence_binding": json.loads(binding.read_text(encoding="utf-8")) if binding.is_file() else None,
            })
        current_drafts = drafts
        return json.dumps({
            "current_draft_parts": drafts,
            "draft_status": "Persisted working prose, not accepted conclusions; retain unresolved findings from the task input.",
        }, ensure_ascii=False, separators=(",", ":"))

    key = json.dumps([envelope.run_id, envelope.task_id, envelope.revision, output_contract], separators=(",", ":"))
    rebuilder = TaskContextRebuilder(
        key, render, project_history=lambda messages: _project_persisted_writes(messages, current_drafts),
        render_prefix=render_prompt,
    )
    setter(rebuilder)
    return rebuilder
