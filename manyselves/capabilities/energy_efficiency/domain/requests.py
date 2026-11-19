"""Public inputs of the two entrypoints and the values passed between steps."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python

from .inputs import Manifest
from .submissions import JudgmentSubmission, LookSubmission, ResidualSubmission


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Plain(_Strict):
    """A request that carries an agent's answer as plain data: the runtime may hand the answer over as a model."""

    @model_validator(mode="before")
    @classmethod
    def plain(cls, data: Any) -> Any:
        return to_jsonable_python(data) if isinstance(data, dict) else data


class CandidatesRequest(_Strict):
    input_dir: str = Field(
        min_length=1,
        description="项目里放数据文件的文件夹；有 manifest.yaml 就按它读，没有时程序先探测文件、由模型起草一份",
    )
    meters: list[str] = Field(default_factory=list, description="只分析这些表（表编号）；不写时分析全部")
    start: datetime | None = Field(default=None, description="只分析这之后（站点本地时间）")
    end: datetime | None = Field(default=None, description="只分析这之前（站点本地时间）")
    focus: str | None = Field(default=None, max_length=500, description="关注点（某个系统、设备或问题）；只作为给模型的提示")

    @model_validator(mode="after")
    def ordered(self) -> "CandidatesRequest":
        for value in (self.start, self.end):
            if value is not None and value.tzinfo is not None:
                raise ValueError("start and end are site local time without a time zone")
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end must be after start")
        if len(set(self.meters)) != len(self.meters):
            raise ValueError("meters repeat")
        return self

    def task_scope(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include={"meters", "start", "end", "focus"})


class ReportRequest(_Strict):
    source_run_id: str = Field(min_length=1, description="出台账的那次运行")
    marks_file: str | None = Field(default=None, description="标好“取舍”列的台账（.xlsx 或 .csv）；不给时按规则文件和筛选结果")
    rules_file: str | None = Field(default=None, description="人写的选择规则（.yaml）；不给时按标记和筛选结果")


class RunRef(_Strict):
    run_id: str


class CheckRequest(_Plain):
    """A model's answer to one brief, with the brief's key and its run."""

    run_id: str
    key: str
    submission: dict[str, Any]


class LookFixRequest(_Strict):
    """The program's check of an answer, sent back with the answer."""

    run_id: str
    key: str
    check: dict[str, Any]
    submission: LookSubmission


class ResidualFixRequest(_Strict):
    """The program's check of an answer, sent back with the answer."""

    run_id: str
    key: str
    check: dict[str, Any]
    submission: ResidualSubmission


class JudgmentFixRequest(_Strict):
    """The program's check of an answer, sent back with the answer."""

    run_id: str
    key: str
    check: dict[str, Any]
    submission: JudgmentSubmission


class LookAttempt(_Strict):
    submission: LookSubmission
    check: dict[str, Any]


class ResidualAttempt(_Strict):
    submission: ResidualSubmission
    check: dict[str, Any]


class JudgmentAttempt(_Strict):
    submission: JudgmentSubmission
    check: dict[str, Any]


class SettleRequest(_Plain):
    """The first answer and check of one brief, and what the correction round returned."""

    key: str
    first_submission: dict[str, Any]
    first_check: dict[str, Any]
    corrected: dict[str, Any] = Field(default_factory=dict)
    correction_errors: dict[str, Any] = Field(default_factory=dict)


class SettledResult(_Strict):
    submission: dict[str, Any]
    check: dict[str, Any]
    corrected: bool = False
    notes: list[str] = Field(default_factory=list)


class ApplyLooksRequest(_Plain):
    run_id: str
    results: dict[str, SettledResult] = Field(default_factory=dict)
    errors: dict[str, Any] = Field(default_factory=dict)


class ResidualRoundRequest(_Strict):
    run_id: str
    round: int = Field(ge=1)


class ApplyResidualRequest(_Plain):
    run_id: str
    round: int = Field(ge=1)
    results: dict[str, SettledResult] = Field(default_factory=dict)
    errors: dict[str, Any] = Field(default_factory=dict)


class RegisterRequest(_Plain):
    run_id: str
    judge_results: dict[str, SettledResult] = Field(default_factory=dict)
    judge_errors: dict[str, Any] = Field(default_factory=dict)
    assumption_errors: dict[str, Any] = Field(default_factory=dict)


class DraftedAssumption(_Strict):
    """One inference about what the data measures, drafted from the program's facts."""

    topic: str = Field(min_length=1, description="关于什么，例如某块表的计量范围、数量列的口径")
    inference: str = Field(min_length=1, description="推断：可能是什么，写成待确认的话")
    reasoning: str = Field(min_length=1, description="从依据怎样推到这条推断；有别的可能也写上")
    basis_fact_ids: list[str] = Field(min_length=1, description="依据的编号：程序算的事实（如 <表编号>-MACHINES）或现场调研答复的编号")
    meter_ids: list[str] = Field(default_factory=list, description="涉及的表")
    affects: str = Field(default="", description="推断不成立时，哪些数字或线索会变（例如单耗、批间功率）")
    how_to_confirm: str = Field(min_length=1, description="现场怎样确认：看什么、问谁、比什么")


class AssumptionDraft(_Strict):
    assumptions: list[DraftedAssumption] = Field(default_factory=list)


class SettleAssumptionsRequest(_Strict):
    run_id: str
    draft: AssumptionDraft


class TryManifestRequest(_Strict):
    run_id: str
    manifest: Manifest


class ManifestFixRequest(_Strict):
    check: dict[str, Any]
    manifest: Manifest


class SettleManifestRequest(_Strict):
    run_id: str
    first: Manifest
    corrected: dict[str, Manifest] = Field(default_factory=dict)


class SelectRequest(_Strict):
    run_id: str
    request: dict[str, Any]


class RenderRequest(_Strict):
    run_id: str
    request: dict[str, Any]
    selection: dict[str, Any]


__all__ = [
    "ApplyLooksRequest",
    "ApplyResidualRequest",
    "AssumptionDraft",
    "CandidatesRequest",
    "CheckRequest",
    "DraftedAssumption",
    "JudgmentAttempt",
    "JudgmentFixRequest",
    "LookAttempt",
    "LookFixRequest",
    "ManifestFixRequest",
    "RegisterRequest",
    "RenderRequest",
    "ReportRequest",
    "ResidualAttempt",
    "ResidualFixRequest",
    "ResidualRoundRequest",
    "RunRef",
    "SelectRequest",
    "SettleAssumptionsRequest",
    "SettleManifestRequest",
    "SettleRequest",
    "SettledResult",
    "TryManifestRequest",
]
