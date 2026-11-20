"""What the model hands in: loss items for a system or the residual, specs, site questions and judgments.

The analyst proposes the loss items: each adopts some of the program's
differences or brings a reference spec, and says how it was found, what the
thinking was and which causes are possible (at least one that is not a loss).
The program computes every item over the whole period and screens it.  The
model never writes an amount: numbers in its text are read back against the
program's and flagged when they match none.  Every field it fills is an
opinion; the screening never reads one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .preparation import Role
from .probes import TestRequest
from .specs import Basis, Spec


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteQuestion(_Strict):
    question: str = Field(min_length=1, description="能直接去现场问的话")
    ask_role: Role = Field(description="问谁：roles 里的键")
    objects: list[str] = Field(default_factory=list, description="涉及的表、机台或设备")
    settles: list[str] = Field(default_factory=list, description="问清后能定的触发、规格、候选或原因编号")


class Cause(_Strict):
    id: str = Field(pattern=r"^C\d{1,2}$", description="C1、C2…，本项（或本候选）内编号")
    cause: str = Field(min_length=1, description="候选原因（含非损耗的解释）")
    kind: Literal["loss", "necessary", "data", "external"] = Field(description="loss 浪费；necessary 必要服务或工艺需要；data 数据问题；external 外部驱动")
    basis: list[Basis] = Field(default_factory=list)
    test: TestRequest | None = Field(default=None, description="程序能跑的检验（从 test_types 里选），没有就不写")
    status: Literal["open", "kept", "dropped"] = Field(default="open", description="提项时写 open；判断时按检验结果写：kept 保留、dropped 数据矛盾放弃、open 未定")


ITEM_ID = r"^[A-Za-z][A-Za-z0-9_]{0,19}$"


class ItemProposal(_Strict):
    """One loss item the analyst proposes; the program computes it over the whole period."""

    id: str = Field(pattern=ITEM_ID, description="本次提交里的编号，如 I1；程序会加上系统或轮次的前缀")
    title: str = Field(min_length=1, max_length=120, description="一句话：哪块表、什么时候、比什么多用了电")
    differences: list[str] = Field(default_factory=list, description="采用的程序差额编号（differences 里的 id），可以几条；采用后这一行的数就是这条差额的全期数")
    spec: str | None = Field(default=None, description="或者（也可以同时）：本次 specs 里一条参照规格的编号，程序按它在全期回算")
    finding: str = Field(min_length=1, description="寻找方式：从哪条差额、哪种比较、哪条问卷或领域常识看出来的，参照是什么")
    reasoning: str = Field(min_length=1, description="思考过程：为什么像损耗；还查了什么，排除了什么")
    causes: list[Cause] = Field(min_length=1, description="候选原因，至少一条不是损耗（necessary、data 或 external）")
    missing_info: str = Field(default="", description="还缺什么资料才能定")
    related: list[str] = Field(default_factory=list, description="相关项的编号（本次提交里的，或已有项的完整编号）")


class SetAside(_Strict):
    """Why a difference large enough to matter is not part of any item."""

    difference: str = Field(description="程序差额编号")
    reason: Literal["necessary", "external", "data", "not_comparable", "cannot_tell"] = Field(
        description="necessary 必要服务或工艺需要；external 外部驱动（天气、作息、别的系统）；data 数据或口径问题；not_comparable 参照不可比；cannot_tell 看不出"
    )
    note: str = Field(min_length=1, description="依据是什么")


class TriggerAnswer(_Strict):
    trigger: str = Field(description="触发编号（triggers 里的 id）")
    disposition: Literal["necessary_service", "possible_waste", "comparable_group", "driver", "needs_data", "not_a_problem"] = Field(
        description="necessary_service 必要服务；possible_waste 可能浪费（提一个项）；comparable_group 能比（交可比分组规格）；"
        "driver 别的东西在推动（交驱动假设或剩余解释）；needs_data 要补资料（写现场问题或 request）；not_a_problem 数据看不出问题（写理由）"
    )
    items: list[str] = Field(default_factory=list, description="possible_waste：对应的项编号（本次提交里的）")
    specs: list[str] = Field(default_factory=list, description="这条处置对应的规格编号（本次提交里的）")
    note: str = Field(min_length=1, description="为什么这样处置，依据是什么")


class Mechanism(_Strict):
    """One mechanism the domain or the survey suggests for this system, and what came of it."""

    mechanism: str = Field(min_length=1, description="机理：例如 休息日供冷、空压管网泄漏、照明随作息")
    objects: list[str] = Field(default_factory=list, description="涉及的表")
    outcome: Literal["item", "no_evidence", "not_applicable", "site_question"] = Field(
        description="item 提了项（写 items）；no_evidence 数据里没看到；not_applicable 本系统不适用；site_question 数据看不了，转现场问题"
    )
    items: list[str] = Field(default_factory=list, description="item：项编号（本次提交里的）")
    basis: list[Basis] = Field(default_factory=list)
    note: str = Field(min_length=1)


class LookSubmission(_Strict):
    """S2: the loss items of one system, and why the large differences left out are left out."""

    key: str = Field(description="说明包的 key，原样填")
    items: list[ItemProposal] = Field(default_factory=list, description="提出的损耗项")
    set_aside: list[SetAside] = Field(default_factory=list, description="must_answer 里没被任何项采用的差额，每条写为什么暂不立项")
    triggers: list[TriggerAnswer] = Field(default_factory=list, description="triggers 里每个触发各一条")
    mechanisms: list[Mechanism] = Field(default_factory=list, description="领域先验和问卷检查：本系统该看的机理，每条写结果")
    specs: list[Spec] = Field(default_factory=list)
    site_questions: list[SiteQuestion] = Field(default_factory=list)
    note: str = ""


class SliceAnswer(_Strict):
    slice: str = Field(description="剩余片编号（slices 里的 id）")
    disposition: Literal["explained", "new_item", "cannot_tell", "needs_data"] = Field(
        description="explained 交剩余解释规格；new_item 提一个项（像浪费）；cannot_tell 看不出；needs_data 要补资料"
    )
    items: list[str] = Field(default_factory=list, description="new_item：项编号（本次提交里的）")
    specs: list[str] = Field(default_factory=list)
    note: str = Field(min_length=1)


class ResidualSubmission(_Strict):
    """S4: one round over the largest unexplained pieces."""

    key: str
    slices: list[SliceAnswer] = Field(default_factory=list, description="slices 里每片各一条")
    items: list[ItemProposal] = Field(default_factory=list)
    specs: list[Spec] = Field(default_factory=list)
    site_questions: list[SiteQuestion] = Field(default_factory=list)
    note: str = ""


class Measure(_Strict):
    type: Literal["management", "operation", "control", "repair", "retrofit"] = Field(
        description="management 管理；operation 运行调整；control 控制改造；repair 检修；retrofit 改造。原因没确认时只给前两类，"
        "检修、改造要写 depends_on，且所依的原因检验是支持"
    )
    action: str = Field(min_length=1)
    owner_role: Role
    depends_on: list[str] = Field(default_factory=list, description="依赖的原因编号")


class Judgment(_Strict):
    candidate: str = Field(description="行编号（judge 里的 id）")
    opinion: Literal["likely_loss", "likely_necessary", "data_problem", "unclear"] = Field(
        description="意见（不进筛选）：likely_loss 像浪费；likely_necessary 像必要；data_problem 像数据问题；unclear 看不出"
    )
    reference_choice: str = Field(description="levels 里你认为合适的那一档的 label；都不合适写 other")
    reference_reason: str = Field(min_length=1, description="理由只谈可比性：是不是同一场景、同一产品，有没有特殊情况")
    causes: list[Cause] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    site_questions: list[SiteQuestion] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list, description="相关候选编号（同因、互斥、上下级）")
    note: str = ""


class JudgmentSubmission(_Strict):
    key: str
    judgments: list[Judgment] = Field(default_factory=list, description="judge 里每个候选各一条")


__all__ = [
    "ITEM_ID",
    "Cause",
    "ItemProposal",
    "Judgment",
    "JudgmentSubmission",
    "LookSubmission",
    "Measure",
    "Mechanism",
    "ResidualSubmission",
    "SetAside",
    "SiteQuestion",
    "SliceAnswer",
    "TriggerAnswer",
]
