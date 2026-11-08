"""The human verification checklist of the report (steps 5-6 of the knowledge base).

Root causes and conclusions are a person's: the program only lays out, for
each kept item, what a person has to rule in or out.  It attaches the
knowledge base's competing explanations by the item's method and proposed
categories (a fixed table, 5.5), turns the model's candidate causes into
minimal site questions (5.6.1), and leaves the evidence grade (5.4) and the
conclusion state (6.3) blank for the person.  Nothing here is filled in by
the model or decided by the program.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from .items import CAUSE_KINDS


@dataclass(frozen=True)
class Competing:
    trigger: str
    keep: str
    discriminate: str
    methods: frozenset[str]
    categories: frozenset[str]


# Knowledge base 5.5, row by row; methods and categories say when a row applies.
COMPETING: tuple[Competing, ...] = (
    Competing(
        "停产窗口公辅持续运行，候选联动缺失",
        "实验、环保、余热冷却、保护、共享对象、正常蓄冷",
        "全服务对象与最低需求清单；对照主机状态、命令反馈和支路测量，必要时做获准的局部试验",
        frozenset({"calendar-state", "daily-clock-window"}),
        frozenset({"LOSS-SUPPORT", "LOSS-IDLE"}),
    ),
    Competing(
        "报表间隙存在生产级功率，候选漏批或时钟偏移",
        "调试、烘机、报表时间定义不同、共表或错挂",
        "原报工审计、设备事件时标、工单和独立表；确认后重新切窗，不直接删除电量",
        frozenset({"production-gaps"}),
        frozenset({"LOSS-IDLE", "MEASUREMENT_ISSUE"}),
    ),
    Competing(
        "低功率长段，候选等料或故障响应迟缓",
        "计划休息、换网、必要保温、正常减速、缺测填零",
        "原因码、日历、报警和生产日志与曲线逐事件配对；区分故障、交接与计划停机",
        frozenset({"threshold-segments", "baseload", "production-gaps"}),
        frozenset({"LOSS-INTERRUPTION", "LOSS-IDLE", "LOSS-HOLD"}),
    ),
    Competing(
        "同条件设备持续高耗，候选散热或部件退化",
        "温控、装载、物料、仪表偏差、隐藏辅机、能力不同",
        "同温同装载复测；仪表核校、热像、温区电流及状态检查；维护前后用同条件留出样本复核",
        frozenset({"batch-intensity"}),
        frozenset({"LOSS-PROCESS", "LOSS-CONDITIONAL"}),
    ),
    Competing(
        "偏差集中某班或夜间，候选控制执行不同",
        "产品任务、保温、环境、交接需求或设备状态",
        "批准值、实际值、命令反馈及任务记录；可逆条件切换试验，不以班次标签直接归责",
        frozenset({"daily-clock-window"}),
        frozenset({"MECH-CONTROL"}),
    ),
    Competing(
        "加工单耗高，候选功率、投料速率或良品率变化",
        "成品与投入混用、有效时长不同、反算投入错误、漏报产品",
        "同批同窗口重构三因子，核称重、喂料、废料和质量；重构不闭合先查口径",
        frozenset({"batch-intensity"}),
        frozenset({"LOSS-PROCESS", "OPP-CAPABILITY"}),
    ),
    Competing(
        "清机长或出现二次清机，候选顺序或执行差异",
        "切换对难度、冷态、质量终点、中试目的、间隔本就需重启",
        "前后产品、颜色、料量、温度转速及首件记录；重建完整顺序，不能从“两次”直接判冗余",
        frozenset({"power-events"}),
        frozenset({"LOSS-TRANSITION"}),
    ),
    Competing(
        "主辅比变高或变低，候选辅机控制不匹配",
        "主机分母变化、欠供、漏表、共享服务和环境变化",
        "同看两侧绝对量、服务量与品质；核拓扑和实际供给，低比值不能单独证明高效",
        frozenset(),
        frozenset({"LOSS-SUPPORT"}),
    ),
    Competing(
        "真空或空压近恒功率，候选台数或支路未跟随",
        "最低压力、再生、备用、泄漏或其他服务对象",
        "需求、压力、阀位、运行台数和加载卸载记录；分段隔离或性能测试区分，不只看电曲线",
        frozenset({"baseload", "threshold-segments"}),
        frozenset({"LOSS-SUPPORT"}),
    ),
    Competing(
        "夜间冷负荷高，候选定时策略未更新",
        "正常蓄冷、次日供冷、库存跨期、天气或传感器偏置",
        "同服务、同初末库存边界核能量；查温度、阀位、时段及次日需求",
        frozenset(),
        frozenset({"LOSS-HOLD", "MECH-CONTROL"}),
    ),
    Competing(
        "多设备同步尖峰或单时点挂全天电量",
        "真实共同启动、累计量化、倍率、补录和归集程序",
        "独立表底、功率、采集与设备日志交叉；先隔离疑点，不按异常形态自动插值或认定故障",
        frozenset({"power-events"}),
        frozenset({"MEASUREMENT_ISSUE"}),
    ),
    Competing(
        "高价占比或账单均价升高，候选排程或购电因素",
        "必要工艺、交期、负荷结构、固定费摊薄、价表或归集错误",
        "完整事件与实际日期价格、合同及分项账单；物理效率与费用机制分别解释",
        frozenset(),
        frozenset({"COST-SHIFT", "COST-PROCURE", "COST-BASIC"}),
    ),
)

# Knowledge base 5.4, plus the quality block it names apart.
EVIDENCE_GRADES = ("数据直接证明", "数据推断且现场证据支持", "假设待验证", "质量阻断")
# Knowledge base 6.3.
CONCLUSION_STATES = (
    "观测事实成立，机制未决",
    "机制获范围内支持",
    "条件性解释",
    "修正后保留",
    "假设被否定或关闭",
    "转计量或基础资料核查",
    "证据不足，继续未决",
)
FORBIDDEN = "不写：相关性高所以存在浪费；功率贡献最大所以设备是根因；夜班偏高所以人员未关机；检修后变好所以全部由于老化；比值低所以系统高效。"


def competing_rows(item: dict[str, Any]) -> list[Competing]:
    method_id = item["finding_method"]["method_id"]
    categories = {proposal["item_id"] for proposal in item.get("candidate_categories", [])}
    return [row for row in COMPETING if method_id in row.methods or row.categories & categories]


def checklist(item: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a person has to check for one item; every judgment column is left empty."""

    causes = item.get("root_cause_candidates", [])
    return {
        "item_id": item["id"],
        "necessary_service": (decision or {}).get("service") or "未知",
        "possible_necessary_services": item.get("possible_necessary_services", []),
        "facts": [
            {"claim": "程序在所列时间窗和全期回放算出的数值（见本项“程序计算”）", "grade_prefill": "数据直接证明"},
            *[
                {"claim": f"[{CAUSE_KINDS.get(cause.get('kind', 'loss'), '')}] {cause['cause']}", "grade_prefill": "假设待验证"}
                for cause in causes
            ],
        ],
        "competing": [
            {"trigger": row.trigger, "keep": row.keep, "discriminate": row.discriminate} for row in competing_rows(item)
        ]
        + [
            {"trigger": "模型写的非损耗解释", "keep": cause["cause"], "discriminate": cause["how_to_check"]}
            for cause in causes
            if cause.get("kind", "loss") != "loss"
        ],
        "questions": [
            {
                "evidence": "、".join(item.get("evidence_ids", [])) or item["phenomenon"],
                "fact": cause["cause"],
                "records": cause["how_to_check"],
            }
            for cause in causes
        ]
        + [{"evidence": "还缺的资料", "fact": missing, "records": ""} for missing in item.get("missing_info", [])],
    }


