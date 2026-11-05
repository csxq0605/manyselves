"""Durable histories for Agent conversations of one Run.

A conversation's history is saved after each Agent invocation.  It lets a
restarted Run continue the same conversation, and lets a forked conversation
start from its source's completed turns when the source session is no longer
live.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass, replace
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


def _is_request(message: Any, request: str) -> bool:
    return (
        _get(message, "role") == "user"
        and not _get(message, "is_tool_result")
        and str(_get(message, "content") or "").strip() == str(request or "").strip()
    )


def _without_calls(message: Any, call_ids: set[str]) -> Any:
    """A copy of an assistant message without the tool calls ``call_ids``."""

    calls = [
        call
        for call in _get(message, "tool_calls") or ()
        if str(_get(call, "id") or "") not in call_ids
    ]
    if isinstance(message, Mapping):
        return {**message, "tool_calls": calls}
    return replace(message, tool_calls=calls)


def _accepted(message: Any) -> bool:
    """Whether a tool result says its submission was accepted."""

    try:
        payload = json.loads(str(_get(message, "content") or ""))
    except ValueError:
        return False
    return isinstance(payload, Mapping) and payload.get("accepted") is True


def drop_rejected_attempts(
    messages: Sequence[Any],
    start: int,
    *,
    request: str | None = None,
    terminal: str = "submit_result",
) -> list[Any]:
    """The conversation after one finished task, without its rejected attempts.

    ``messages[start:]`` is the task's own turn: its request, then the
    model's attempts.  When the last ``terminal`` call was accepted, the
    earlier attempts that only submitted (or said something without
    submitting), the results answering them and the correction prompts in
    between are removed: later turns read the accepted answer once, not a
    superseded copy next to it.  Calls to other tools and their results stay,
    also when a superseded submission was made in the same round as them.
    In the round of the accepted submission, the other ``terminal`` calls
    (rejected before it, or skipped after it) and their results go too.

    With ``request``, the turn's first message is kept as its request only
    when it is that request; compaction during the turn may have evicted
    the request, and then the turn starts with an attempt.
    """

    head, turn = list(messages[:start]), list(messages[start:])
    first = 1 if request is None or (turn and _is_request(turn[0], request)) else 0
    finals = [
        index
        for index, message in enumerate(turn)
        if _get(message, "role") == "assistant"
        and any(name == terminal for _id, name in _calls(message))
    ]
    if not finals:
        return list(messages)
    last = finals[-1]
    submissions = {call_id for call_id, name in _calls(turn[last]) if name == terminal}
    accepted = {
        _get(message, "tool_call_id")
        for message in turn[last + 1 :]
        if _get(message, "tool_call_id") in submissions and _accepted(message)
    }
    # The accepted submission is told by its result, not by its place: one
    # skipped after it comes later in the round.  When no result says so,
    # the round stays as it is.
    beside = submissions - accepted if accepted else set()
    dropped: set[str] = set(beside)
    kept: list[Any] = []
    for index, message in enumerate(turn):
        role = _get(message, "role")
        calls = _calls(message)
        if index == last and beside:
            message = _without_calls(message, beside)
        if first <= index < last:
            if role == "assistant" and all(name == terminal for _id, name in calls):
                dropped.update(call_id for call_id, _name in calls)
                continue
            if role == "user" and not _get(message, "is_tool_result"):
                continue
            superseded = {call_id for call_id, name in calls if name == terminal}
            if role == "assistant" and superseded:
                dropped |= superseded
                message = _without_calls(message, superseded)
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


__all__ = [
    "FileConversationHistoryStore",
    "drop_rejected_attempts",
    "serialize_message",
]
