"""Durable full histories for Agent conversations of one Run.

A conversation's history is saved after each Agent invocation.  It lets a
restarted Run continue the same conversation, and lets a forked conversation
start from its source's completed turns when the source session is no longer
live.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def _safe_component(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_" for char in str(value)
    ) or "_"


def serialize_message(message: Any) -> dict[str, Any]:
    """Return one LLM message as JSON-ready data that ``restore_conversation`` reads."""

    if isinstance(message, Mapping):
        return json.loads(json.dumps(dict(message), ensure_ascii=False, default=str))
    if not is_dataclass(message):
        raise TypeError(f"cannot serialize conversation message: {type(message)!r}")
    data = asdict(message)
    data["images"] = [
        image.to_json() if hasattr(image, "to_json") else dict(image)
        for image in getattr(message, "images", ()) or ()
    ]
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


class FileConversationHistoryStore:
    """Store one JSON message list per conversation under the Run directory."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.root = (
            Path(workspace) / "Work" / "runs" / _safe_component(run_id) / "conversations"
        )

    def path(self, conversation_id: str) -> Path:
        return self.root / f"{_safe_component(conversation_id)}.json"

    def load(self, conversation_id: str) -> list[dict[str, Any]] | None:
        path = self.path(conversation_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, Mapping)
            or payload.get("conversation_id") != conversation_id
            or not isinstance(payload.get("messages"), list)
        ):
            raise ValueError(
                f"persisted conversation history does not match: {conversation_id}"
            )
        return list(payload["messages"])

    def save(self, conversation_id: str, messages: Sequence[Any]) -> None:
        path = self.path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "conversation_id": conversation_id,
                    "messages": [serialize_message(message) for message in messages],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)


__all__ = ["FileConversationHistoryStore", "serialize_message"]
