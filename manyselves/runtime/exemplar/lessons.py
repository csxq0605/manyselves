"""Lessons: general writing rules learned from reviews, kept apart from the exemplar.

A lesson is an error type and a general rule, with where it came from, the
roles it is for, and a trigger the capability evaluates on a run's prepared
data.  Lessons live in their own file and are merged into role Skills only
when loaded, so each one can be revoked on its own.

The program refuses a lesson that names anything specific to the run it came
from (its objects, codes or values) or uses a mandatory word; whether its
trigger is meaningful is the capability's check.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

_NUMBER = re.compile(r"\d+(?:[.,．]\d+)*")
_SPACE = re.compile(r"\s+")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LessonSource(_Model):
    run: str = Field(min_length=1)
    finding: str = Field(min_length=1, description="The review finding the lesson came from")
    date: str = Field(min_length=1)


class Lesson(_Model):
    id: str = Field(pattern=r"^L-\d+$")
    roles: list[str] = Field(min_length=1)
    error_type: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    trigger: dict[str, Any] = Field(default_factory=lambda: {"kind": "always"})
    source: LessonSource
    status: Literal["active", "revoked"] = "active"
    revoked_reason: str = ""


class LessonBook(_Model):
    lessons: list[Lesson] = Field(default_factory=list)


def load_lessons(path: Path) -> LessonBook:
    if not path.is_file():
        return LessonBook()
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    book = LessonBook.model_validate(value)
    ids = [lesson.id for lesson in book.lessons]
    if len(ids) != len(set(ids)):
        raise ValueError(f"lesson ids must be unique: {path}")
    return book


def specific_terms(
    subjects: Iterable[str], vocabulary: Iterable[str]
) -> set[str]:
    """Object names of a run that are not words of the fixed product vocabulary."""

    words = [_SPACE.sub("", word) for word in vocabulary if word]
    terms: set[str] = set()
    for subject in subjects:
        term = _SPACE.sub("", subject or "")
        if len(term) >= 2 and not any(term in word for word in words):
            terms.add(term)
    return terms


def lesson_problems(
    *,
    roles: Sequence[str],
    error_type: str,
    rule: str,
    trigger: Mapping[str, Any],
    known_roles: Iterable[str],
    run_terms: Iterable[str],
    mandatory_words: Sequence[str],
    trigger_problem: Callable[[Mapping[str, Any]], str | None],
) -> list[str]:
    """Why a lesson draft cannot be kept; empty when it can."""

    problems: list[str] = []
    unknown = [role for role in roles if role not in set(known_roles)]
    if not roles:
        problems.append("没有写适用的角色")
    if unknown:
        problems.append("角色不存在：" + "、".join(unknown))
    text = f"{error_type}\n{rule}"
    compact = _SPACE.sub("", text)
    numbers = list(dict.fromkeys(_NUMBER.findall(text)))
    if numbers:
        problems.append("含具体数值或编号：" + "、".join(numbers) + "；教训只写错误类型和通用规则")
    named = sorted(term for term in set(run_terms) if term in compact)
    if named:
        problems.append("含本次运行的对象：" + "、".join(named))
    words = [word for word in mandatory_words if word in text]
    if words:
        problems.append("含强制词：" + "、".join(words) + "；硬规定放任务合同")
    issue = trigger_problem(trigger)
    if issue:
        problems.append(issue)
    return problems


def render_lessons(lessons: Iterable[Lesson]) -> str:
    rows = [f"- 易错：{lesson.error_type}。做法：{lesson.rule}" for lesson in lessons]
    if not rows:
        return ""
    return "\n".join(["", "## 以往评审总结的易错点", *rows]) + "\n"


def lessons_for(
    book: LessonBook,
    role_id: str,
    fires: Callable[[Mapping[str, Any]], bool],
) -> list[Lesson]:
    return [
        lesson
        for lesson in book.lessons
        if lesson.status == "active" and role_id in lesson.roles and fires(lesson.trigger)
    ]


def next_lesson_ids(book: LessonBook, count: int) -> list[str]:
    used = [int(lesson.id.split("-")[1]) for lesson in book.lessons]
    start = max(used, default=0) + 1
    return [f"L-{number:03d}" for number in range(start, start + count)]


__all__ = [
    "Lesson",
    "LessonBook",
    "LessonSource",
    "lesson_problems",
    "lessons_for",
    "load_lessons",
    "next_lesson_ids",
    "render_lessons",
    "specific_terms",
]
