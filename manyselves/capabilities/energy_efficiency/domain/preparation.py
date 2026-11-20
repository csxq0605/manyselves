"""The preparation inputs of a run, following EveryMe's A01–A05.

- A01 业务任务定义 ``task.yaml``: what the business asks, who uses the
  result, which decisions it should support, the objects and period, how
  deep to look, the constraints, and when to stop.  Anything not written is
  listed as 待定; nothing blocks on it.
- A02 收件与来源登记: every frozen file gets a source id, its role, its hash
  and the sheets that were read (``source_register``).
- A03 解析与对象登记: ``manifest.yaml`` says how to read the files; the
  program turns them into the company model (``company_model``).
- A04 分析偏好与现场假设: ``preferences.yaml`` (or ``分析偏好.xlsx``;
  the older name ``parameters.yaml`` still reads) holds values a person set,
  each with its source.  An analysis preference not set takes its value from
  the default file ``preferences_default.yaml`` beside this module, which
  says where each value came from; preferences are choices, not facts, and
  never go to the site checklist.  A site assumption not confirmed stays
  ASSUMED and goes to the site checklist.  Nothing blocks the run.
- A05 关键归属及可比条件确认 ``confirmations.yaml``: what a person with the
  right confirmed or rejected: which machines a meter measures, what a
  utility serves, tariff bindings, comparable groups, calendar days,
  parameters, inferences.  The program and the model only propose; a link
  stays CANDIDATE until a confirmation says otherwise.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .inputs import (
    MANIFEST_NAME,
    InputFormatError,
    Manifest,
    TableSource,
    TariffFileSpec,
    TreeJsonSpec,
    file_sha256,
)

Day = date  # a field of Confirmation is named ``date``
TASK_NAME = "task.yaml"
PARAMETERS_NAME = "parameters.yaml"
PREFERENCES_NAME = "preferences.yaml"
PREFERENCES_BOOK = "分析偏好.xlsx"
CONFIRMATIONS_NAME = "confirmations.yaml"
PREPARATION_NAMES = (TASK_NAME, PREFERENCES_NAME, PREFERENCES_BOOK, PARAMETERS_NAME, CONFIRMATIONS_NAME)
DEFAULT_PREFERENCES = Path(__file__).with_name("preferences_default.yaml")

# How a value came about (EveryMe 能效公司模型扩充定义 §2, with USER_CONFIRMED from its r5 task).
BASES: dict[str, str] = {
    "MEASURED": "量测",
    "REGISTERED": "登记（人在资料或参数文件里写的）",
    "DERIVED": "程序派生",
    "SOURCE_REPORTED": "现场答复",
    "USER_CONFIRMED": "有权人员确认",
    "CONVENTION": "分析偏好的默认值或工作约定（默认文件里写了来历）",
    "ASSUMED": "登记假设",
}
STATUSES: dict[str, str] = {
    "CONFIRMED": "已确认",
    "PARTIAL": "部分确认",
    "CANDIDATE": "候选，待确认",
    "ASSUMED": "按假设",
    "CONFLICT": "有冲突",
    "REJECTED": "已被否定",
}

# Who to ask: a fixed set, chosen by the program from structured kinds.
ROLES: dict[str, str] = {
    "metering": "计量/电气负责人",
    "production": "生产计划/调度",
    "process": "工艺负责人",
    "equipment": "设备/公辅负责人",
    "energy_finance": "能源管理/电费经办",
    "data": "数据/系统管理员",
}
Role = Literal["metering", "production", "process", "equipment", "energy_finance", "data"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# A01 ---------------------------------------------------------------------------


class TaskPeriod(_Strict):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def ordered(self) -> "TaskPeriod":
        if self.start.tzinfo or self.end.tzinfo:
            raise ValueError("the task period is site local time without a time zone")
        if self.end <= self.start:
            raise ValueError("the task period ends before it starts")
        return self


class TaskDefinition(_Strict):
    """A01 业务任务定义. Every field may be left out; what is missing is listed as 待定."""

    request: str | None = Field(default=None, description="企业诉求：想解决什么问题")
    users: list[str] = Field(default_factory=list, description="结果给谁用")
    decisions: list[str] = Field(default_factory=list, description="要支持哪些决策")
    meters: list[str] = Field(default_factory=list, description="只看这些表（表编号）；不写时看全部")
    period: TaskPeriod | None = Field(default=None, description="只看这段时间（站点本地时间）")
    depth: str | None = Field(default=None, description="分析深度，例如“先筛一遍”“逐条到现场核实”")
    constraints: list[str] = Field(default_factory=list, description="约束和资源：不能停的设备、预算、现场能配合的时间等")
    stop_conditions: list[str] = Field(default_factory=list, description="审核与停止条件：什么情况下这一轮可以收")
    focus: str | None = Field(default=None, max_length=500, description="关注点，只作为给模型的提示")
    defined_by: str | None = Field(default=None, description="写这份任务说明的人")

    def pending(self) -> list[str]:
        """The parts of the task nobody wrote: listed as 待定."""

        labels = {
            "request": "企业诉求",
            "users": "结果使用者",
            "decisions": "要支持的决策",
            "depth": "分析深度",
            "constraints": "约束和资源",
            "stop_conditions": "审核与停止条件",
        }
        return [label for key, label in labels.items() if not getattr(self, key)]


# A04 ---------------------------------------------------------------------------
#
# Two kinds of values the program needs and nobody may have given:
#
# - 分析偏好 (``nature="preference"``): how the analysis is set up - where a
#   power band starts, which quantiles serve as references, how much shared
#   energy makes two factors one group.  They are choices, not facts about
#   the site, so they never go to the site checklist.  When a person did not
#   set one, its value comes from the default file next to this module
#   (``preferences_default.yaml``), which says where each value came from;
#   nothing is hidden in the code.
# - 现场假设 (``nature="site"``): what the program has to take about the site
#   when nobody told it (meters not nested, an unmapped meter serves every
#   machine).  They stay ASSUMED and go to the site checklist.


@dataclass(frozen=True)
class ParameterSpec:
    """One value the program uses, with what it is and what would replace it."""

    id: str
    key: str
    topic: str
    unit: str | None
    rule: str
    replaces_when: str
    affected: tuple[str, ...]
    nature: Literal["preference", "site"] = "preference"
    default: Any = None  # site assumptions only; preferences take theirs from the default file
    rationale: str = ""
    alternatives: tuple[Any, ...] = ()
    per_meter: bool = False
    ask: Role = "production"
    kind: Literal["number", "bool", "columns", "numbers", "rules"] = "number"
    bounds: tuple[float, float] | None = None


PARAMETERS: tuple[ParameterSpec, ...] = (
    ParameterSpec(
        id="P01",
        key="day_run_hours",
        topic="开工日与休息日的判定",
        unit="h",
        rule="当天有任一机台的登记批次，或任一挂机台的表功率档为 RUN 累计不少于本值，记为开工日（OPERATING），否则记为休息日（REST）",
        replaces_when="有人确认的日历（confirmations 里 kind: calendar）",
        affected=("DAY", "SL", "MX"),
        ask="production",
        bounds=(0.0, 24.0),
    ),
    ParameterSpec(
        id="P02",
        key="band_reference_quantile",
        topic="产线表功率档的参考功率",
        unit=None,
        rule="参考功率取本表机台登记批次时段里各区间功率的这个分位数（nearest-rank）",
        replaces_when="铭牌额定功率或工艺给的正常加工功率（按表设）",
        affected=("XS", "PLANT", "DAY", "STOP", "BAND", "MX", "SL", "PST"),
        per_meter=True,
        ask="process",
        bounds=(0.5, 1.0),
    ),
    ParameterSpec(
        id="P03",
        key="band_off_fraction",
        topic="功率档 OFF 的界线",
        unit=None,
        rule="区间功率低于参考功率 × 本值记为 OFF",
        replaces_when="现场给出的停机功率（按表设）",
        affected=("XS", "PLANT", "STOP", "BAND", "MX", "SL", "PST"),
        per_meter=True,
        ask="equipment",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P04",
        key="band_run_fraction",
        topic="功率档 RUN 的界线",
        unit=None,
        rule="区间功率不低于参考功率 × 本值记为 RUN；介于 OFF 和 RUN 之间为 LOW",
        replaces_when="现场给出的运行功率下限（按表设）",
        affected=("XS", "PLANT", "DAY", "BAND", "MX", "SL", "PST"),
        per_meter=True,
        ask="equipment",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P05",
        key="full_fraction",
        topic="满产和部分生产的划分",
        unit=None,
        rule="在加工的机台数少于在用机台数 × 本值记为 PARTIAL，否则记为 FULL",
        replaces_when="生产计划给出的满产定义",
        affected=("PLANT", "MX", "SL", "PST"),
        ask="production",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P06",
        key="meters_independent",
        topic="表之间有没有上下级",
        nature="site",
        default=True,
        unit=None,
        rule="各表互不嵌套，全厂功率按各表相加（只用于需量同时率和全厂状态电量占比）",
        rationale="没有计量上下级资料；工厂树和台账只说拓扑，不证明计量关系",
        replaces_when="计量上下级关系（confirmations 里 kind: meter_topology）",
        affected=("PK", "PKC", "MX"),
        ask="metering",
        kind="bool",
    ),
    ParameterSpec(
        id="P07",
        key="default_service_scope",
        topic="没挂机台的表服务哪些机台",
        nature="site",
        default=True,
        unit=None,
        rule="服务对象没人确认的表，按服务全部机台计算需求代理（在产机台数、被服务产线功率）",
        rationale="没有服务关系资料；需求代理只表示需求可能的大小，不是实测服务量",
        replaces_when="服务关系（confirmations 里 kind: service）",
        affected=("DF", "DFS"),
        ask="equipment",
        kind="bool",
    ),
    ParameterSpec(
        id="P08",
        key="peer_group_columns",
        topic="单耗可比组",
        unit=None,
        rule="单耗按这些生产记录列分组；默认（空）按生产记录的产品列；同组跨机台的可比条件没人核过",
        replaces_when="改分组列，或 confirmations 里 kind: peer_group 确认/否定某组",
        affected=("GRP", "GRPM"),
        ask="process",
        kind="columns",
    ),
    ParameterSpec(
        id="P09",
        key="peak_top_intervals",
        topic="需量同时率取多少个最高区间",
        unit="个",
        rule="按全厂功率最高的这些区间计算各表的平均占比",
        replaces_when="改这一项",
        affected=("PKC",),
        ask="energy_finance",
        bounds=(1.0, 10000.0),
    ),
    ParameterSpec(
        id="P10",
        key="demand_busy_min_hours",
        topic="需求跟随里“最忙一档”至少要有多少小时",
        unit="h",
        rule="按在产台数分档后，取至少有本值小时的档里台数最多的一档，作为“需求最多时”的功率",
        replaces_when="改这一项",
        affected=("DFS",),
        ask="equipment",
        bounds=(0.0, 10000.0),
    ),
    ParameterSpec(
        id="P11",
        key="band_reference_fallback",
        topic="加工段没有功率记录时，参考功率改用准备段",
        unit=None,
        rule="是：本表机台在加工段没有功率记录时，用准备段的功率算参考功率；否：没有参考功率，本表不分功率档",
        replaces_when="改这一项，或按表给额定功率",
        affected=("XS", "BAND", "STOP"),
        ask="process",
        kind="bool",
    ),
    ParameterSpec(
        id="P12",
        key="reference_quantiles",
        topic="因子展开时的参照分位",
        unit=None,
        rule="每个因子按本表同类各次出现的平均功率取这些分位数作参照，算高于参照部分的电量（统计参考，资格未核）",
        replaces_when="改这一项",
        affected=("FACTOR",),
        ask="energy_finance",
        kind="numbers",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P13",
        key="overlap_group_share",
        topic="两个因子算作同一组的共用电量比例",
        unit=None,
        rule="两个因子在同一块表的同一批记录上共用的电量，占其中较小一项电量不少于本值，归为同一重叠组；组内合计按记录去重",
        replaces_when="改这一项",
        affected=("FACTOR",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P14",
        key="caliber_min_batches",
        topic="口径推断里列出机台对表的影响至少要几批",
        unit="批",
        rule="一台机至少有这么多批落在表的数据期里，才列出它开机时表功率的升幅；报表电量和表计比对也要这么多批",
        replaces_when="改这一项",
        affected=("CALIBER",),
        ask="data",
        bounds=(1.0, 1000.0),
    ),
    ParameterSpec(
        id="P15",
        key="caliber_top_machines",
        topic="口径推断里每块表列出几台升幅最大的机",
        unit="台",
        rule="每块表按开机时功率升幅从大到小列出这么多台机（挂在本表的机台总是列出）",
        replaces_when="改这一项",
        affected=("CALIBER",),
        ask="data",
        bounds=(1.0, 1000.0),
    ),
    ParameterSpec(
        id="P16",
        key="caliber_pair_hours",
        topic="两表比较至少要多少个共同小时",
        unit="h",
        rule="两块表共同有数据的整点小时不少于本值，才比较它们的关系；最高表对其余表之和的比较也用这个值",
        replaces_when="改这一项",
        affected=("CALIBER",),
        ask="data",
        bounds=(1.0, 100000.0),
    ),
    ParameterSpec(
        id="P17",
        key="caliber_pair_correlation",
        topic="两表列为“一起变”的相关系数下限",
        unit=None,
        rule="两块表小时功率的相关系数不低于本值，才列出这对表",
        replaces_when="改这一项",
        affected=("CALIBER",),
        ask="data",
        bounds=(-1.0, 1.0),
    ),
    ParameterSpec(
        id="P18",
        key="caliber_pair_under",
        topic="两表列为“一块在另一块之下”的小时占比下限",
        unit=None,
        rule="小的那块表不超过大的那块的小时占比不低于本值，或两表平均功率之比不低于 0.9，才列出这对表",
        replaces_when="改这一项",
        affected=("CALIBER",),
        ask="data",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P19",
        key="caliber_max_pairs",
        topic="口径推断最多列出几对表",
        unit="对",
        rule="按相关系数从高到低列出这么多对",
        replaces_when="改这一项",
        affected=("CALIBER",),
        ask="data",
        bounds=(1.0, 100000.0),
    ),
    # The lenses, the triggers, the residual rounds and the screening (2026-11-19).
    ParameterSpec(
        id="P20",
        key="plant_stop_fraction",
        topic="按功率判全厂停",
        unit=None,
        rule="所有产线表都不在 RUN 档，而且产线表功率之和低于它的 P95 × 本值，这个时刻记为全厂停（按功率判，和登记的全厂状态并列）",
        replaces_when="人确认的日历（confirmations 里 kind: calendar）只改日状态，不改这个判定",
        affected=("STP", "PWS", "DAY", "LENS"),
        ask="production",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P21",
        key="driven_correlation",
        topic="没挂机台的表算作“随生产”的相关下限",
        unit=None,
        rule="本表日电量和产线日电量的秩相关不低于本值，算作随生产：适用“无需求用电”视角，驱动残差按产线电量拟合；否则按滚动中位看异常日",
        replaces_when="服务关系（confirmations 里 kind: service）",
        affected=("LENS",),
        ask="equipment",
        bounds=(-1.0, 1.0),
    ),
    ParameterSpec(
        id="P22",
        key="idle_floor_quantile",
        topic="停机底功率取哪个分位",
        unit=None,
        rule="产线表取本表不在 RUN 档时功率的这个分位；随生产的辅助表取全厂停时本表功率的这个分位；高于它的电量算“无需求用电”和“批内停机”的超出",
        replaces_when="现场给出的停机必要功率（confirmations 里 kind: service 的 minimum_kw）",
        affected=("LENS",),
        ask="equipment",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P23",
        key="batch_reference_quantiles",
        topic="批次先例的参照档",
        unit=None,
        rule="同线同组批次的运行单耗、准备段电量各取这些分位作参照（0 是组内最低）；同组至少 2 批",
        replaces_when="改这一项",
        affected=("LENS",),
        ask="process",
        kind="numbers",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P24",
        key="batch_default_quantile",
        topic="批次先例的默认参照",
        unit=None,
        rule="筛选和总览用这一档；它要在 P23 里",
        replaces_when="人在台账里给某项选了别的参照",
        affected=("LENS", "SCREEN"),
        ask="process",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P25",
        key="day_window_days",
        topic="日电量先例在前后多少天里找",
        unit="天",
        rule="辅助和非生产表的日电量先例只在前后本值天内找（时间相近，粗略代替季节和天气）",
        replaces_when="改这一项",
        affected=("LENS",),
        ask="energy_finance",
        bounds=(1.0, 366.0),
    ),
    ParameterSpec(
        id="P26",
        key="day_neighbours",
        topic="日电量先例取产线电量最相近的几天",
        unit="天",
        rule="在 P25 的范围里取产线日电量最接近的这么多天（含当天）",
        replaces_when="改这一项",
        affected=("LENS",),
        ask="energy_finance",
        bounds=(1.0, 366.0),
    ),
    ParameterSpec(
        id="P27",
        key="day_reference_quantiles",
        topic="日电量先例的参照档",
        unit=None,
        rule="在 P26 选出的几天里取本表日电量的这些分位作参照",
        replaces_when="改这一项",
        affected=("LENS",),
        ask="energy_finance",
        kind="numbers",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P28",
        key="day_default_quantile",
        topic="日电量先例的默认参照",
        unit=None,
        rule="筛选和总览用这一档；它要在 P27 里",
        replaces_when="人在台账里给某项选了别的参照",
        affected=("LENS", "SCREEN"),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P29",
        key="residual_sigma",
        topic="驱动残差算作异常日的倍数",
        unit=None,
        rule="日电量减去拟合值（随生产的表按产线电量线性拟合，其余按 15 天滚动中位）超过稳健标准差（1.4826 × MAD）的本值倍，算异常日",
        replaces_when="改这一项",
        affected=("LENS",),
        ask="energy_finance",
        bounds=(0.0, 100.0),
    ),
    ParameterSpec(
        id="P30",
        key="residual_min_share",
        topic="异常日的残差至少占日中位电量多少",
        unit=None,
        rule="残差同时要不少于本表日电量中位数 × 本值，才算异常日",
        replaces_when="改这一项",
        affected=("LENS",),
        ask="energy_finance",
        bounds=(0.0, 10.0),
    ),
    ParameterSpec(
        id="P31",
        key="elasticity_trigger",
        topic="“停产时降不降”的触发线",
        unit=None,
        rule="本表在全厂停时的平均功率 ÷ 在产时的平均功率不低于本值，且停时电量占本表电量不少于 P32，触发模型去看（判必要服务还是可能浪费）",
        replaces_when="改这一项",
        affected=("TRIGGER",),
        ask="energy_finance",
        bounds=(0.0, 10.0),
    ),
    ParameterSpec(
        id="P32",
        key="trigger_min_share",
        topic="触发至少占本表电量多少",
        unit=None,
        rule="触发涉及的电量（停时电量或剩余超出）不少于本表电量 × 本值才触发",
        replaces_when="改这一项",
        affected=("TRIGGER",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P33",
        key="residual_rounds",
        topic="补找剩余最多几轮",
        unit="轮",
        rule="模型看剩余超出、给解释或新参照，最多这么多轮（工作流最多排三轮）",
        replaces_when="改这一项",
        affected=("RESIDUAL",),
        ask="energy_finance",
        bounds=(0.0, 3.0),
    ),
    ParameterSpec(
        id="P34",
        key="residual_stop_share",
        topic="补找什么时候停",
        unit=None,
        rule="一轮新解释或新找到的电量少于这一轮开始时剩余超出 × 本值，就不再补找",
        replaces_when="改这一项",
        affected=("RESIDUAL",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P35",
        key="residual_top_slices",
        topic="每轮补找看多少片剩余",
        unit="片",
        rule="按电量从大到小，每轮给模型看这么多片（每片是一块表的一周）",
        replaces_when="改这一项",
        affected=("RESIDUAL",),
        ask="energy_finance",
        bounds=(1.0, 200.0),
    ),
    ParameterSpec(
        id="P36",
        key="screen_min_kwh",
        topic="有效项的电量下限",
        unit="kWh",
        rule="默认参照下的超出低于本值的项（每块表一行）筛为无效；程序差额达到本值和 P37 的，模型提项时要逐条交代（采用或写为什么不立项）",
        replaces_when="改这一项",
        affected=("SCREEN",),
        ask="energy_finance",
        bounds=(0.0, 1e9),
    ),
    ParameterSpec(
        id="P37",
        key="screen_min_share",
        topic="有效项至少占本表电量多少",
        unit=None,
        rule="默认参照下的超出低于本表全期电量 × 本值的项筛为无效；也是程序差额要模型逐条交代的下限之一",
        replaces_when="改这一项",
        affected=("SCREEN",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P38",
        key="screen_min_coverage",
        topic="表的数据覆盖率下限",
        unit=None,
        rule="表的可用记录覆盖数据期低于本值时，它上面的项筛为待定（数据不全，数可能偏）",
        replaces_when="改这一项",
        affected=("SCREEN",),
        ask="data",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P39",
        key="reference_rules",
        topic="人确认的参照规则",
        unit=None,
        rule="每条是一个参照规格（表、时段、参照）。程序每次运行都按它算，每条是一个人定的损耗项；模型提议的规格写进来以后就成了确定性规则",
        replaces_when="把台账“规格”页里确认的规格抄进来",
        affected=("LENS", "SCREEN"),
        ask="energy_finance",
        kind="rules",
    ),
    ParameterSpec(
        id="P40",
        key="residual_rules",
        topic="人确认的剩余规则",
        unit=None,
        rule="每条是一个剩余解释规格（表、时段、类型：必要服务、外部驱动、数据问题）。落在里面的超出记为已解释，项的行扣掉它以后低于电量下限的筛为无效",
        replaces_when="把台账“规格”页里确认的解释抄进来",
        affected=("RESIDUAL", "SCREEN"),
        ask="energy_finance",
        kind="rules",
    ),
    ParameterSpec(
        id="P41",
        key="discovery_min_share",
        topic="算“独立发现”的一片要多大",
        unit=None,
        rule="一块表一天里某视角的超出不少于这块表当天电量 × 本值，才算这个视角在这一片有发现；只有它一个视角有发现，算独立发现",
        replaces_when="改这一项",
        affected=("SATURATION",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P42",
        key="probe_change_share",
        topic="“某日前后变了”的检验线",
        unit=None,
        rule="指定日期前后，本项逐日超出的均值相差不少于两者中较大者 × 本值，算支持",
        replaces_when="改这一项",
        affected=("PROBE",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P43",
        key="probe_share",
        topic="“集中在……”类检验的占比线",
        unit=None,
        rule="本项超出落在指定时段、指定组或最大的两成事件里的比例不少于本值，算支持",
        replaces_when="改这一项",
        affected=("PROBE",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P44",
        key="stability_half_share",
        topic="前后两半都要有超出",
        unit=None,
        rule="把数据期按天分成前后两半，一项的超出在较小的一半不到较大一半 × 本值时，筛为待定（可能是一次性的事，或从某天起变了）",
        replaces_when="改这一项",
        affected=("SCREEN",),
        ask="energy_finance",
        bounds=(0.0, 1.0),
    ),
    ParameterSpec(
        id="P45",
        key="shift_change_share",
        topic="规格时段挪一格的变化线",
        unit=None,
        rule="模型交的参照规格，把时段提前或推后一个记录间隔，超出变化超过原值 × 本值时，筛为待定（时段写得太贴）",
        replaces_when="改这一项",
        affected=("SCREEN",),
        ask="energy_finance",
        bounds=(0.0, 10.0),
    ),
)
PREFERENCES = tuple(spec for spec in PARAMETERS if spec.nature == "preference")
PARAMETER_BY_KEY = {spec.key: spec for spec in PARAMETERS}
PARAMETER_BY_ID = {spec.id: spec for spec in PARAMETERS}


class ParameterValue(_Strict):
    value: Any
    source: str = Field(min_length=1, description="来源或审核依据，例如“设备科 2026-11 给的铭牌功率”")
    reviewed_by: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    meters: list[str] = Field(default_factory=list, description="只对这些表（只有按表的参数可以写）")


class Parameters(_Strict):
    """A04 参数与事件规则: values a person set, each with its source."""

    version: str | None = None
    values: dict[str, ParameterValue | list[ParameterValue]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known(self) -> "Parameters":
        for key, entries in self.values.items():
            spec = PARAMETER_BY_KEY.get(key)
            if spec is None:
                raise ValueError(f"unknown parameter {key!r}; the parameters are {sorted(PARAMETER_BY_KEY)}")
            for entry in entries if isinstance(entries, list) else [entries]:
                if entry.meters and not spec.per_meter:
                    raise ValueError(f"parameter {key} applies to the whole site; it takes no meters")
                _check_value(spec, entry.value)
        return self

    def entries(self, key: str) -> list[ParameterValue]:
        value = self.values.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


def _check_value(spec: ParameterSpec, value: Any) -> None:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"parameter {spec.key} is true or false")
        return
    if spec.kind == "columns":
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"parameter {spec.key} is a list of production record columns")
        return
    if spec.kind == "rules":
        # P39 holds reference specs and P40 residual specs, as the register's
        # 规格 sheet writes them (without a kind); the lens step reads each one.
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError(f"parameter {spec.key} is a list of specs (one mapping each)")
        return
    if spec.kind == "numbers":
        if not isinstance(value, list) or not value:
            raise ValueError(f"parameter {spec.key} is a list of numbers")
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"parameter {spec.key} is a list of numbers")
            if spec.bounds and not spec.bounds[0] <= float(item) <= spec.bounds[1]:
                raise ValueError(f"parameter {spec.key}: every number must be within {spec.bounds}")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"parameter {spec.key} is a number")
    if spec.bounds and not spec.bounds[0] <= float(value) <= spec.bounds[1]:
        raise ValueError(f"parameter {spec.key} must be within {spec.bounds}")


# A05 ---------------------------------------------------------------------------

ConfirmationKind = Literal[
    "meter_asset",
    "service",
    "tariff_binding",
    "peer_group",
    "calendar",
    "parameter",
    "assumption",
    "commissioning",
    "meter_topology",
]
CONFIRMATION_KINDS: dict[str, str] = {
    "meter_asset": "表计量哪些机台或设备",
    "service": "公辅服务哪些机台",
    "tariff_binding": "表的电价绑定",
    "peer_group": "可比组",
    "calendar": "某天是开工日还是休息日",
    "parameter": "参数取值",
    "assumption": "口径推断或现场假设成不成立",
    "commissioning": "机台或表是否投用",
    "meter_topology": "表的上下级",
}


class Confirmation(_Strict):
    """One thing a person with the right confirmed, rejected or corrected (A05)."""

    kind: ConfirmationKind
    subject: str = Field(min_length=1, description="确认的对象：表编号、机台、组名、日期、参数名或推断编号")
    result: Literal["confirmed", "rejected", "corrected"] = "confirmed"
    confirmed_by: str = Field(min_length=1, description="有权确认的人")
    date: Day | None = None
    basis: str = Field(min_length=1, description="依据：看了什么、问了谁")
    note: str | None = None
    assets: list[str] = Field(default_factory=list, description="meter_asset：这块表计的机台或设备")
    boundary: str | None = Field(default=None, description="meter_asset：计量边界：这块表含哪些设备或支路")
    serves: list[str] = Field(default_factory=list, description="service：服务的机台；全部机台写 ALL")
    minimum_kw: float | None = Field(default=None, ge=0, description="service：停产时的必要最低服务功率（kW）")
    plan: str | None = Field(default=None, description="tariff_binding：电价方案编号")
    machines: list[str] = Field(default_factory=list, description="peer_group：组内可比的机台")
    day_status: Literal["OPERATING", "REST"] | None = Field(default=None, description="calendar：开工日或休息日")
    value: Any = Field(default=None, description="parameter：参数值")
    commissioned: bool | None = Field(default=None, description="commissioning：是否投用")
    children: list[str] = Field(default_factory=list, description="meter_topology：下级表")

    @model_validator(mode="after")
    def fields_of_kind(self) -> "Confirmation":
        need = {
            "service": ("serves", bool(self.serves) or self.minimum_kw is not None),
            "tariff_binding": ("plan", bool(self.plan)),
            "calendar": ("day_status", self.day_status is not None),
            "parameter": ("value", self.value is not None),
            "commissioning": ("commissioned", self.commissioned is not None),
            "meter_topology": ("children", bool(self.children)),
        }
        if self.kind in need and self.result != "rejected":
            name, present = need[self.kind]
            if not present:
                raise ValueError(f"a {self.kind} confirmation needs {name}")
        if self.kind == "parameter":
            spec = PARAMETER_BY_KEY.get(self.subject)
            if spec is None:
                raise ValueError(f"unknown parameter {self.subject!r}")
            if self.result != "rejected":
                _check_value(spec, self.value)
        if self.kind == "calendar":
            _day_range(self.subject)
        return self


def _day_range(subject: str) -> tuple[date, date]:
    """``2026-07-05`` or ``2026-07-05/2026-07-07`` (both days included)."""

    first, _, last = subject.partition("/")
    try:
        start = date.fromisoformat(first.strip())
        end = date.fromisoformat(last.strip()) if last else start
    except ValueError as exc:
        raise ValueError(f"a calendar confirmation's subject is a date or date/date, not {subject!r}") from exc
    if end < start:
        raise ValueError(f"calendar range {subject!r} ends before it starts")
    return start, end


def day_range(subject: str) -> tuple[date, date]:
    return _day_range(subject)


class Confirmations(_Strict):
    confirmations: list[Confirmation] = Field(default_factory=list)


# Loading -----------------------------------------------------------------------


def _load(folder: Path, name: str, model: type[BaseModel]) -> Any:
    path = Path(folder) / name
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise InputFormatError(f"{name} cannot be read: {exc}") from exc
    try:
        return model.model_validate(data or {})
    except (ValidationError, ValueError) as exc:
        raise InputFormatError(f"{name} does not follow the format: {exc}") from exc


def load_task(folder: Path) -> TaskDefinition | None:
    return _load(folder, TASK_NAME, TaskDefinition)


def load_parameters(folder: Path) -> Parameters | None:
    """A04 as a person gave it: ``preferences.yaml``, ``分析偏好.xlsx`` or the older ``parameters.yaml`` (only one)."""

    present = [name for name in (PREFERENCES_NAME, PREFERENCES_BOOK, PARAMETERS_NAME) if (Path(folder) / name).is_file()]
    if len(present) > 1:
        raise InputFormatError(f"只能放一份分析偏好，现在有 {present}")
    if not present:
        return None
    if present[0] == PREFERENCES_BOOK:
        return read_preferences_book(Path(folder) / PREFERENCES_BOOK)
    return _load(folder, present[0], Parameters)


def load_default_preferences(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The default file: every analysis preference with its value and where the value came from."""

    path = path or DEFAULT_PREFERENCES
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        raise InputFormatError(f"默认分析偏好 {path.name} 读不了：{exc}") from exc
    values = data.get("values") or {}
    defaults: dict[str, dict[str, Any]] = {}
    for spec in PREFERENCES:
        entry = values.get(spec.key)
        if not isinstance(entry, dict) or "value" not in entry or not entry.get("origin"):
            raise InputFormatError(f"默认分析偏好 {path.name} 缺 {spec.key}（要有 value 和 origin）")
        try:
            _check_value(spec, entry["value"])
        except ValueError as exc:
            raise InputFormatError(f"默认分析偏好 {path.name}：{exc}") from exc
        alternatives = entry.get("alternatives") or []
        if not isinstance(alternatives, list):
            raise InputFormatError(f"默认分析偏好 {path.name}：{spec.key} 的 alternatives 是一个列表")
        for alternative in alternatives:
            try:
                _check_value(spec, alternative)
            except ValueError as exc:
                raise InputFormatError(f"默认分析偏好 {path.name}：{spec.key} 的备选值 {exc}") from exc
        defaults[spec.key] = {"value": entry["value"], "origin": str(entry["origin"]), "alternatives": list(alternatives)}
    unknown = sorted(set(values) - set(defaults))
    if unknown:
        raise InputFormatError(f"默认分析偏好 {path.name} 里有不认识的项 {unknown}")
    return defaults


