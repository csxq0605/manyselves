"""Read-only index over normalized project evidence artifacts."""

from __future__ import annotations

from pathlib import Path

from ..models import EvidenceItem


def project_evidence_locator(item: EvidenceItem) -> str:
    """Return the one canonical, backward-compatible locator for an E-* item."""

    source = item.source
    parts = [source.path.as_posix()]
    if source.sheet:
        parts.append(f"工作表={source.sheet}")
    if source.cell:
        parts.append(f"单元格={source.cell}")
    if source.page:
        parts.append(f"页码={source.page}")
    return "；".join(parts)


class ProjectEvidenceIndex:
    """Search one run's immutable normalized customer-evidence snapshot."""

    def __init__(self, workspace: Path, run_id: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.path = (
            self.workspace / f"Work/runs/{run_id}/preparation/evidence.jsonl"
            if run_id
            else self.workspace / "Work/evidence.jsonl"
        )

    def items(self) -> list[EvidenceItem]:
        if not self.path.exists():
            return []
        return [
            EvidenceItem.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def get(self, evidence_id: str) -> EvidenceItem:
        if not evidence_id.startswith("E-"):
            raise ValueError("project source ids must start with E-")
        for item in self.items():
            if item.id == evidence_id:
                return item
        raise ValueError(f"unknown project evidence id: {evidence_id}")

    def search(self, query: str, limit: int = 10) -> list[EvidenceItem]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms or limit <= 0:
            return []
        ranked: list[tuple[int, EvidenceItem]] = []
        for item in self.items():
            text = " ".join(
                str(value)
                for value in (
                    item.id,
                    item.subject,
                    item.fact,
                    item.value or "",
                    item.unit or "",
                    item.observed_at or "",
                    item.source.path,
                    item.source.sheet or "",
                    item.source.cell or "",
                )
            ).casefold()
            score = sum(text.count(term) for term in terms)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [item for _, item in ranked[:limit]]
