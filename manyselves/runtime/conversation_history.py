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


def _get(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, Mapping) else getattr(message, name, None)


def _calls(message: Any) -> list[tuple[str, str]]:
    return [
        (str(_get(call, "id") or ""), str(_get(call, "name") or ""))
        for call in _get(message, "tool_calls") or ()
    ]


def drop_rejected_attempts(
    messages: Sequence[Any], start: int, *, terminal: str = "submit_result"
) -> list[Any]:
    """The conversation after one finished task, without its rejected attempts.

    ``messages[start:]`` is the task's own turn: its request, then the
    model's attempts.  When the last ``terminal`` call was accepted, the
    earlier attempts that only submitted (or said something without
    submitting), the results answering them and the correction prompts in
    between are removed: later turns read the accepted answer once, not a
    superseded copy next to it.  Calls to other tools and their results stay.
    """

    head, turn = list(messages[:start]), list(messages[start:])
    finals = [
        index
        for index, message in enumerate(turn)
        if _get(message, "role") == "assistant"
        and any(name == terminal for _id, name in _calls(message))
    ]
    if not finals:
        return list(messages)
    last = finals[-1]
    dropped: set[str] = set()
    kept: list[Any] = []
    for index, message in enumerate(turn):
        role = _get(message, "role")
        calls = _calls(message)
        if 0 < index < last:
            if role == "assistant" and all(name == terminal for _id, name in calls):
                dropped.update(call_id for call_id, _name in calls)
                continue
            if role == "user" and not _get(message, "is_tool_result"):
                continue
        if _get(message, "tool_call_id") in dropped:
            continue
        kept.append(message)
    return head + kept


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


__all__ = ["FileConversationHistoryStore", "drop_rejected_attempts", "serialize_message"]
