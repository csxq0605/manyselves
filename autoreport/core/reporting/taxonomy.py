"""Fixed report taxonomy shared by coverage, drafting, and review stages."""

from dataclasses import dataclass


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
        "供配电系统概况",
        (
            ("2.1.1", "供配电系统基本信息"),
            ("2.1.2", "电源及供电可靠性"),
            ("2.1.3", "主接线及运行方式"),
            ("2.1.4", "主要设备配置"),
            ("2.1.5", "负荷概况"),
        ),
    ),
    "2.2": _module(
        "2.2",
        "供配电系统运行分析",
        (
            ("2.2.1.1", "变压器负载率"),
            ("2.2.1.2", "线路及开关负载率"),
            ("2.2.1.3", "负荷分布与运行方式"),
            ("2.2.2.1", "电压质量"),
            ("2.2.2.2", "功率因数"),
            ("2.2.2.3", "谐波与三相不平衡"),
        ),
    ),
    "2.3": _module(
        "2.3",
        "供配电系统保护分析",
        (
            ("2.3.1", "继电保护配置"),
            ("2.3.2", "保护定值与配合"),
            ("2.3.3", "保护装置运行状态"),
        ),
    ),
    "2.4": _module(
        "2.4",
        "供配电设备安全状态",
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
        "供配电安全管理",
        (
            ("2.5.1", "安全管理制度"),
            ("2.5.2", "运行维护管理"),
            ("2.5.3.1", "操作票管理"),
            ("2.5.3.2", "工作票管理"),
            ("2.5.3.3", "交接班与巡检记录"),
            ("2.5.4", "人员资质与培训"),
            ("2.5.5", "应急管理"),
            ("2.5.6", "安全工器具管理"),
            ("2.5.7", "隐患排查与整改闭环"),
        ),
    ),
}


def resolve_submodule(submodule_id: str) -> SubmoduleDefinition:
    """Return a fixed submodule definition or reject an invented identifier."""

    for module in REPORT_TAXONOMY.values():
        if submodule := module.submodules.get(submodule_id):
            return submodule
    raise ValueError(f"unknown report submodule: {submodule_id}")