# 分析偏好.xlsx ------------------------------------------------------------------

BOOK_SHEET = "分析偏好"
BOOK_COLUMNS = ("编号", "键", "名称", "当前取值", "单位", "允许范围", "按表", "规则", "影响", "当前取值来历", "取值（改这里）", "只对这些表", "来源（改了就写）")


def _book_value(spec: ParameterSpec, text: Any) -> Any:
    if isinstance(text, str):
        text = text.strip()
    if spec.kind == "bool":
        if isinstance(text, bool):
            return text
        lowered = str(text).casefold()
        if lowered in {"是", "true", "yes", "1"}:
            return True
        if lowered in {"否", "false", "no", "0"}:
            return False
        raise ValueError(f"{spec.id} {spec.key} 填 是 或 否")
    if spec.kind == "rules":
        try:
            loaded = yaml.safe_load(str(text)) if not isinstance(text, list) else text
        except yaml.YAMLError as exc:
            raise ValueError(f"{spec.id} {spec.key} 填 JSON 或 YAML 列表：{exc}") from exc
        return loaded if isinstance(loaded, list) else [loaded]
    if spec.kind in {"columns", "numbers"}:
        parts = [part.strip() for part in re.split(r"[,，、;；]", str(text)) if part.strip()]
        return parts if spec.kind == "columns" else [float(part) for part in parts]
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return text
    return float(str(text))


