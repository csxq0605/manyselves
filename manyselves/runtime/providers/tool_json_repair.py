"""Repair the few JSON slips a model makes in a long streamed tool input.

A model writing a 30k-character tool input sometimes slips once: a comma
before a closing bracket, an object it forgot to close before the next one
starts, a doubled comma, or a raw line break inside a string.  Resampling the
whole answer costs as long as writing it.  Each repair here keeps every value
the model wrote; anything else is left for a fresh sample.  The repaired value
still goes through the tool's contract, which rejects a wrong structure.
"""

from __future__ import annotations

import json
from typing import Any

MAX_REPAIRS = 4


def _previous(text: str, position: int) -> int:
    """Index of the last non-space character before ``position``, or -1."""

    index = position - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    return index


def _next(text: str, position: int) -> int:
    index = position
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _repair_once(text: str, error: json.JSONDecodeError) -> tuple[str, str] | None:
    at = _next(text, error.pos)
    if at >= len(text):
        return None
    found = text[at]
    before = _previous(text, at)
    after_comma = before >= 0 and text[before] == ","
    if error.msg.startswith("Expecting property name enclosed in double quotes"):
        if after_comma and found == "}":
            return text[:before] + text[before + 1:], "comma before }"
        if after_comma and found == ",":
            return text[:at] + text[at + 1:], "doubled comma"
        if after_comma and found == "{":
            # An object left open before the next one in the list starts.
            return text[:before] + "}" + text[before:], "unclosed object before {"
        return None
    if error.msg.startswith("Expecting value") and after_comma and found in "]}":
        return text[:before] + text[before + 1:], f"comma before {found}"
    return None


def repair_tool_json(text: str) -> tuple[Any, list[str]] | None:
    """The parsed value and the repairs made, or None when it cannot be repaired.

    Raw control characters inside strings (a line break the model wrote
    unescaped) are accepted as they are.
    """

    repairs: list[str] = []
    for _ in range(MAX_REPAIRS + 1):
        try:
            return json.loads(text, strict=False), repairs
        except json.JSONDecodeError as error:
            repaired = _repair_once(text, error)
            if repaired is None:
                return None
            text, what = repaired
            repairs.append(f"{what} at {error.pos}")
    return None


__all__ = ["repair_tool_json"]
