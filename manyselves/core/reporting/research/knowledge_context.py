"""Deterministically assemble project/global Knowledge for report-writing tasks."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import SpecialTopicPlan
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

from ..input_snapshot import RunInputSnapshotStore
from .reference_library import ReferenceDocument, ReferenceLibrary


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    path: Path
    text: str
    source_ids: tuple[str, ...]
    snapshot_ref: Path | None = None


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
    REPORTING_RULE_TERMS = (
        "报告知识库",
        "报告模板",
        "报告生成",
        "报告写作",
        "报告质量",
        "章节结构",
        "章节引言",
        "结论段落生成",
        "风险分析段落生成",
        "建议段落生成",
        "表格证据链",
        "表格数据填充",
        "图片规则",
        "跨ku关联分析",
        "第三章汇总",
        "排版格式",
        "篇幅要求",
        "字数要求",
        "交付文档",
        "审稿规范",
        "自动校验规则",
        "内部黑盒",
        "调用契约",
        "客户问答边界",
        "保密规则",
    )
    MAX_DOCUMENTS_PER_SUBMODULE = 3

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        *,
        global_root: Path | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        snapshot_path = self.workspace / f"Work/runs/{run_id}/input-snapshot.json"
        if snapshot_path.is_file():
            snapshot = RunInputSnapshotStore(self.workspace).load(run_id)
            self.library = ReferenceLibrary(
                self.workspace,
                knowledge_root=snapshot.scope_root(self.workspace, "Knowledge"),
                index_root=(
                    self.workspace / f"Work/runs/{run_id}/indexes/knowledge"
                ),
                global_root=global_root,
            )
        else:
            self.library = ReferenceLibrary(
                self.workspace,
                global_root=global_root,
            )
        self.ledger = SourceLedger(self.workspace, run_id)
        self.store = ReportingStore(self.workspace)
        self._documents: tuple[ReferenceDocument, ...] | None = None

    @property
    def _manifest_path(self) -> Path:
        return (
            self.workspace
            / "Work/runs"
            / self.run_id
            / "context-manifests/knowledge-sources.json"
        )

    def freeze_sources(self) -> tuple[ReferenceDocument, ...]:
        """Freeze the run's composite knowledge sources on first use."""
        if self._documents is not None:
            return self._documents
        current = self.library.documents()
        if self._manifest_path.is_file():
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            frozen: list[ReferenceDocument] = []
            current_by_identity = {
                (document.namespace, document.relative_path, document.content_sha256): document
                for document in current
            }
            for item in payload.get("sources", []):
                identity = (
                    item.get("namespace"),
                    item.get("logicalPath"),
                    item.get("sha256"),
                )
                document = current_by_identity.get(identity)
                if document is None:
                    raise ValueError(
                        "knowledge source snapshot no longer matches the frozen run"
                    )
                frozen.append(document)
            self._documents = tuple(frozen)
            return self._documents
        self._documents = current
        self.store.write_json(
            f"Work/runs/{self.run_id}/context-manifests/knowledge-sources.json",
            {
                "sources": [
                    {
                        "logicalPath": document.relative_path,
                        "namespace": document.namespace,
                        "sha256": document.content_sha256,
                    }
                    for document in self._documents
                ]
            },
        )
        return self._documents

    def _load_documents(self) -> tuple[ReferenceDocument, ...]:
        return self.freeze_sources()

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

    @classmethod
    def _normalized_snippet_hash(cls, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text)
        normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def _domain_terms(cls) -> tuple[str, ...]:
        return tuple(
            value
            for module in REPORT_TAXONOMY.values()
            for submodule_id, submodule in module.submodules.items()
            for value in (submodule_id, submodule.title)
        )

    @classmethod
    def _is_reporting_only(cls, document: ReferenceDocument) -> bool:
        identity = f"{document.title}\n{document.relative_path}".casefold()
        if any(term.casefold() in identity for term in cls.REPORTING_RULE_TERMS):
            return True
        body = document.text.casefold()
        has_reporting_rule = any(
            term.casefold() in body for term in cls.REPORTING_RULE_TERMS
        )
        has_domain_content = any(term in document.text for term in cls._domain_terms())
        return has_reporting_rule and not has_domain_content

    def _ranked(
        self,
        terms: tuple[str, ...],
        limit: int,
        *,
        professional_only: bool = False,
    ) -> list[ReferenceDocument]:
        ranked = sorted(
            (
                (self._score(document, terms), document)
                for document in self._load_documents()
                if not professional_only or not self._is_reporting_only(document)
            ),
            key=lambda item: (-item[0], item[1].relative_path),
        )
        return [document for score, document in ranked if score > 0][:limit]

    def _register(self, document: ReferenceDocument) -> str:
        return self.ledger.register_local(
            document.title,
            document.relative_path,
            document.text,
            namespace=document.namespace,
        ).id

    def build_module(self, module_id: str) -> KnowledgeContext:
        module = REPORT_TAXONOMY[module_id]
        header_lines = [
            f"# 模块 {module_id} 确定性知识上下文",
            "",
            "以下内容是可追溯的项目/全局知识参考，不是模型认知边界，也不得作为客户现场事实。",
            "可结合模型已有专业知识解释机理、提出备选原因、比较方案和补充行业实践；涉及本项目是否存在、具体数值、设备状态或合规结论时仍必须依赖 E-* 项目证据。",
        ]
        header = "\n".join(header_lines).strip()
        submodule_count = len(module.submodules)
        separator_reserve = 2 * submodule_count
        per_submodule_chars = max(
            240,
            (self.MAX_MODULE_CHARS - len(header) - separator_reserve)
            // submodule_count,
        )
        blocks: list[str] = []
        source_ids: list[str] = []
        snippet_sources: dict[str, str] = {}
        for submodule_id, submodule in module.submodules.items():
            terms = (submodule_id, submodule.title)
            documents = self._ranked(
                terms,
                self.MAX_DOCUMENTS_PER_SUBMODULE,
                professional_only=True,
            )
            block_lines = [f"## {submodule_id} {submodule.title}"]
            if not documents:
                block_lines.append(
                    "未检索到项目/全局 Knowledge 匹配项；可使用模型专业知识继续分析，"
                    "但通用知识或假设不得写成客户现场事实。"
                )
            else:
                for document in documents:
                    snippet = self._snippet(document.text, terms)
                    if not snippet:
                        continue
                    source_id = self._register(document)
                    snippet_hash = self._normalized_snippet_hash(snippet)
                    existing_source = snippet_sources.get(snippet_hash)
                    if existing_source is not None:
                        entry = f"复用知识引用：{existing_source}（相同片段不重复注入）"
                    else:
                        entry_prefix = (
                            f"### {source_id} {document.title}\n"
                            f"来源：{document.relative_path}\n"
                        )
                        remaining = (
                            per_submodule_chars
                            - len("\n".join(block_lines))
                            - len(entry_prefix)
                            - 2
                        )
                        if remaining < 80:
                            continue
                        entry = entry_prefix + snippet[:remaining].rstrip()
                        snippet_sources[snippet_hash] = source_id
                    candidate = "\n".join([*block_lines, entry])
                    if len(candidate) > per_submodule_chars:
                        continue
                    block_lines.append(entry)
                    source_ids.append(existing_source or source_id)
            if len(block_lines) == 1:
                block_lines.append(
                    "匹配项因本模块配额或去重规则未重复展开；可通过已列 R-* 引用追溯。"
                )
            blocks.append("\n".join(block_lines))
        text = "\n\n".join([header, *blocks]).strip()
        if len(text) > self.MAX_MODULE_CHARS:
            raise ValueError("module Knowledge assembly exceeded its deterministic budget")
        path = self.store.write_text(
            f"Work/runs/{self.run_id}/context/module-{module_id}-knowledge.md",
            text + "\n",
        )
        return KnowledgeContext(
            path=path.relative_to(self.workspace),
            text=text,
            source_ids=tuple(dict.fromkeys(source_ids)),
            snapshot_ref=self.library.snapshot_manifest_ref(),
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
            snapshot_ref=self.library.snapshot_manifest_ref(),
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
            "以下 R-* 内容是可追溯的项目/全局知识参考，不是客户现场事实，也不是模型认知边界。",
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
                        "未检索到项目/全局 Knowledge 匹配项；可使用模型专业知识补充分析，"
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
            snapshot_ref=self.library.snapshot_manifest_ref(),
        )
