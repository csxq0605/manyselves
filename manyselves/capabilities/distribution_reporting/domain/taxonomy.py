"""Capability-owned run-scoped taxonomy for distribution reporting.

The built-in tree is the process default for non-authoring inspection.
Every new authoring run replaces it with the immutable tree parsed from that
run's frozen S4-6 workbook.
"""

import hashlib
import re
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


@dataclass(frozen=True, slots=True)
class SubmoduleDefinition:
    id: str
    title: str
    module_id: str


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    id: str
    title: str
    sections: dict[str, SubmoduleDefinition]
    submodules: dict[str, SubmoduleDefinition]

    @property
    def groups(self) -> dict[str, SubmoduleDefinition]:
        """Return structural headings that own child sections but no prose part."""

        return {
            section_id: section
            for section_id, section in self.sections.items()
            if section_id not in self.submodules
        }


def _module(
    module_id: str,
    title: str,
    sections: tuple[tuple[str, str], ...],
) -> ModuleDefinition:
    definitions = {
        section_id: SubmoduleDefinition(
            id=section_id,
            title=section_title,
            module_id=module_id,
        )
        for section_id, section_title in sections
    }
    return ModuleDefinition(
        id=module_id,
        title=title,
        sections=definitions,
        submodules={
            section_id: definition
            for section_id, definition in definitions.items()
            if not any(
                other_id.startswith(f"{section_id}.")
                for other_id in definitions
            )
        },
    )


_DEFAULT_REPORT_TAXONOMY: dict[str, ModuleDefinition] = {
    "2.1": _module(
        "2.1",
        "配电系统架构问题",
        (
            ("2.1.1", "配电系统负荷分配与过载风险"),
            ("2.1.2", "关键负荷供电路径与应急/备用供电的问题"),
            ("2.1.3", "配网自动化、备用电源自动切换（可能性及功能验证）"),
            ("2.1.4", "防止2路电源并联产生环流"),
            ("2.1.5", "系统无功补偿与电容柜问题"),
        ),
    ),
    "2.2": _module(
        "2.2",
        "环境工况风险",
        (
            ("2.2.1", "来自电能质量的风险"),
            ("2.2.1.1", "谐波风险情况"),
            ("2.2.1.2", "电压扰动情况"),
            ("2.2.1.3", "频繁启动与冲击负荷"),
            ("2.2.2", "其他运行工况风险"),
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
            ("2.3.3", "电压事件（过压）的防范"),
        ),
    ),
    "2.4": _module(
        "2.4",
        "配电设备/元件内在风险",
        (
            ("2.4.1", "配置与选型问题"),
            ("2.4.1.1", "额定/分断能力"),
            ("2.4.1.2", "配电柜分隔形式"),
            ("2.4.1.3", "配电设备安全连锁/闭锁"),
            ("2.4.1.4", "设备分合/储能/工作位置显示"),
            ("2.4.2", "安装规范性问题"),
            ("2.4.2.1", "裸露导体防护"),
            ("2.4.2.2", "等电位连接与接地问题"),
            ("2.4.2.3", "电气连接问题"),
            ("2.4.2.4", "标牌标识"),
            ("2.4.2.5", "电缆、桥架、母线安装问题"),
            ("2.4.2.6", "设备外壳IP等级与封堵问题"),
            ("2.4.3", "带病运行问题汇总"),
            ("2.4.3.1", "低压回路剩余电流过大"),
            ("2.4.3.2", "部分高压柜照明功能缺失"),
            ("2.4.3.3", "部分高压柜柜内除湿装置未开启"),
            ("2.4.4", "末端配电抽查情况"),
        ),
    ),
    "2.5": _module(
        "2.5",
        "运维管理与风险管控机制",
        (
            ("2.5.1", "SOP/EOP"),
            ("2.5.2", "图纸资料"),
            ("2.5.3", "运维（巡检、维护）的实施与组织"),
            ("2.5.3.1", "运维组织架构与人员配备"),
            ("2.5.3.2", "关键配电设备维护工作全面性检查"),
            ("2.5.3.3", "配电设备维保覆盖"),
            ("2.5.4", "运维的智能化手段"),
            ("2.5.5", "配电室装备与LOTO流程的实施"),
            ("2.5.6", "备件管理"),
            ("2.5.7", "退市设备与生命周期管理"),
        ),
    ),
}

_ACTIVE_REPORT_TAXONOMY: ContextVar[Mapping[str, ModuleDefinition] | None] = (
    ContextVar("active_report_taxonomy", default=None)
)


class _RunTaxonomyView(Mapping[str, ModuleDefinition]):
    """Mapping facade whose value follows the current async run context."""

    @staticmethod
    def _current() -> Mapping[str, ModuleDefinition]:
        return _ACTIVE_REPORT_TAXONOMY.get() or _DEFAULT_REPORT_TAXONOMY

    def __getitem__(self, key: str) -> ModuleDefinition:
        return self._current()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._current())

    def __len__(self) -> int:
        return len(self._current())


REPORT_TAXONOMY: Mapping[str, ModuleDefinition] = _RunTaxonomyView()


def _section_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = format(float(value), ".12g")
    else:
        text = str(value).strip()
    text = re.sub(r"\s+", "", text)
    return text if re.fullmatch(r"2\.[1-5](?:\.\d+)*", text) else None


