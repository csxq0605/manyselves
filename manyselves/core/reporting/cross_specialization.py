"""Module-owned Cross review lenses.

These are task specializations for one shared Cross reviewer role.  They are
deliberately not Module Skills: Cross reads the five approved subjects and
uses each lens only to discover interface defects whose writeback owner is the
named module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .taxonomy import REPORT_TAXONOMY


@dataclass(frozen=True, slots=True)
class CrossLaneSpecialization:
    module_id: str
    title: str
    review_focus: tuple[str, ...]

    def prompt_context(self) -> str:
        focus = "\n".join(f"- {item}" for item in self.review_focus)
        return (
            f'<cross_lane_specialization owner_module_id="{self.module_id}">\n'
            f"责任落点：{self.module_id} {self.title}\n"
            "这不是模块写作 Skill，也不授权重审该模块的局部专业质量。读取五个已批准模块，"
            "只发现跨模块关系对本责任模块的判断、风险、行动、前提或验收造成的接口缺陷。\n"
            f"{focus}\n"
            "finding 的 owner_module_id 必须等于本责任模块，target_submodule_ids 也只能属于"
            "本责任模块。已完整写回且无需返工的关系，只有在本责任模块是 involved modules "
            "中编号最小者时才提交 synthesis_input，以避免多个 lane 重复申报同一关系。\n"
            "</cross_lane_specialization>"
        )


CROSS_LANE_SPECIALIZATIONS: dict[str, CrossLaneSpecialization] = {
    "2.1": CrossLaneSpecialization(
        module_id="2.1",
        title=REPORT_TAXONOMY["2.1"].title,
        review_focus=(
            "核对负荷分配、关键供电路径和备用切换结论是否吸收 2.2 工况、2.3 保护、2.4 设备能力及 2.5 操作条件。",
            "核对双电源、自动切换、防并联和恢复路径是否与保护配合、机械/电气闭锁及 SOP/EOP 的实际接口一致。",
            "核对无功补偿、冲击负荷等架构措施是否遗漏谐波、保护动作、元件耐受和联合验证依赖。",
        ),
    ),
    "2.2": CrossLaneSpecialization(
        module_id="2.2",
        title=REPORT_TAXONOMY["2.2"].title,
        review_focus=(
            "核对谐波、电压扰动和冲击负荷的风险判断是否连接 2.1 供电方式、2.3 保护行为、2.4 元件能力及 2.5 监测处置。",
            "核对发热、局放和物理环境异常是否与设备状态、安装缺陷、维护覆盖和停运条件形成可执行接口。",
            "核对工况异常从出现、监测、告警到隔离和恢复的传播链是否存在跨模块盲点。",
        ),
    ),
    "2.3": CrossLaneSpecialization(
        module_id="2.3",
        title=REPORT_TAXONOMY["2.3"].title,
        review_focus=(
            "核对保护方案、定值和选择性结论是否使用与 2.1 实际拓扑及运行方式一致的前提。",
            "核对短路、接地、零序/漏电及过压防范是否与 2.2 工况、2.4 额定/分断能力和接地连接边界一致。",
            "核对保护投退、试验、变更和动作复盘是否与 2.5 的 SOP、图纸、维护和联合验收闭环衔接。",
        ),
    ),
    "2.4": CrossLaneSpecialization(
        module_id="2.4",
        title=REPORT_TAXONOMY["2.4"].title,
        review_focus=(
            "核对设备选型、额定和分断能力判断是否建立在 2.1 拓扑/负荷、2.2 工况和 2.3 故障参数的共同边界上。",
            "核对闭锁、接地、连接、封堵和安装缺陷是否削弱保护动作、切换操作或故障隔离屏障。",
            "核对带病运行及末端抽查发现是否进入 2.5 的维护优先级、LOTO、备件和生命周期处置。",
        ),
    ),
    "2.5": CrossLaneSpecialization(
        module_id="2.5",
        title=REPORT_TAXONOMY["2.5"].title,
        review_focus=(
            "核对 SOP/EOP、LOTO 和人员责任是否覆盖 2.1 切换路径、2.3 保护投退及 2.4 闭锁和设备操作约束。",
            "核对图纸、台账、巡检和智能监测能否承接 2.2 工况信号、2.3 动作记录及 2.4 状态缺陷。",
            "核对维护、备件和生命周期计划是否按其他模块揭示的风险优先级、前置依赖和联合验收顺序闭环。",
        ),
    ),
}


def cross_lane_specialization(module_id: str) -> CrossLaneSpecialization:
    try:
        return CROSS_LANE_SPECIALIZATIONS[module_id]
    except KeyError as exc:
        raise ValueError(f"unsupported Cross owner module: {module_id}") from exc
