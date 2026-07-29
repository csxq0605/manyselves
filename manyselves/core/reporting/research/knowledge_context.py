"""Deterministically assemble project Knowledge for report-writing tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..source_ledger import SourceLedger
from ..store import ReportingStore
from ..models import SpecialTopicPlan
from ..taxonomy import REPORT_TAXONOMY
from .reference_library import ReferenceDocument, ReferenceLibrary


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    path: Path
    text: str
    source_ids: tuple[str, ...]


class KnowledgeContextBuilder:
    """Build bounded, taxonomy-aligned context without relying on model retrieval."""

    MAX_SNIPPET_CHARS = 900
    MAX_MODULE_CHARS = 14_000
    QUALITY_TERMS = (
        "报告质量",
        "结构完整性",
        "风险分析",
        "建议可执行性",
        "跨KU",
    )

    def __init__(self, workspace: Path, run_id: str):
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.library = ReferenceLibrary(self.workspace)
        self.ledger = SourceLedger(self.workspace, run_id)
        self.store = ReportingStore(self.workspace)
        self._documents: tuple[ReferenceDocument, ...] | None = None

    def _load_documents(self) -> tuple[ReferenceDocument, ...]:
        if self._documents is not None:
            return self._documents
        if not self.library.root.is_dir():
            self._documents = ()
            return self._documents
        documents: list[ReferenceDocument] = []
        supported = self.library.TEXT_SUFFIXES | self.library.DOCUMENT_SUFFIXES
        for path in sorted(self.library.root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in supported:
                continue
            try:
                relative = path.relative_to(self.workspace).as_posix()
                documents.append(self.library.open(relative))
            except (OSError, ValueError):
                continue
        self._documents = tuple(documents)
        return self._documents

    @staticmethod
    def _score(document: ReferenceDocument, terms: tuple[str, ...]) -> int:
        lowered = document.text.casefold()
        return sum(lowered.count(term.casefold()) for term in terms if term)

    @classmethod
    def _snippet(cls, text: str, terms: tuple[str, ...]) -> str:
        lowered = text.casefold()
        positions = [lowered.find(term.casefold()) for term in terms if term and term.casefold() in lowered]
        if not positions:
            return ""
        first = min(positions)
        heading_start = text.rfind("\n#", 0, first)
        start = heading_start + 1 if heading_start >= 0 else max(0, first - 180)
        snippet = text[start : start + cls.MAX_SNIPPET_CHARS]
        return re.sub(r"\n{3,}", "\n\n", snippet).strip()

    def _ranked(self, terms: tuple[str, ...], limit: int) -> list[ReferenceDocument]:
        ranked = sorted(
            ((self._score(document, terms), document) for document in self._load_documents()),
            key=lambda item: (-item[0], item[1].relative_path),
        )
        return [document for score, document in ranked if score > 0][:limit]

    def _register(self, document: ReferenceDocument) -> str:
        return self.ledger.register_local(
            document.title, document.relative_path, document.text
        ).id

    def build_module(self, module_id: str) -> KnowledgeContext:
        module = REPORT_TAXONOMY[module_id]
        lines = [
            f"# 模块 {module_id} 确定性知识上下文",
            "",
            "以下内容是可追溯的项目知识参考，不是模型认知边界，也不得作为客户现场事实。",
            "可结合模型已有专业知识解释机理、提出备选原因、比较方案和补充行业实践；涉及本项目是否存在、具体数值、设备状态或合规结论时仍必须依赖 E-* 项目证据。",
        ]
        source_ids: list[str] = []
        for submodule_id, submodule in module.submodules.items():
            terms = (submodule_id, submodule.title)
            documents = self._ranked(terms, 2)
            lines.extend(["", f"## {submodule_id} {submodule.title}"])
            if not documents:
                lines.append(
                    "未检索到项目 Knowledge 匹配项；可使用模型专业知识继续分析，"
                    "但通用知识或假设不得写成客户现场事实。"
                )
                continue
            for document in documents:
                source_id = self._register(document)
                source_ids.append(source_id)
                snippet = self._snippet(document.text, terms)
                lines.extend(
                    [
                        f"### {source_id} {document.title}",
                        f"来源：{document.relative_path}",
                        snippet,
                    ]
                )
        text = "\n".join(lines).strip()
        if len(text) > self.MAX_MODULE_CHARS:
            text = text[: self.MAX_MODULE_CHARS].rsplit("\n", 1)[0] + "\n\n[知识上下文已按成本上限截断]"
        path = self.store.write_text(
            f"Work/runs/{self.run_id}/context/module-{module_id}-knowledge.md",
            text + "\n",
        )
        return KnowledgeContext(
            path=path.relative_to(self.workspace),
            text=text,
            source_ids=tuple(dict.fromkeys(source_ids)),
        )

    def build_quality(self) -> KnowledgeContext:
        lines = [
            "# 总编内容质量门参考",
            "",
            "总编必须保留全部固定子模块，并检查风险机理、建议可执行性、跨模块关系和结构完整性。",
        ]
        source_ids: list[str] = []
        for document in self._ranked(self.QUALITY_TERMS, 5):
            source_id = self._register(document)
            source_ids.append(source_id)
            lines.extend(
                [
                    "",
                    f"## {source_id} {document.title}",
                    f"来源：{document.relative_path}",
                    self._snippet(document.text, self.QUALITY_TERMS),
                ]
            )
        text = "\n".join(lines).strip()
        path = self.store.write_text(
            f"Work/runs/{self.run_id}/context/report-quality-criteria.md",
            text + "\n",
        )
        return KnowledgeContext(
            path=path.relative_to(self.workspace),
            text=text,
            source_ids=tuple(dict.fromkeys(source_ids)),
        )

    @staticmethod
    def _special_topic_terms(title: str, requirement: str) -> tuple[str, ...]:
        phrases = [
            title,
            *re.split(r"[\s、，。；：:（）()【】/]+", requirement),
        ]
        return tuple(
            dict.fromkeys(
                phrase.strip()
                for phrase in phrases
                if 2 <= len(phrase.strip()) <= 24
            )
        )[:16]

    def build_special_topics(self, plan: SpecialTopicPlan) -> KnowledgeContext:
        """Build a bounded Knowledge packet aligned to the dynamic Chapter 4 plan."""

        lines = [
            "# 专项问题分析知识上下文",
            "",
            f"要求来源：{plan.source_ref.as_posix()}",
            "以下 R-* 内容是可追溯的项目知识参考，不是客户现场事实，也不是模型认知边界。",
            "可使用模型已有专业知识补充机理、方案比较、行业实践与验证思路；"
            "当前项目是否存在某问题、具体数值、设备状态及合规结论仍只能由 E-* 项目证据支持。",
        ]
        source_ids: list[str] = []
        for section in plan.sections:
            terms = self._special_topic_terms(section.title, section.requirement)
            lines.extend(
                [
                    "",
                    f"## {section.section_id} {section.title}",
                    "",
                    "Inputs 简要要求：",
                    section.requirement,
                ]
            )
            documents = self._ranked(terms, 3)
            if not documents:
                lines.extend(
                    [
                        "",
                        "未检索到项目 Knowledge 匹配项；可使用模型专业知识补充分析，"
                        "但必须明确其为通用工程判断，不得写成客户事实。",
                    ]
                )
                continue
            for document in documents:
                source_id = self._register(document)
                source_ids.append(source_id)
                lines.extend(
                    [
                        "",
                        f"### {source_id} {document.title}",
                        f"来源：{document.relative_path}",
                        self._snippet(document.text, terms),
                    ]
                )
        text = "\n".join(lines).strip()
        if len(text) > self.MAX_MODULE_CHARS:
            text = (
                text[: self.MAX_MODULE_CHARS].rsplit("\n", 1)[0]
                + "\n\n[专项知识上下文已按成本上限截断]"
            )
        path = self.store.write_text(
            f"Work/runs/{self.run_id}/context/special-topic-knowledge.md",
            text + "\n",
        )
        return KnowledgeContext(
            path=path.relative_to(self.workspace),
            text=text,
            source_ids=tuple(dict.fromkeys(source_ids)),
        )
