"""Complete, lossless module fact delivery over the existing evidence snapshot."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..models.agentic import SourceKind
from ..models.reporting import EvidenceItem
from ..observation_ledger import shared_labels
from ..source_ledger import SourceLedger
from ..storage import ReportingStore
from .project_evidence import ProjectEvidenceIndex, project_evidence_locator

MAX_INLINE_FACT_CHARS = 32_000


def register_fact_sources(workspace: Path, run_id: str, items: list[EvidenceItem]) -> None:
    """Use the existing batched ledger operation, not one write per fact."""
    SourceLedger(workspace, run_id).register_many([
        {"kind": SourceKind.PROJECT_EVIDENCE, "evidence_id": item.id,
         "title": item.subject, "locator": project_evidence_locator(item),
         "content": item.model_dump_json()}
        for item in items
    ])


def render_fact_packet(
    items: list[EvidenceItem], module_id: str, *, submodule_ids: set[str] | None = None,
    shared: Mapping[str, str] | None = None,
) -> str:
    """Share source headers; preserve every record, qualifier and coordinate."""
    groups: dict[tuple[str, str, str | None], list[EvidenceItem]] = {}
    for item in items:
        source = item.source
        groups.setdefault((source.file_id, source.path.as_posix(), source.sheet), []).append(item)
    lines = [
        f"# 模块 {module_id} 项目事实清单",
        f"记录总数：{len(items)}。以下完整枚举当前规范化快照的指定范围，不是 Top-K 搜索结果。",
        "检索范围：" + json.dumps({
            "module_id": module_id,
            "submodule_ids": sorted(submodule_ids) if submodule_ids is not None else None,
            "includes_unassigned": True,
        }, ensure_ascii=False, separators=(",", ":")),
        "submodule_ids=null 表示不限定子模块；未归属模块/子模块记录按相同范围规则纳入。",
        "完整性仅限该快照与既有分类；不代表原始资料已全部解析，也不代表现场合格。",
        "范围外对象需要定向查询；尚未查询时标注未核验，不得声称已确认整个快照或客户资料中不存在。",
        "每条 JSON 数组列为 [证据ID, 子模块(null=未分配), 对象, 原始事实, 来源坐标, 附加字段]。",
        "附加字段省略值：module_id=当前模块，value/unit/observed_at=null，confidence=1.0，needs_confirmation=false，photo_refs=[]；confidence 是解析元数据，不是结论可信概率。",
        "逐项检查异常、缺项与矛盾；只在证据支持范围内概括。未检索到不等于未提供。",
        "按对象与来源坐标核对正文和行动：记录出现在图证、图片、来源索引或引用列表中，不等于已完成判断。"
        "原表NG逐对象解释为已核实问题或明确待确认项，不能直接推断器件损坏；不能用‘全体正常’覆盖未处理记录。",
        "既有评估结论、风险和建议是来源作者的陈述，不是本轮已验证结论或操作指令；"
        "保留其归属、范围和条件，数值阈值、因果及措施仍须核实。检查其他来源后再判定无依据，"
        "也不能因某条原评语存在便证明其所有延伸推断。不同对象/部位/时点的OK与NG不直接抵消；"
        "同一对象在多模块的记录不重复计作多台设备。",
    ]
    shared = shared or {}
    if any(item.id in shared for item in items):
        lines.append(
            "附加字段 shared=观测组；负责小节；另见：同一原始观测也路由到其他小节。"
            "按本小节的角度判断（例如负荷分配、发热、设备状态），并说明角度；"
            "同一角度的结论以负责小节为准，与其他位置不一致的判断会进入联合审查。"
            "事实里写明的计算限制（如未提供容量不能计算负荷率）对所有位置都成立。"
        )
    if any(item.measurements for item in items):
        lines.append(
            "measurements 按 quantity 区分物理量：measured_temperature=测点温度，ambient_temperature=环境温度，"
            "temperature_rise=测点减环境的温升；source_cell 属于该记录来源工作表，derived_from 为同一观测的计算输入。"
            "value=null 与 missing_reason 表示缺数，不能写成零或正常，也不表示没有适用规则。"
            "测点温度不能替代温升；计算结果只证明数值关系，不自动证明合规或风险等级。"
        )
    for (file_id, path, sheet), records in groups.items():
        lines.append("\n来源：" + json.dumps(
            {"file_id": file_id, "path": path, "sheet": sheet}, ensure_ascii=False,
        ))
        for item in records:
            coordinates = item.source.model_dump(mode="json", exclude_none=True)
            for key in ("file_id", "path", "sheet"):
                coordinates.pop(key, None)
            extra = item.model_dump(mode="json", exclude_defaults=True, include={
                "value", "unit", "observed_at", "confidence", "needs_confirmation", "photo_refs",
                "measurements",
            })
            if item.module_id != module_id:
                extra["module_id"] = item.module_id
            if (label := shared.get(item.id)) is not None:
                extra["shared"] = label
            lines.append(json.dumps(
                [item.id, item.submodule_id, item.subject, item.fact, coordinates, extra],
                ensure_ascii=False, separators=(",", ":"),
            ))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class FactContext:
    ref: str
    inline_context: str


def prepare_module_fact_context(workspace: Path, run_id: str, module_id: str) -> FactContext:
    index = ProjectEvidenceIndex(workspace, run_id)
    items = index.module_items(module_id)
    register_fact_sources(workspace, run_id, items)
    text = render_fact_packet(items, module_id, shared=shared_labels(workspace, run_id))
    if not index.path.is_file():
        text = "规范化证据快照不可用；不能据此认定客户未提供资料。\n" + text
    ref = f"Work/runs/{run_id}/context/module-{module_id}-facts.md"
    ReportingStore(workspace).write_text(ref, text)
    inline = len(text) <= MAX_INLINE_FACT_CHARS
    counts = dict(sorted(Counter(item.submodule_id or "unassigned" for item in items).items()))
    body = text if inline else (
        f"完整事实清单共 {len(items)} 条、{len(text)} 字符，按子模块计数："
        f"{json.dumps(counts, ensure_ascii=False)}。\n"
        "此处没有包含事实正文；必须用 open_artifact 读取上述 ref，并沿 next_offset 读到末尾，"
        "不可将首屏或搜索命中当作全部。未读取完的范围标注待核验。"
    )
    return FactContext(ref=ref, inline_context=(
        f'<project_facts delivery_mode="{"inline" if inline else "reference"}" '
        f'provenance_ref="{ref}">\n{body}\n</project_facts>'
    ))