def read_preferences_book(path: Path) -> Parameters:
    """The workbook a person filled: a row with a value in 取值（改这里） sets that preference."""

    try:
        frame = pd.read_excel(path, sheet_name=BOOK_SHEET, dtype=object)
    except (OSError, ValueError, KeyError) as exc:
        raise InputFormatError(f"{path.name} 读不了（要有工作表“{BOOK_SHEET}”）：{exc}") from exc
    missing = [column for column in ("键", "取值（改这里）") if column not in frame.columns]
    if missing:
        raise InputFormatError(f"{path.name} 缺列 {missing}")
    values: dict[str, list[dict[str, Any]]] = {}
    for number, row in frame.iterrows():
        raw = row.get("取值（改这里）")
        if raw is None or (isinstance(raw, float) and np.isnan(raw)) or (isinstance(raw, str) and not raw.strip()):
            continue
        key = str(row.get("键") or "").strip()
        spec = PARAMETER_BY_KEY.get(key)
        if spec is None:
            raise InputFormatError(f"{path.name} 第 {number + 2} 行：不认识的键 {key!r}")
        try:
            value = _book_value(spec, raw)
        except ValueError as exc:
            raise InputFormatError(f"{path.name} 第 {number + 2} 行：{exc}") from exc
        meters_text = row.get("只对这些表")
        meters = (
            [part.strip() for part in re.split(r"[,，、;；]", str(meters_text)) if part.strip()]
            if isinstance(meters_text, str)
            else []
        )
        source = row.get("来源（改了就写）")
        source = str(source).strip() if isinstance(source, str) and source.strip() else f"{BOOK_SHEET}表第 {number + 2} 行"
        values.setdefault(key, []).append({"value": value, "source": source, "meters": meters})
    try:
        return Parameters.model_validate({"values": values})
    except (ValidationError, ValueError) as exc:
        raise InputFormatError(f"{path.name} 不合格式：{exc}") from exc


