"""Cover facts and draft status of the delivered report.

The cover of the report template names the client and the assessed site.  Those
facts come from the project's 收资表 evidence when it states them as a labelled
value (``客户名称=…`` / ``项目地点：…``); otherwise the cover says ``待补``
instead of keeping the template's placeholder.

A report is a draft when the request asked to keep a draft for missing
evidence (``missing_evidence_policy == "draft"``) and the run registered data
gaps.  The draft status is shown the same way in the Markdown and the DOCX.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .source_index_sections import load_data_gaps

MISSING_COVER_VALUE = "待补"
DRAFT_SUFFIX = "（草稿）"
DRAFT_NOTICE = (
    "草稿：本报告存在资料缺口，相关结论待资料补齐后确认；"
    "缺口清单见《证据与来源索引》中的数据缺口表。"
)

_CLIENT_LABELS = (
    "委托方名称",
    "委托单位",
    "委托方",
    "客户名称",
    "客户单位",
    "单位名称",
    "公司名称",
    "企业名称",
)
_SITE_LABELS = (
    "项目地点",
    "项目地址",
    "评估地点",
    "评估地区",
    "所在地区",
    "厂区名称",
    "厂区",
    "厂址",
    "站点名称",
    "地址",
    "地区",
)
_PLACEHOLDER = re.compile(r"X{2,}|〈[^〉]*〉|^待补$|^[-—/／\s]*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CoverFacts:
    client_name: str = MISSING_COVER_VALUE
    site_name: str = MISSING_COVER_VALUE


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _labelled_value(text: str, labels: tuple[str, ...]) -> str | None:
    pattern = re.compile(
        r"(?:^|[；;\n|/，,])\s*(?:" + "|".join(re.escape(label) for label in labels) + r")"
        r"\s*[=：:]\s*([^；;\n|]+)"
    )
    for match in pattern.finditer(text):
        value = match.group(1).strip().strip("。")
        if value and not _PLACEHOLDER.search(value):
            return value
    return None


def _from_collection_sheet(item: Any) -> bool:
    source = _field(item, "source") or {}
    return "收资" in f"{_field(source, 'sheet') or ''}{_field(source, 'path') or ''}"


def report_cover_facts(evidence_items: Iterable[Any]) -> CoverFacts:
    """Find the client and site stated by the project's 收资表 evidence."""

    client = site = None
    for item in filter(_from_collection_sheet, evidence_items):
        text = f"{_field(item, 'subject') or ''}\n{_field(item, 'fact') or ''}"
        client = client or _labelled_value(text, _CLIENT_LABELS)
        site = site or _labelled_value(text, _SITE_LABELS)
        if client and site:
            break
    return CoverFacts(
        client_name=client or MISSING_COVER_VALUE,
        site_name=site or MISSING_COVER_VALUE,
    )


def report_is_draft(workspace: Path, state: Mapping[str, Any]) -> bool:
    """Whether the delivered report must be marked as a draft."""

    policy = _field(state.get("request"), "missing_evidence_policy")
    if policy != "draft":
        return False
    return bool(load_data_gaps(Path(workspace), str(state["run_id"])))


def draft_title(title: str) -> str:
    return title if title.endswith(DRAFT_SUFFIX) else title + DRAFT_SUFFIX


__all__ = [
    "DRAFT_NOTICE",
    "DRAFT_SUFFIX",
    "MISSING_COVER_VALUE",
    "CoverFacts",
    "draft_title",
    "report_cover_facts",
    "report_is_draft",
]
