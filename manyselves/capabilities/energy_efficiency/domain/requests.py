"""Public inputs of the two entrypoints and the values passed between steps."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .items import PartitionSubmission


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidatesRequest(_Strict):
    input_dir: str = Field(min_length=1, description="项目里放 manifest.yaml 和数据文件的文件夹")
    max_meters_per_partition: int = Field(default=6, ge=1, le=30)


class ReportRequest(_Strict):
    source_run_id: str = Field(min_length=1, description="出候选清单的那次运行")
    marks_file: str | None = Field(default=None, description="标好“选择”列的清单（.xlsx 或 .csv）")
    rules_file: str | None = Field(default=None, description="人写的选择规则（.yaml）")

    @model_validator(mode="after")
    def something_to_select_with(self) -> "ReportRequest":
        if not self.marks_file and not self.rules_file:
            raise ValueError("give marks_file, rules_file or both")
        return self


class RunRef(_Strict):
    run_id: str


class PartitionRequest(_Strict):
    run_id: str
    max_meters: int = Field(ge=1)


class CheckRequest(_Strict):
    scope: str
    submission: PartitionSubmission


class FixRequest(_Strict):
    """The check result and the submission it is about, sent back to the finder."""

    scope: str
    check: dict[str, Any]
    submission: PartitionSubmission


class PartitionAttempt(_Strict):
    """A corrected submission and the program's recheck of it."""

    submission: PartitionSubmission
    check: dict[str, Any]


class SettleRequest(_Strict):
    partition_id: str
    first_submission: PartitionSubmission
    first_check: dict[str, Any]
    corrected: dict[str, PartitionAttempt] = Field(default_factory=dict)
    correction_errors: dict[str, Any] = Field(default_factory=dict)


class PartitionResult(_Strict):
    """One partition's settled submission, its last check and partition notes."""

    partition_id: str
    submission: PartitionSubmission
    check: dict[str, Any]
    notes: list[str] = Field(default_factory=list)


class QuantifyRequest(_Strict):
    run_id: str
    partition_results: dict[str, PartitionResult]
    partition_errors: dict[str, Any] = Field(default_factory=dict)


class SelectRequest(_Strict):
    run_id: str
    request: dict[str, Any]


class RenderRequest(_Strict):
    run_id: str
    request: dict[str, Any]
    selection: dict[str, Any]


__all__ = [
    "CandidatesRequest",
    "CheckRequest",
    "FixRequest",
    "PartitionAttempt",
    "PartitionRequest",
    "PartitionResult",
    "QuantifyRequest",
    "RenderRequest",
    "ReportRequest",
    "RunRef",
    "SelectRequest",
    "SettleRequest",
]
