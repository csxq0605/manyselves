"""The shape of a review triage: where each finding goes, and lesson drafts.

A finding the program can check goes to program checks; one about how inputs
are read or what a task must do goes to the task contract; only a writing
problem becomes a lesson.  Lesson drafts are checked by program before they
are offered for adoption.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Route = Literal["program_check", "task_contract", "lesson", "no_action"]
TriggerKind = Literal["always", "mapping_gap", "fact_flag", "calculation_gap"]

ROUTE_HELP = (
    "program_check=程序能核对的错误（数量、配对、编号、格式）；"
    "task_contract=输入怎么读、任务必须做什么的问题；"
    "lesson=只有写法问题才进教训；no_action=不是错误或已经处理。"
)
TRIGGER_HELP = (
    "always=任何项目都适用；mapping_gap=资料映射出现某种缺口时适用，value 填缺口代码；"
    "fact_flag=有事实带某个标记时适用，value 填 needs_confirmation 或 summary_entry；"
    "calculation_gap=有计算因缺资料而做不了时适用。"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LessonTrigger(_Model):
    kind: TriggerKind = Field(description=TRIGGER_HELP)
    value: str = ""


class LessonDraft(_Model):
    roles: list[str] = Field(min_length=1, description="适用的写作角色编号")
    error_type: str = Field(min_length=1, description="错误类型，一句话，不带本次的对象和数值")
    rule: str = Field(min_length=1, description="通用做法，一句到两句，换一个项目也成立")
    trigger: LessonTrigger


class TriagedFinding(_Model):
    finding_ids: list[str] = Field(min_length=1)
    route: Route = Field(description=ROUTE_HELP)
    reason: str = Field(min_length=1)
    lesson: LessonDraft | None = None

    @model_validator(mode="after")
    def lesson_only_for_lessons(self) -> "TriagedFinding":
        if (self.route == "lesson") != (self.lesson is not None):
            raise ValueError("lesson is required for route=lesson and only for it")
        return self


class LessonTriage(_Model):
    items: list[TriagedFinding] = Field(default_factory=list)


__all__ = [
    "LessonDraft",
    "LessonTriage",
    "LessonTrigger",
    "ROUTE_HELP",
    "Route",
    "TRIGGER_HELP",
    "TriagedFinding",
    "TriggerKind",
]