def _validate_taxonomy(modules: Mapping[str, ModuleDefinition]) -> None:
    expected_modules = tuple(f"2.{index}" for index in range(1, 6))
    if tuple(modules) != expected_modules:
        raise ValueError(
            "S4-6 workbook taxonomy must contain ordered modules 2.1 through 2.5; "
            f"actual={list(modules)}"
        )
    seen: set[str] = set(modules)
    for module_id, module in modules.items():
        if not module.title.strip() or not module.sections:
            raise ValueError(f"workbook taxonomy module is empty: {module_id}")
        for section_id, section in module.sections.items():
            if section_id in seen:
                raise ValueError(f"duplicate workbook taxonomy id: {section_id}")
            seen.add(section_id)
            if section.module_id != module_id or not section.title.strip():
                raise ValueError(f"invalid workbook taxonomy section: {section_id}")
            parent_id = section_id.rsplit(".", 1)[0]
            if parent_id != module_id and parent_id not in module.sections:
                raise ValueError(
                    f"workbook taxonomy section has no parent: {section_id} -> {parent_id}"
                )


def parse_report_taxonomy_workbook(
    path: Path,
    *,
    source_ref: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Parse the complete 2.1-2.5 heading tree from a frozen S4-6 workbook."""

    path = Path(path)
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        if "评估信息汇总表" not in workbook.sheetnames:
            raise ValueError("S4-6 workbook is missing sheet: 评估信息汇总表")
        sheet = workbook["评估信息汇总表"]
        module_titles: dict[str, str] = {}
        section_rows: dict[str, list[tuple[str, str]]] = {}
        ordered_ids: list[str] = []
        for row in range(1, sheet.max_row + 1):
            identifier = _section_id(sheet.cell(row, 2).value)
            if identifier is None:
                continue
            title = next(
                (
                    str(sheet.cell(row, column).value).strip()
                    for column in range(3, 6)
                    if sheet.cell(row, column).value is not None
                    and str(sheet.cell(row, column).value).strip()
                ),
                "",
            )
            if not title:
                raise ValueError(
                    f"S4-6 workbook taxonomy title is empty at {sheet.title}!B{row}:E{row}"
                )
            if identifier in ordered_ids:
                raise ValueError(f"duplicate S4-6 workbook taxonomy id: {identifier}")
            ordered_ids.append(identifier)
            if identifier.count(".") == 1:
                module_titles[identifier] = title
                section_rows.setdefault(identifier, [])
            else:
                module_id = ".".join(identifier.split(".")[:2])
                if module_id not in module_titles:
                    raise ValueError(
                        f"S4-6 workbook taxonomy child precedes module: {identifier}"
                    )
                section_rows[module_id].append((identifier, title))
        modules = {
            module_id: _module(
                module_id,
                module_titles[module_id],
                tuple(section_rows[module_id]),
            )
            for module_id in module_titles
        }
        _validate_taxonomy(modules)
        digest = source_sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
        payload = report_taxonomy_snapshot(
            modules,
            source_ref=source_ref or path.as_posix(),
            source_sha256=digest,
            sheet=sheet.title,
        )
        payload["source_kind"] = "xlsx"
        return payload
    finally:
        workbook.close()


def taxonomy_from_snapshot(payload: Mapping[str, Any]) -> dict[str, ModuleDefinition]:
    """Validate and materialize one persisted run taxonomy snapshot."""

    if payload.get("schema_version") != 1:
        raise ValueError("unsupported report taxonomy snapshot version")
    modules: dict[str, ModuleDefinition] = {}
    for raw_module in payload.get("modules") or ():
        if not isinstance(raw_module, Mapping):
            raise ValueError("invalid report taxonomy module payload")
        module_id = str(raw_module.get("id") or "")
        title = str(raw_module.get("title") or "").strip()
        sections = tuple(
            (str(item.get("id") or ""), str(item.get("title") or "").strip())
            for item in (raw_module.get("sections") or ())
            if isinstance(item, Mapping)
        )
        modules[module_id] = _module(module_id, title, sections)
    _validate_taxonomy(modules)
    return modules


def report_taxonomy_snapshot(
    modules: Mapping[str, ModuleDefinition],
    *,
    source_ref: str,
    source_sha256: str,
    sheet: str,
) -> dict[str, Any]:
    """Serialize one validated taxonomy without report prose or evidence."""

    _validate_taxonomy(modules)
    payload = {
        "schema_version": 1,
        "source_ref": source_ref,
        "source_sha256": source_sha256,
        "sheet": sheet,
        "modules": [
            {
                "id": module.id,
                "title": module.title,
                "sections": [
                    {"id": section.id, "title": section.title}
                    for section in module.sections.values()
                ],
            }
            for module in modules.values()
        ],
    }
    return payload


def activate_report_taxonomy(
    payload: Mapping[str, Any],
) -> Token[Mapping[str, ModuleDefinition] | None]:
    """Bind a validated immutable taxonomy to the current async run context."""

    modules = taxonomy_from_snapshot(payload)
    return _ACTIVE_REPORT_TAXONOMY.set(modules)


def reset_report_taxonomy(
    token: Token[Mapping[str, ModuleDefinition] | None],
) -> None:
    _ACTIVE_REPORT_TAXONOMY.reset(token)


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
    for section_id, section in definition.sections.items():
        heading_level = section_id.count(".") + 1
        if section_id not in definition.submodules:
            blocks.append(f"{'#' * heading_level} {section_id} {section.title}")
            continue
        submodule_id = section_id
        submodule = section
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
        body_heading = "#" * min(heading_level + 1, 6)
        normalized_body = "\n".join(
            re.sub(r"^#{1,6}\s+", f"{body_heading} ", line)
            if re.match(r"^#{1,6}\s+", line)
            else line
            for line in lines
        ).strip()
        blocks.append(
            f"{'#' * heading_level} {submodule_id} {submodule.title}\n\n"
            f"{normalized_body}"
        )
    return "\n\n".join(blocks)
