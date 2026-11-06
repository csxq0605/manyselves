"""What the loss finder submits for one partition.

These models are the output contract of the finder's task, so a submission
that breaks them goes back to the model to be corrected in the same turn.
They hold only what can be checked without the run's data; checks against
the data (meters of the partition, cited evidence, method prerequisites) are
the ``check-partition-items`` tool's.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .methods import MENU, NO_METHOD, MethodParamsError, Window, overlapping_windows, parse_params

_KEY = r"^[A-Za-z0-9_-]{1,20}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RootCauseCandidate(_Strict):
    cause: str = Field(min_length=1, description="可能的原因")
    basis: Literal["data", "general_knowledge"] = Field(
        description="依据类型：data 数据现象；general_knowledge 通用工艺/设备知识"
    )
    basis_detail: str = Field(
        min_length=1,
        description="依据的具体出处：data 写本项 evidence_ids 里的证据编号（可加一句说明）；general_knowledge 写是哪条知识",
    )
    how_to_check: str = Field(min_length=1, description="人要核实或排除这个原因，需要看什么、问什么")


class FindingMethod(_Strict):
    method_id: str = Field(description="方法菜单里的 id；菜单里没有合适的方法时填 none")
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(min_length=1, description="用文字说清是怎么找到这一项的")

    @model_validator(mode="after")
    def known_method(self) -> "FindingMethod":
        if self.method_id == NO_METHOD:
            if self.params:
                raise ValueError("method_id none takes no params")
            return self
        try:
            parse_params(self.method_id, self.params)
        except MethodParamsError as exc:
            raise ValueError(str(exc)) from exc
        return self


class LossItem(_Strict):
    key: str = Field(pattern=_KEY, description="本分区内的编号，例如 A1")
    meter_ids: list[str] = Field(min_length=1)
    windows: list[Window] = Field(min_length=1, max_length=20, description="看到现象的具体时间窗")
    finding_method: FindingMethod
    phenomenon: str = Field(min_length=1, description="看到了什么；数字要来自引用的证据")
    evidence_ids: list[str] = Field(min_length=1)
    reasoning: str = Field(min_length=1, description="思考过程：从画像和查数一步步怎样看到这里")
    root_cause_candidates: list[RootCauseCandidate] = Field(min_length=1)
    missing_info: list[str] = Field(default_factory=list, description="判断这一项还缺的资料")
    monitoring_suggestion: str | None = None
    related_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct(self) -> "LossItem":
        if len(set(self.meter_ids)) != len(self.meter_ids):
            raise ValueError(f"item {self.key}: meter_ids repeat")
        if self.key in self.related_keys:
            raise ValueError(f"item {self.key} cannot be related to itself")
        clashes = overlapping_windows(self.windows)
        if clashes:
            raise ValueError(f"item {self.key}: windows must not overlap ({'; '.join(clashes)})")
        return self


class CoverageEntry(_Strict):
    method_id: str
    meter_ids: list[str] = Field(min_length=1)
    outcome: Literal["found", "none_found", "not_applicable", "needs_input"]
    note: str = Field(min_length=1, description="查了什么、为什么没找到、为什么不适用或缺什么")
    item_keys: list[str] = Field(default_factory=list, description="用这个方法找到的、你提交的损耗项 key")
    rule_keys: list[str] = Field(default_factory=list, description="用这个方法找到、已作为规则项列出的 rule_key")

    @model_validator(mode="after")
    def method_in_menu(self) -> "CoverageEntry":
        if self.method_id not in MENU:
            raise ValueError(f"coverage method {self.method_id!r} is not in the menu {sorted(MENU)}")
        listed = self.item_keys or self.rule_keys
        if self.outcome == "found" and not listed:
            raise ValueError(f"coverage of {self.method_id}: outcome found needs item_keys or rule_keys")
        if self.outcome != "found" and listed:
            raise ValueError(f"coverage of {self.method_id}: only outcome found lists item_keys or rule_keys")
        return self


class RuleItemNote(_Strict):
    rule_key: str = Field(min_length=1)
    note: str = Field(min_length=1)


class PartitionSubmission(_Strict):
    partition_id: str = Field(min_length=1)
    items: list[LossItem] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(min_length=1)
    rule_item_notes: list[RuleItemNote] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def consistent(self) -> "PartitionSubmission":
        keys = [item.key for item in self.items]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            raise ValueError(f"item keys repeat: {repeated}")
        known = set(keys)
        for item in self.items:
            unknown = sorted(set(item.related_keys) - known)
            if unknown:
                raise ValueError(f"item {item.key}: related_keys {unknown} are not items")
        covered = {entry.method_id for entry in self.coverage}
        missing = [method_id for method_id in MENU if method_id not in covered]
        if missing:
            raise ValueError(
                f"coverage must say for every menu method whether it was checked; missing {missing}"
            )
        for entry in self.coverage:
            unknown = sorted(set(entry.item_keys) - known)
            if unknown:
                raise ValueError(f"coverage of {entry.method_id}: item_keys {unknown} are not items")
        return self


__all__ = [
    "CoverageEntry",
    "FindingMethod",
    "LossItem",
    "PartitionSubmission",
    "RootCauseCandidate",
    "RuleItemNote",
]