def _md(value: Any) -> str:
    return " ".join(str("" if value is None else value).split()).replace("|", "／")


def checklist_markdown(lists: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## 人工核实清单",
        "",
        "下面各表的空列由人填写；程序只列出要核的事、知识库里的竞争解释和模型起草的现场问题，不判定原因，也不下结论。",
        f"证据等级可选：{'、'.join(EVIDENCE_GRADES)}。结论状态可选：{'、'.join(CONCLUSION_STATES)}。",
        f"现场回答“无法提供”不算“否”，转为替代测量或暂停该主张。{FORBIDDEN}",
        "",
    ]
    if not lists:
        return [*lines, "（没有保留或需补资料的项）", ""]
    for entry in lists:
        lines += [f"### {entry['item_id']} 核实", ""]
        services = "；".join(entry["possible_necessary_services"]) or "模型未列"
        lines.append(f"- 必要服务（人确认）：{_md(entry['necessary_service'])}；模型列出的可能必要服务：{_md(services)}")
        lines.append("- 结论状态（人填）：")
        lines += ["", "| 主张 | 程序预填等级 | 人定等级 | 依据 |", "|---|---|---|---|"]
        lines += [f"| {_md(fact['claim'])} | {fact['grade_prefill']} |  |  |" for fact in entry["facts"]]
        lines += ["", "| 触发现象 | 须保留的竞争解释 | 最小鉴别记录或试验 | 已排除/未排除（人填） | 依据 |", "|---|---|---|---|---|"]
        lines += [
            f"| {_md(row['trigger'])} | {_md(row['keep'])} | {_md(row['discriminate'])} |  |  |"
            for row in entry["competing"]
        ] or ["| （知识库表里没有对应这项方法和类别的行） |  |  |  |  |"]
        lines += ["", "| 触发证据 | 待确认事实 | 所需记录或问什么 | 问谁 | 回答/状态 |", "|---|---|---|---|---|"]
        lines += [
            f"| {_md(row['evidence'])} | {_md(row['fact'])} | {_md(row['records'])} |  |  |"
            for row in entry["questions"]
        ]
        lines.append("")
    return lines


