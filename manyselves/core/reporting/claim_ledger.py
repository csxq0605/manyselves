"""Hidden claim/source ledger and deterministic citation binding.

This module deliberately keeps audit structure behind the report surface.  It
never generates or rewrites report prose; it only validates sources, assigns
stable markers, and binds explicit Claim markers to source footnotes.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

import re

from pydantic import Field, model_validator

from .agentic_models import (
    CitationEntry,
    CitationPlan,
    ClaimRecord,
    SourceKind,
    SourceRecord,
    StrictModel,
)


class CitationBindingError(ValueError):
    """Raised when an approved claim cannot be bound to edited prose."""


def claim_citation_marker(claim_id: str) -> str:
    """Return the opaque marker placed once by the Claim-owning module author."""

    return f"[[CLAIM:{claim_id}]]"


class ClaimLedger(StrictModel):
    """Auditable claims and their project/reference/web sources."""

    claims: list[ClaimRecord] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ledger(self) -> "ClaimLedger":
        claim_counts = Counter(claim.id for claim in self.claims)
        duplicate_claims = sorted(key for key, count in claim_counts.items() if count > 1)
        source_counts = Counter(source.id for source in self.sources)
        duplicate_sources = sorted(key for key, count in source_counts.items() if count > 1)
        if duplicate_claims:
            raise ValueError(f"duplicate claim ids: {duplicate_claims}")
        if duplicate_sources:
            raise ValueError(f"duplicate source ids: {duplicate_sources}")

        known_sources = set(source_counts)
        for claim in self.claims:
            unknown = sorted(set(claim.source_ids) - known_sources)
            if unknown:
                raise ValueError(f"claim {claim.id} references unknown source: {unknown}")
            if claim.footnote_required and not claim.source_ids and not claim.unresolved:
                raise ValueError(f"claim {claim.id} requires a footnote source")
            if (
                claim.claim_type == "risk_judgment"
                and not claim.unresolved
                and not any(source_id.startswith("E-") for source_id in claim.source_ids)
            ):
                raise ValueError(f"risk claim {claim.id} requires at least one E-* source")

        for source in self.sources:
            self._validate_source_locator(source)
        return self

    @staticmethod
    def _validate_source_locator(source: SourceRecord) -> None:
        if source.kind == SourceKind.WEB:
            parsed = urlparse(source.locator)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"web source {source.id} requires an HTTP(S) URL locator")
            if not source.accessed_at:
                raise ValueError(f"web source {source.id} requires accessed_at")
        elif source.kind == SourceKind.LOCAL_REFERENCE:
            normalized = source.locator.replace("\\", "/")
            path_part = normalized.split("；", 1)[0].split("#", 1)[0]
            if not path_part.startswith("Knowledge/") or "/../" in f"/{path_part}/":
                raise ValueError(
                    f"local reference {source.id} must be located beneath project Knowledge"
                )
        elif source.kind == SourceKind.PROJECT_EVIDENCE:
            if source.locator.startswith(("http://", "https://")):
                raise ValueError(f"project evidence {source.id} must use a project file locator")

    def build_citation_plan(self) -> CitationPlan:
        """Assign markers in approved claim order and build the source index."""

        entries = self._citation_entries()
        return CitationPlan(entries=entries, evidence_index_markdown=self.source_index_markdown())

    def _citation_entries(self) -> list[CitationEntry]:
        cited_claims = [
            claim for claim in self.claims if claim.footnote_required and claim.source_ids
        ]
        return [
            CitationEntry(marker=index, claim_id=claim.id, source_ids=claim.source_ids)
            for index, claim in enumerate(cited_claims, start=1)
        ]

    def source_index_markdown(self) -> str:
        groups = (
            (SourceKind.PROJECT_EVIDENCE, "项目证据 E-*"),
            (SourceKind.LOCAL_REFERENCE, "本地参考 R-*"),
            (SourceKind.WEB, "网络来源 W-*"),
        )
        lines = ["## 证据与来源索引", ""]
        entries = self._citation_entries()
        lines.extend(["### 脚注对应关系", ""])
        if entries:
            for entry in entries:
                lines.append(
                    f"- [{entry.marker}] {entry.claim_id}：" + "、".join(entry.source_ids)
                )
        else:
            lines.append("本报告无关键脚注。")
        lines.append("")
        for kind, title in groups:
            lines.extend([f"### {title}", ""])
            records = sorted((source for source in self.sources if source.kind == kind), key=lambda x: x.id)
            if not records:
                lines.extend(["本报告未引用此类来源。", ""])
                continue
            for source in records:
                metadata = [source.title, source.locator]
                if source.publisher:
                    metadata.append(f"机构={source.publisher}")
                if source.published_at:
                    metadata.append(f"发布日期={source.published_at}")
                if source.accessed_at:
                    metadata.append(f"访问日期={source.accessed_at}")
                if source.scope_note:
                    metadata.append(f"适用范围={source.scope_note}")
                lines.append(f"- {source.id}：" + "；".join(metadata))
            lines.append("")
        return "\n".join(lines).rstrip()

    def bind_citations(self, narrative: str) -> str:
        """Replace exact Claim markers with numeric citation tokens."""

        cited = narrative
        plan_by_claim = {entry.claim_id: entry for entry in self.build_citation_plan().entries}
        for claim in self.claims:
            entry = plan_by_claim.get(claim.id)
            if entry is None:
                continue
            marker = claim_citation_marker(claim.id)
            occurrences = cited.count(marker)
            if occurrences != 1:
                raise CitationBindingError(
                    f"claim {claim.id} citation marker must occur exactly once; got {occurrences}"
                )
            cited = cited.replace(marker, f"[[CITE:{entry.marker}]]", 1)
        unresolved = sorted(set(re.findall(r"\[\[CLAIM:(C-[^\]\s]+)\]\]", cited)))
        if unresolved:
            raise CitationBindingError(
                f"report contains unresolved Claim citation markers: {unresolved}"
            )
        return cited
