"""Fixed report taxonomy shared by coverage, drafting, and review stages."""

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class SubmoduleDefinition:
    id: str
    title: str
    module_id: str


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    id: str
    title: str
    submodules: dict[str, SubmoduleDefinition]


def _module(
    module_id: str,
    title: str,
    submodules: tuple[tuple[str, str], ...],
) -> ModuleDefinition:
    return ModuleDefinition(
        id=module_id,
        title=title,
        submodules={
            submodule_id: SubmoduleDefinition(
                id=submodule_id,
                title=submodule_title,
                module_id=module_id,
            )
            for submodule_id, submodule_title in submodules
        },
    )


REPORT_TAXONOMY: dict[str, ModuleDefinition] = {
    "2.1": _module(
        "2.1",
        "电力系统架构问题",
        (
            ("2.1.1", "电力系统负荷分配与过载风险"),
            ("2.1.2", "关键负荷供电路径与应急/备用供电的问题"),
            ("2.1.3", "配网自动化、备用电源自动切换"),
            ("2.1.4", "防止2路电源并联环流返送"),
            ("2.1.5", "系统无功补偿与电容柜问题"),
        ),
    ),
    "2.2": _module(
        "2.2",
        "环境工况风险",
        (
            ("2.2.1.1", "谐波风险情况"),
            ("2.2.1.2", "电压扰动情况"),
            ("2.2.1.3", "频繁启动与冲击负荷"),
            ("2.2.2.1", "低压配电设备发热情况"),
            ("2.2.2.2", "高压配电设备局放情况"),
            ("2.2.2.3", "其他物理环境风险"),
        ),
    ),
    "2.3": _module(
        "2.3",
        "针对故障的保护",
        (
            ("2.3.1", "配电系统保护方案与定值的论证计算"),
            ("2.3.2", "零序/漏电的防范"),
            ("2.3.3", "电压事件（过压、欠压）的防范"),
        ),
    ),
    "2.4": _module(
        "2.4",
        "配电设备/元件风险",
        (
            ("2.4.1.1", "额定/极限容量"),
            ("2.4.1.2", "配电柜分隔形式"),
            ("2.4.1.3", "配电设备安全连锁/闭锁"),
            ("2.4.1.4", "设备分合/工作位置显示"),
            ("2.4.2.1", "裸露导体防护"),
            ("2.4.2.2", "等电位连接与接地问题"),
            ("2.4.2.3", "电气连接问题"),
            ("2.4.2.4", "标牌标识"),
            ("2.4.2.5", "电缆、桥架、母线安装问题"),
            ("2.4.2.6", "设备外壳IP等级与封堵问题"),
            ("2.4.3.1", "低压回路剩余电流过大"),
            ("2.4.3.2", "部分高压柜照明功能缺失"),
            ("2.4.3.3", "部分高压柜柜内除湿装置未开启"),
            ("2.4.4", "末端配电抽查情况"),
        ),
    ),
    "2.5": _module(
        "2.5",
        "运维管理与风险管控",
        (
            ("2.5.1", "SOP/EOP"),
            ("2.5.2", "图纸资料"),
            ("2.5.3.1", "运维组织架构与人员配备"),
            ("2.5.3.2", "关键配电设备维护工作全面性检查"),
            ("2.5.3.3", "配电设备维保覆盖"),
            ("2.5.4", "运维的智能化手段"),
            ("2.5.5", "配电室装备与LOTO流程的实施"),
            ("2.5.6", "退市设备与生命周期管理"),
            ("2.5.7", "备件管理"),
        ),
    ),
}


def resolve_submodule(submodule_id: str) -> SubmoduleDefinition:
    """Return a fixed submodule definition or reject an invented identifier."""

    for module in REPORT_TAXONOMY.values():
        if submodule := module.submodules.get(submodule_id):
            return submodule
    raise ValueError(f"unknown report submodule: {submodule_id}")


def compose_module_markdown(
    module_id: str, submodule_narratives: dict[str, str]
) -> str:
    """Derive the only canonical module Markdown from fixed submodule prose."""

    definition = REPORT_TAXONOMY[module_id]
    expected = set(definition.submodules)
    actual = set(submodule_narratives)
    if actual != expected:
        raise ValueError(
            "cannot compose module Markdown with incomplete taxonomy; "
            f"missing={sorted(expected - actual)}; extra={sorted(actual - expected)}"
        )

    blocks = [f"## {module_id} {definition.title}"]
    for submodule_id, submodule in definition.submodules.items():
        narrative = submodule_narratives[submodule_id].strip()
        if not narrative:
            raise ValueError(f"cannot compose empty submodule narrative: {submodule_id}")
        lines = narrative.splitlines()
        first_content = next(
            (index for index, line in enumerate(lines) if line.strip()), None
        )
        if first_content is not None and re.match(
            rf"^#{{1,6}}\s+{re.escape(submodule_id)}(?:\.|\s|$)",
            lines[first_content].strip(),
        ):
            del lines[first_content]
            while first_content < len(lines) and not lines[first_content].strip():
                del lines[first_content]
        normalized_body = "\n".join(
            re.sub(r"^#{1,6}\s+", "#### ", line) if re.match(r"^#{1,6}\s+", line) else line
            for line in lines
        ).strip()
        blocks.append(
            f"### {submodule_id} {submodule.title}\n\n{normalized_body}"
        )
    return "\n\n".join(blocks)
