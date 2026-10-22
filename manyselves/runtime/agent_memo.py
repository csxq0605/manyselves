"""Content-addressed record of completed Agent turns for one Run.

A turn is keyed by everything the model saw: the Agent's system prompt, the
task message, the model-visible tool and output schemas, the image bytes and
the conversation history before the turn.  When a Run is re-executed in place
(after a resume that recompiled the plan, or a revision), a turn whose key is
unchanged is replayed from its record instead of calling the model again, and
its conversation continues exactly as it did the first time.

The record does not depend on workflow positions, conversation ids or action
ids, so it survives plan changes.  It knows nothing about any Capability.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .conversation_history import serialize_message

_MESSAGE_FIELDS = ("role", "content", "tool_call_id", "is_tool_result", "thinking")


def _safe_component(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_" for char in str(value)
    ) or "_"


def canonical_message(message: Any) -> dict[str, Any]:
    """Return the parts of one message the model sees, in a stable shape."""

    data = message if isinstance(message, Mapping) else serialize_message(message)
    out: dict[str, Any] = {field: data.get(field) for field in _MESSAGE_FIELDS}
    out["content"] = str(out.get("content") or "")
    out["is_tool_result"] = bool(out.get("is_tool_result"))
    out["tool_calls"] = [
        {
            "id": str(call.get("id") or ""),
            "name": str(call.get("name") or ""),
            "arguments": call.get("arguments") or {},
        }
        for call in (data.get("tool_calls") or ())
        if isinstance(call, Mapping)
    ]
    out["images"] = [
        {
            # Image files of a Run are content addressed; the file name is
            # stable while the absolute workspace path may move.
            "name": Path(str(image.get("path") or "")).name,
            "media_type": str(image.get("media_type") or ""),
        }
        for image in (data.get("images") or ())
        if isinstance(image, Mapping)
    ]
    return out


def canonical_history(messages: Iterable[Any]) -> list[dict[str, Any]]:
    return [canonical_message(message) for message in messages]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def turn_key(
    *,
    system_prompt: str,
    prompt: str,
    tool_schemas: Sequence[Any],
    output_schema: Any,
    image_digests: Sequence[str],
    history: Sequence[Any],
) -> str:
    """Fingerprint one turn from the model's point of view."""

    return _digest(
        {
            "system": system_prompt,
            "prompt": prompt,
            "tools": list(tool_schemas),
            "output": output_schema,
            "images": list(image_digests),
            "history": canonical_history(history),
        }
    )


class FileAgentTurnMemo:
    """One JSON record per completed turn under ``Work/runs/<run>/agent-memo``.

    The memo is kept across in-place re-executions of the Run: archiving a
    version moves results and histories, never the memo.
    """

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.root = (
            Path(workspace) / "Work" / "runs" / _safe_component(run_id) / "agent-memo"
        )

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        path = self.path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, Mapping) or payload.get("key") != key:
            return None
        return dict(payload)

    def save(
        self,
        key: str,
        *,
        result: Any,
        before: Sequence[Any],
        after: Sequence[Any],
        detail: Mapping[str, Any],
    ) -> None:
        before_rows = [_plain(message) for message in before]
        after_rows = [_plain(message) for message in after]
        prefix = len(before_rows)
        if len(after_rows) >= prefix and canonical_history(
            after_rows[:prefix]
        ) == canonical_history(before_rows):
            history = {"mode": "append", "messages": after_rows[prefix:]}
        else:
            # The turn rewrote earlier history (for example a compaction).
            history = {"mode": "replace", "messages": after_rows}
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"key": key, "result": result, "history": history, **dict(detail)},
                ensure_ascii=False,
                indent=1,
                default=str,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def history_after(record: Mapping[str, Any], before: Sequence[Any]) -> list[dict[str, Any]]:
        history = record.get("history") or {}
        messages = [dict(item) for item in history.get("messages") or ()]
        if history.get("mode") == "replace":
            return messages
        return [_plain(message) for message in before] + messages

    def log_hit(self, entry: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "hits.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), ensure_ascii=False, default=str) + "\n")


def _plain(message: Any) -> dict[str, Any]:
    return dict(message) if isinstance(message, Mapping) else serialize_message(message)


__all__ = [
    "FileAgentTurnMemo",
    "canonical_history",
    "canonical_message",
    "file_digest",
    "turn_key",
]