def _table(head: list[str], rows: list[list[str]]) -> str:
    cells = "".join(f"<th>{html.escape(name)}</th>" for name in head)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>" for row in rows)
    return f"<table class='check'><tr>{cells}</tr>{body}</table>"


def checklist_html(lists: list[dict[str, Any]]) -> str:
    parts = [
        "<h2>人工核实清单</h2>",
        "<p>空列由人填写；程序只列出要核的事、知识库里的竞争解释和模型起草的现场问题，不判定原因，也不下结论。</p>",
        f"<p class='note'>证据等级可选：{html.escape('、'.join(EVIDENCE_GRADES))}。结论状态可选：{html.escape('、'.join(CONCLUSION_STATES))}。"
        f"现场回答“无法提供”不算“否”。{html.escape(FORBIDDEN)}</p>",
    ]
    if not lists:
        parts.append("<p>（没有保留或需补资料的项）</p>")
    for entry in lists:
        services = "；".join(entry["possible_necessary_services"]) or "模型未列"
        parts.append(f"<h3>{html.escape(entry['item_id'])} 核实</h3>")
        parts.append(
            f"<p>必要服务（人确认）：{html.escape(entry['necessary_service'])}；模型列出的可能必要服务：{html.escape(services)}</p>"
            "<p>结论状态（人填）：</p>"
        )
        parts.append(
            _table(["主张", "程序预填等级", "人定等级", "依据"], [[fact["claim"], fact["grade_prefill"], "", ""] for fact in entry["facts"]])
        )
        parts.append(
            _table(
                ["触发现象", "须保留的竞争解释", "最小鉴别记录或试验", "已排除/未排除（人填）", "依据"],
                [[row["trigger"], row["keep"], row["discriminate"], "", ""] for row in entry["competing"]]
                or [["（知识库表里没有对应这项方法和类别的行）", "", "", "", ""]],
            )
        )
        parts.append(
            _table(
                ["触发证据", "待确认事实", "所需记录或问什么", "问谁", "回答/状态"],
                [[row["evidence"], row["fact"], row["records"], "", ""] for row in entry["questions"]],
            )
        )
    return "\n".join(parts)


__all__ = [
    "COMPETING",
    "CONCLUSION_STATES",
    "EVIDENCE_GRADES",
    "checklist",
    "checklist_html",
    "checklist_markdown",
    "competing_rows",
]