def _shown(value: Any) -> Any:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        import json

        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return value


def preference_rows(params: "ParameterSet") -> list[dict[str, Any]]:
    """One row per preference value in force (the site-wide one, then any per meter)."""

    rows = []
    for spec in PREFERENCES:
        for entry in params.by_key[spec.key]:
            low, high = spec.bounds if spec.bounds else (None, None)
            rows.append(
                {
                    "编号": spec.id,
                    "键": spec.key,
                    "名称": spec.topic,
                    "当前取值": _shown(entry.value),
                    "单位": spec.unit or "",
                    "允许范围": "" if low is None else f"{low:g} 至 {high:g}",
                    "按表": "可以" if spec.per_meter else "不可以",
                    "规则": spec.rule,
                    "影响": "、".join(spec.affected),
                    "当前取值来历": entry.source,
                    "取值（改这里）": None,
                    "只对这些表": "、".join(entry.meters),
                    "来源（改了就写）": None,
                }
            )
    return rows


def write_preferences_book(params: "ParameterSet", path: Path) -> Path:
    """The workbook to change the preferences: the current values, and an empty column to fill."""

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    book = Workbook()
    sheet = book.active
    sheet.title = BOOK_SHEET
    sheet.append(list(BOOK_COLUMNS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    fill = PatternFill("solid", fgColor="FFF4CC")
    for row in preference_rows(params):
        sheet.append([row[column] for column in BOOK_COLUMNS])
        for column in ("取值（改这里）", "只对这些表", "来源（改了就写）"):
            sheet.cell(row=sheet.max_row, column=BOOK_COLUMNS.index(column) + 1).fill = fill
    widths = {"名称": 26, "规则": 60, "当前取值来历": 40, "影响": 18}
    for index, column in enumerate(BOOK_COLUMNS, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = widths.get(column, 14)
        for cell in sheet.iter_cols(min_col=index, max_col=index, min_row=2).__next__():
            cell.alignment = Alignment(wrap_text=column in widths, vertical="top")
    notes = book.create_sheet("怎么改")
    for line in (
        "在“取值（改这里）”填新值，“来源（改了就写）”写谁定的、依据是什么；没填的行保持当前取值。",
        "按表设置：在“只对这些表”写表编号（逗号分隔），一行只管这些表；可以一项写几行。",
        "是/否 的项填“是”或“否”；多个数或多个列用逗号分隔。",
        "改好后把这个文件放进输入文件夹（文件名保持“分析偏好.xlsx”），在同一运行编号上续跑。",
        "分析偏好是怎样分析的选择，不是工厂的事实，不进现场核实清单。",
    ):
        notes.append([line])
    notes.column_dimensions["A"].width = 100
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def load_confirmations(folder: Path) -> Confirmations | None:
    return _load(folder, CONFIRMATIONS_NAME, Confirmations)


def merged_scope(request_scope: dict[str, Any], task: TaskDefinition | None) -> dict[str, Any]:
    """The run's scope: what the request names, else what the task definition names."""

    scope = dict(request_scope)
    if task is None:
        return scope
    if not scope.get("meters") and task.meters:
        scope["meters"] = list(task.meters)
    if not scope.get("start") and not scope.get("end") and task.period is not None:
        scope["start"] = task.period.start.isoformat(sep=" ")
        scope["end"] = task.period.end.isoformat(sep=" ")
    if not scope.get("focus") and task.focus:
        scope["focus"] = task.focus
    return scope


# Resolving parameters ------------------------------------------------------------------------


@dataclass
class ResolvedParameter:
    spec: ParameterSpec
    value: Any
    basis: str
    status: str
    source: str
    reviewed_by: str | None = None
    meters: list[str] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    notes: list[str] = field(default_factory=list)
    alternatives: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "id": spec.id,
            "key": spec.key,
            "topic": spec.topic,
            "value": self.value,
            "unit": spec.unit,
            "basis": self.basis,
            "status": self.status,
            "source": self.source,
            "reviewed_by": self.reviewed_by,
            "meters": self.meters,
            "valid_from": self.valid_from.isoformat(sep=" ") if self.valid_from else None,
            "valid_until": self.valid_until.isoformat(sep=" ") if self.valid_until else None,
            "rule": spec.rule,
            "nature": spec.nature,
            "rationale": spec.rationale,
            "replaces_when": spec.replaces_when,
            "alternatives": list(self.alternatives),
            "affected": list(spec.affected),
            "ask": spec.ask,
            "notes": self.notes,
        }


class ParameterSet:
    """Every parameter's value for one run: set by a person, confirmed, or the registered default."""

    def __init__(
        self,
        parameters: Parameters | None,
        confirmations: Confirmations | None,
        period: tuple[datetime, datetime] | None,
        product_column: str | None,
    ) -> None:
        self.by_key: dict[str, list[ResolvedParameter]] = {}
        entries = confirmations.confirmations if confirmations else []
        confirmed = {entry.subject: entry for entry in entries if entry.kind == "parameter"}
        # A confirmation of a parameter assumption by its id keeps the default value.
        by_id = {entry.subject: entry for entry in entries if entry.kind == "assumption"}
        defaults = load_default_preferences()
        self.defaults = defaults
        for spec in PARAMETERS:
            if spec.nature == "preference":
                default = defaults[spec.key]["value"]
                if spec.key == "peer_group_columns" and not default:
                    default = [product_column] if product_column else []
                first = ResolvedParameter(spec, default, "CONVENTION", "CONFIRMED", f"默认分析偏好：{defaults[spec.key]['origin']}")
                first.alternatives = list(defaults[spec.key]["alternatives"])
            else:
                first = ResolvedParameter(spec, spec.default, "ASSUMED", "ASSUMED", "程序登记的现场假设")
            resolved: list[ResolvedParameter] = [first]
            for entry in parameters.entries(spec.key) if parameters else []:
                item = ResolvedParameter(
                    spec,
                    entry.value,
                    "REGISTERED",
                    "CONFIRMED",
                    entry.source,
                    entry.reviewed_by,
                    list(entry.meters),
                    entry.valid_from,
                    entry.valid_until,
                )
                if period and (
                    (entry.valid_from and entry.valid_from > period[0])
                    or (entry.valid_until and entry.valid_until < period[1])
                ):
                    item.status = "PARTIAL"
                    item.notes.append("有效期没有覆盖整个数据期；程序仍按它算整个数据期")
                if entry.meters:
                    resolved.append(item)
                else:
                    resolved[0] = item
            confirmation = confirmed.get(spec.key)
            judged = by_id.get(spec.id)
            if confirmation is None and judged is not None and resolved[0].basis in {"ASSUMED", "CONVENTION"}:
                if judged.result == "confirmed":
                    resolved[0] = ResolvedParameter(
                        spec, resolved[0].value, "USER_CONFIRMED", "CONFIRMED", judged.basis, judged.confirmed_by
                    )
                else:
                    resolved[0].status = "REJECTED"
                    resolved[0].notes.append(f"{judged.confirmed_by} 否定了这条假设：{judged.basis}；程序仍按它算，结果要重看")
            if confirmation is not None:
                if confirmation.result == "rejected":
                    resolved[0].status = "REJECTED"
                    resolved[0].notes.append(f"{confirmation.confirmed_by} 否定了这个取值：{confirmation.basis}")
                else:
                    resolved[0] = ResolvedParameter(
                        spec, confirmation.value, "USER_CONFIRMED", "CONFIRMED",
                        confirmation.basis, confirmation.confirmed_by,
                    )
            self.by_key[spec.key] = resolved

    def value(self, key: str, meter_id: str | None = None) -> Any:
        return self.entry(key, meter_id).value

    def alternatives(self, key: str) -> list[Any]:
        """Values to compare with, for a preference still at its default; none once a person set it."""

        spec = PARAMETER_BY_KEY[key]
        if spec.nature != "preference" or self.by_key[key][0].basis != "CONVENTION":
            return list(spec.alternatives)
        return list(self.defaults[key]["alternatives"])

    def preference_ids(self, tables: tuple[str, ...] | list[str]) -> list[str]:
        """The ids of the analysis preferences a value of these tables rests on."""

        wanted = set(tables)
        return [spec.id for spec in PREFERENCES if wanted & set(spec.affected)]

    def entry(self, key: str, meter_id: str | None = None) -> ResolvedParameter:
        entries = self.by_key[key]
        if meter_id is not None:
            for item in entries[1:]:
                if meter_id in item.meters:
                    return item
        return entries[0]

    def assumed_ids(self, keys: tuple[str, ...], meter_id: str | None = None) -> list[str]:
        """The ids of the parameters among ``keys`` that are still a registered assumption."""

        return [
            PARAMETER_BY_KEY[key].id
            for key in keys
            if self.entry(key, meter_id).basis == "ASSUMED"
        ]

    def as_list(self) -> list[dict[str, Any]]:
        return [item.as_dict() for entries in self.by_key.values() for item in entries]


# A02 ---------------------------------------------------------------------------


def _sheets(path: Path) -> list[str] | None:
    if path.suffix.casefold() not in {".xlsx", ".xlsm"}:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            text = archive.read("xl/workbook.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    return [
        name.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
        for name in re.findall(r"<sheet [^>]*name=\"([^\"]*)\"", text)
    ]


def source_register(folder: Path, manifest: Manifest) -> list[dict[str, Any]]:
    """A02: every frozen file with a source id, its roles, its hash and what of it was read."""

    roles: dict[str, list[str]] = {}
    read_sheets: dict[str, set[str | None]] = {}

    def add(name: str, role: str, sheet: str | None = None) -> None:
        roles.setdefault(name, [])
        if role not in roles[name]:
            roles[name].append(role)
        read_sheets.setdefault(name, set()).add(sheet)

    add(MANIFEST_NAME, "读法（A03）")
    for meter in manifest.meters:
        add(meter.file, f"表计读数：{meter.id}", meter.sheet)
    labelled = (
        (manifest.production, "生产记录"),
        (manifest.calendar, "日历"),
        (manifest.tariff, "电价"),
        (manifest.site_facts, "现场调研答复"),
        (manifest.assets, "台账"),
        (manifest.plant_tree, "工厂树"),
    )
    for source, label in labelled:
        if isinstance(source, TableSource):
            add(source.file, label, source.sheet)
        elif isinstance(source, TariffFileSpec):
            add(source.file, label, source.price_sheet)
            add(source.file, label, source.period_sheet)
        elif isinstance(source, TreeJsonSpec):
            add(source.file, label)
    for name, label in (
        (TASK_NAME, "任务定义（A01）"),
        (PREFERENCES_NAME, "分析偏好（A04）"),
        (PREFERENCES_BOOK, "分析偏好（A04）"),
        (PARAMETERS_NAME, "分析偏好（A04，旧文件名）"),
        (CONFIRMATIONS_NAME, "确认记录（A05）"),
    ):
        if (Path(folder) / name).is_file():
            add(name, label)
    register = []
    for number, name in enumerate(sorted(roles, key=lambda item: (item != MANIFEST_NAME, item)), start=1):
        path = Path(folder) / name
        sheets = _sheets(path)
        used = read_sheets.get(name, set())
        entry: dict[str, Any] = {
            "source_id": f"S{number:02d}",
            "file": name,
            "roles": roles[name],
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        }
        if sheets is not None:
            # A sheet the manifest did not name is the first one, which pandas reads.
            named = {sheet if sheet is not None else sheets[0] for sheet in used} if sheets else set()
            entry["sheets_read"] = [sheet for sheet in sheets if sheet in named]
            entry["sheets_not_read"] = [sheet for sheet in sheets if sheet not in named]
        register.append(entry)
    return register


def source_ids(register: list[dict[str, Any]]) -> dict[str, str]:
    return {entry["file"]: entry["source_id"] for entry in register}


__all__ = [
    "BASES",
    "CONFIRMATIONS_NAME",
    "CONFIRMATION_KINDS",
    "Confirmation",
    "Confirmations",
    "DEFAULT_PREFERENCES",
    "PARAMETERS",
    "PARAMETERS_NAME",
    "PREFERENCES",
    "PREFERENCES_BOOK",
    "PREFERENCES_NAME",
    "PARAMETER_BY_ID",
    "PARAMETER_BY_KEY",
    "PREPARATION_NAMES",
    "ParameterSet",
    "ParameterSpec",
    "Parameters",
    "ROLES",
    "STATUSES",
    "TASK_NAME",
    "TaskDefinition",
    "day_range",
    "load_confirmations",
    "load_default_preferences",
    "load_parameters",
    "preference_rows",
    "read_preferences_book",
    "write_preferences_book",
    "load_task",
    "merged_scope",
    "source_ids",
    "source_register",
]
