"""What the loss finder submits for one partition.

These models are the output contract of the finder's task, so a submission
that breaks them goes back to the model to be corrected in the same turn.
They hold only what can be checked without the run's data; checks against
the data (meters of the partition, cited evidence, method prerequisites) are
the ``check-partition-items`` tool's.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .methods import MENU, NO_METHOD, MethodParamsError, Window, overlapping_windows, parse_params

_KEY = r"^[A-Za-z0-9_-]{1,20}$"

# The 17 stable items of step 4 of the knowledge base, with their role.
KB_ITEMS: dict[str, tuple[str, str]] = {
    "LOSS-PROCESS": ("ENERGY", "同条件加工超耗"),
    "LOSS-INTERRUPTION": ("ENERGY", "加工中断可避免维持用电"),
    "LOSS-IDLE": ("ENERGY", "批间隙或计划外待机超耗"),
    "LOSS-HOLD": ("ENERGY", "休息日保温策略净超耗"),
    "LOSS-TRANSITION": ("ENERGY", "同切换对过渡作业超耗"),
    "LOSS-PARALLEL": ("ENERGY", "可压缩并行窗口超耗"),
    "LOSS-SUPPORT": ("ENERGY", "公辅供给或运行组合失配"),
    "LOSS-CONDITIONAL": ("ENERGY", "归一条件后的残余超耗"),
    "MECH-CONTROL": ("MECHANISM", "控制覆盖或故障维护机制"),
    "OPP-CAPABILITY": ("OPPORTUNITY", "跨能力档/规模/经济批量排产机会"),
    "COST-SHIFT": ("ECONOMIC", "完整工艺周期移峰净节费"),
    "COST-BASIC": ("ECONOMIC", "基本电费方案与峰值管理机会"),
    "COST-REACTIVE": ("ECONOMIC", "力调费用差额"),
    "COST-STORAGE": ("ECONOMIC", "储能联合调度净收益"),
    "COST-PROCURE": ("ECONOMIC", "购电方案全成本差额"),
    "NECESSARY": ("NON_LOSS", "必要工艺/环境/安全服务耗能"),
    "MEASUREMENT_ISSUE": ("DATA_ISSUE", "计量或时间口径待核"),
}
KbItemId = Literal[
    "LOSS-PROCESS",
    "LOSS-INTERRUPTION",
    "LOSS-IDLE",
    "LOSS-HOLD",
    "LOSS-TRANSITION",
    "LOSS-PARALLEL",
    "LOSS-SUPPORT",
    "LOSS-CONDITIONAL",
    "MECH-CONTROL",
    "OPP-CAPABILITY",
    "COST-SHIFT",
    "COST-BASIC",
    "COST-REACTIVE",
    "COST-STORAGE",
    "COST-PROCURE",
    "NECESSARY",
    "MEASUREMENT_ISSUE",
]
# How two items relate (knowledge base 4.7.1).
RELATIONS: dict[str, str] = {
    "alternative_explanation": "同一事实的替代解释",
    "same_event_evidence": "同一事件的不同证据",
    "parent_child": "父子分解",
    "mutually_exclusive": "并行但互斥",
    "independent": "独立事件",
    "joint_scenario": "联合情景",
}
Relation = Literal[
    "alternative_explanation", "same_event_evidence", "parent_child", "mutually_exclusive", "independent", "joint_scenario"
]
PLANT = "PLANT"
# What a category may be short of (coverage outcome needs_input); the program
# checks the ones the inputs decide: production records, quantity, calendar, tariff.
INPUT_KINDS: dict[str, str] = {
    "production_records": "生产记录（或本表没挂机台）",
    "quantity": "经声明口径的批次数量",
    "qualified_quantity": "合格品量",
    "product": "批次的产品",
    "calendar": "经人确认的日历",
    "tariff": "电价",
    "meter_topology": "表间上下级关系或计量范围",
    "service_parameters": "服务参数（流量、压力、温度等）",
    "control_logs": "控制、告警日志",
    "maintenance_logs": "维护、检修记录",
    "bills_contracts": "账单、合同、需量",
    "environment": "环境数据（气温等）",
    "other": "其他（note 里写明）",
}
InputKind = Literal[
    "production_records",
    "quantity",
    "qualified_quantity",
    "product",
    "calendar",
    "tariff",
    "meter_topology",
    "service_parameters",
    "control_logs",
    "maintenance_logs",
    "bills_contracts",
    "environment",
    "other",
]
CATEGORY_OUTCOMES: dict[str, str] = {
    "none_found": "看过，没找到",
    "not_applicable": "不适用",
    "needs_input": "缺资料",
}


@lru_cache(maxsize=1)
def kb_categories() -> list[dict[str, str]]:
    """The 17 items of step 4 as the knowledge base words them: scope, evidence, pitfalls."""

    path = Path(__file__).with_name("kb_categories.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    categories = [
        {**entry, "role": KB_ITEMS[entry["id"]][0]} for entry in data["categories"]
    ]
    assert [entry["id"] for entry in categories] == list(KB_ITEMS)
    return categories


CAUSE_KINDS: dict[str, str] = {
    "loss": "可能是损耗",
    "normal_operation": "正常运行或工艺需要",
    "necessary_service": "必要服务",
    "measurement": "计量或时间口径问题",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RootCauseCandidate(_Strict):
    cause: str = Field(min_length=1, description="可能的原因")
    kind: Literal["loss", "normal_operation", "necessary_service", "measurement"] = Field(
        description="这个解释属于哪种：loss 可能是损耗；normal_operation 正常运行或工艺需要；"
        "necessary_service 必要服务；measurement 计量或时间口径问题"
    )
    basis: Literal["data", "site_fact", "general_knowledge"] = Field(
        description="依据类型：data 数据现象；site_fact 现场调研答复或数据口径推断；general_knowledge 通用工艺/设备知识"
    )
    basis_detail: str = Field(
        min_length=1,
        description=(
            "依据的具体出处：data 写本项 evidence_ids 里的证据编号（可加一句说明）；"
            "site_fact 写分区说明里 site_facts 或 assumptions 的编号；general_knowledge 写是哪条知识"
        ),
    )
    how_to_check: str = Field(min_length=1, description="人要核实或排除这个原因，需要看什么、问什么")


class CategoryProposal(_Strict):
    item_id: KbItemId = Field(description="知识库第 4 步 17 个稳定项之一")
    why: str = Field(min_length=1, description="为什么可能属于这一类：数据上看到了什么")


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
    root_cause_candidates: list[RootCauseCandidate] = Field(min_length=2)
    candidate_categories: list[CategoryProposal] = Field(
        min_length=1, description="可能对应知识库 17 项里的哪几项（可多选；只是提议，人来定）"
    )
    possible_necessary_services: list[str] = Field(
        default_factory=list, description="这段用电里可能有哪些必要服务（工艺、环保、质量、安全），由人确认"
    )
    missing_info: list[str] = Field(default_factory=list, description="判断这一项还缺的资料")
    depends_on_assumptions: list[str] = Field(
        default_factory=list,
        description="这一项的数字或判断建立在哪几条数据口径推断上（assumptions 的编号）；没有就留空",
    )
    monitoring_suggestion: str | None = None
    related_keys: list[str] = Field(default_factory=list)
    relation_types: dict[str, Relation] = Field(
        default_factory=dict, description="related_keys 里每一项与本项是什么关系"
    )

    @model_validator(mode="after")
    def distinct(self) -> "LossItem":
        if len(set(self.meter_ids)) != len(self.meter_ids):
            raise ValueError(f"item {self.key}: meter_ids repeat")
        if self.key in self.related_keys:
            raise ValueError(f"item {self.key} cannot be related to itself")
        if set(self.relation_types) != set(self.related_keys):
            raise ValueError(f"item {self.key}: relation_types must give a relation for each of related_keys and no other")
        if all(cause.kind == "loss" for cause in self.root_cause_candidates):
            raise ValueError(
                f"item {self.key}: root_cause_candidates need at least one explanation that is not a loss "
                "(normal_operation, necessary_service or measurement)"
            )
        ids = [proposal.item_id for proposal in self.candidate_categories]
        if len(set(ids)) != len(ids):
            raise ValueError(f"item {self.key}: candidate_categories repeat")
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


class CategoryCoverage(_Strict):
    item_id: KbItemId = Field(description="知识库第 4 步 17 个稳定项之一")
    meter_ids: list[str] = Field(min_length=1, description="这条说明针对哪些表")
    outcome: Literal["none_found", "not_applicable", "needs_input"] = Field(
        description="none_found 看过没找到；not_applicable 这些表上不适用；needs_input 缺资料"
    )
    missing_inputs: list[InputKind] = Field(
        default_factory=list, description="needs_input 时缺哪几类资料"
    )
    note: str = Field(min_length=1, description="看了什么、为什么不适用、缺什么")

    @model_validator(mode="after")
    def missing_named(self) -> "CategoryCoverage":
        if self.outcome == "needs_input" and not self.missing_inputs:
            raise ValueError(f"category coverage of {self.item_id}: needs_input names missing_inputs")
        if self.outcome != "needs_input" and self.missing_inputs:
            raise ValueError(f"category coverage of {self.item_id}: only needs_input names missing_inputs")
        if len(set(self.meter_ids)) != len(self.meter_ids):
            raise ValueError(f"category coverage of {self.item_id}: meter_ids repeat")
        return self


class ItemLink(_Strict):
    """A relation the plant-wide pass declares; a side is an item key of this
    submission or a partition item written as <partition id>:<key>, e.g. P01:A1."""

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    relation: Relation
    note: str = Field(min_length=1, description="为什么有这个关系")


class RuleItemNote(_Strict):
    rule_key: str = Field(min_length=1)
    note: str = Field(min_length=1)


class PartitionSubmission(_Strict):
    partition_id: str = Field(min_length=1)
    items: list[LossItem] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    category_coverage: list[CategoryCoverage] = Field(
        default_factory=list,
        description="17 类里本分区（全厂关联时：跨对象）没有损耗项提议的类别，逐类写看过没找到、不适用或缺资料",
    )
    links: list[ItemLink] = Field(
        default_factory=list, description="只在全厂关联时写：本次的项和各分区已有项之间、或已有项之间的关系"
    )
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
        if self.links and self.partition_id != PLANT:
            raise ValueError("links are written only by the plant-wide pass")
        covered = {entry.method_id for entry in self.coverage}
        missing = [method_id for method_id in MENU if method_id not in covered]
        if missing and self.partition_id != PLANT:
            raise ValueError(
                f"coverage must say for every menu method whether it was checked; missing {missing}"
            )
        for entry in self.coverage:
            unknown = sorted(set(entry.item_keys) - known)
            if unknown:
                raise ValueError(f"coverage of {entry.method_id}: item_keys {unknown} are not items")
        return self


__all__ = [
    "CATEGORY_OUTCOMES",
    "INPUT_KINDS",
    "PLANT",
    "CategoryCoverage",
    "ItemLink",
    "kb_categories",
    "CAUSE_KINDS",
    "KB_ITEMS",
    "RELATIONS",
    "CategoryProposal",
    "CoverageEntry",
    "FindingMethod",
    "LossItem",
    "PartitionSubmission",
    "RootCauseCandidate",
    "RuleItemNote",
]
