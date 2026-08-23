"""Capability-owned persistent E/R/W source registry for one reporting run."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    SourceKind,
    SourceRecord,
)

from .state.parallel import exclusive_file_lock

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(path, threading.RLock())


class SourceLedger:
    """Register sources with stable per-kind identifiers and durable metadata."""

    def __init__(self, workspace: Path, run_id: str):
        workspace = Path(workspace).resolve()
        safe_run_id = Path(run_id).name
        if not run_id or safe_run_id != run_id:
            raise ValueError("run_id must be a single safe path component")
        self.workspace = workspace
        self.run_id = safe_run_id
        self.path = workspace / "Work/runs" / safe_run_id / "ledgers/sources.json"
        self.content_root = workspace / "Work/runs" / safe_run_id / "sources"
        self.process_lock_path = self.path.with_suffix(".lock")
        self._lock = _lock_for(self.path)

    def _load(self) -> list[SourceRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [SourceRecord.model_validate(item) for item in raw]

    @property
    def records(self) -> list[SourceRecord]:
        with self._lock:
            with exclusive_file_lock(self.process_lock_path):
                return self._load()

    def _persist(self, records: list[SourceRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                [record.model_dump(mode="json") for record in records],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _persist_content(self, source_id: str, content: str) -> Path:
        if Path(source_id).name != source_id or not source_id:
            raise ValueError("source id must be one safe path component")
        path = self.content_root / f"{source_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".txt.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return path.relative_to(self.workspace)

    def content_ref(self, source_id: str) -> Path | None:
        path = self.content_root / f"{Path(source_id).name}.txt"
        return path.relative_to(self.workspace) if path.is_file() else None

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _next_id(records: list[SourceRecord], kind: SourceKind) -> str:
        prefix = {SourceKind.LOCAL_REFERENCE: "R-", SourceKind.WEB: "W-"}[kind]
        numbers = [
            int(record.id.removeprefix(prefix))
            for record in records
            if record.kind == kind and record.id.removeprefix(prefix).isdigit()
        ]
        return f"{prefix}{max(numbers, default=0) + 1:03d}"

    def _validate_local_locator(
        self,
        locator: str,
        *,
        namespace: Literal["project", "global"] = "project",
    ) -> None:
        locator_path = locator.split("；", 1)[0].split("#", 1)[0]
        if namespace == "project":
            resolved = (self.workspace / locator_path).resolve()
            knowledge_root = (self.workspace / "Knowledge").resolve()
            if not resolved.is_relative_to(knowledge_root) or resolved == knowledge_root:
                raise ValueError("local references must be located beneath project Knowledge")
        else:
            portable = PurePosixPath(locator_path)
            if (
                portable.is_absolute()
                or "\\" in locator_path
                or len(portable.parts) < 2
                or portable.parts[0] != "GlobalKnowledge"
                or any(part in {"", ".", ".."} for part in portable.parts)
            ):
                raise ValueError("global references must use a GlobalKnowledge locator")

    def register_many(self, entries: list[dict[str, Any]]) -> list[SourceRecord]:
        """Register an ordered batch with one lock, id allocation, and registry write.

        Each entry uses ``kind`` plus ``title``, ``locator`` and ``content``.
        Project evidence additionally supplies ``evidence_id``; web entries may
        supply publisher/published_at/scope_note.  Validation completes for the
        entire batch before the registry is replaced, so a bad later item cannot
        partially allocate R/W identifiers.
        """

        if not entries:
            return []
        normalized: list[dict[str, Any]] = []
        for raw in entries:
            entry = dict(raw)
            try:
                kind = SourceKind(entry["kind"])
                title = str(entry["title"]).strip()
                locator = str(entry["locator"]).strip()
                content = str(entry["content"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("source batch entry is incomplete or invalid") from exc
            if not title or not locator:
                raise ValueError("source title and locator must be non-empty")
            namespace = str(entry.get("namespace") or "project")
            if namespace not in {"project", "global"}:
                raise ValueError("local reference namespace must be project or global")
            if kind is SourceKind.LOCAL_REFERENCE:
                self._validate_local_locator(locator, namespace=namespace)
            evidence_id = (
                str(entry.get("evidence_id") or "")
                if kind is SourceKind.PROJECT_EVIDENCE
                else ""
            )
            if kind is SourceKind.PROJECT_EVIDENCE and not evidence_id.startswith("E-"):
                raise ValueError("project evidence id must start with E-")
            normalized.append(
                {
                    **entry,
                    "kind": kind,
                    "title": title,
                    "locator": locator,
                    "content": content,
                    "content_sha256": self._digest(content),
                    "evidence_id": evidence_id,
                    "namespace": namespace,
                    "scope_note": (
                        f"knowledge_namespace={namespace}"
                        if kind is SourceKind.LOCAL_REFERENCE
                        else entry.get("scope_note")
                    ),
                }
            )

        with self._lock:
            with exclusive_file_lock(self.process_lock_path):
                records = self._load()
                results: list[SourceRecord] = []
                pending_contents: list[tuple[str, str]] = []
                for entry in normalized:
                    kind = entry["kind"]
                    digest = entry["content_sha256"]
                    locator = entry["locator"]
                    existing = None
                    if kind is SourceKind.PROJECT_EVIDENCE:
                        existing = next(
                            (
                                record
                                for record in records
                                if record.id == entry["evidence_id"]
                            ),
                            None,
                        )
                        if existing is not None and (
                            existing.locator != locator
                            or existing.content_sha256 != digest
                        ):
                            raise ValueError(
                                "conflicting project evidence id: "
                                f"{entry['evidence_id']}"
                            )
                    else:
                        existing = next(
                            (
                                record
                                for record in records
                                if record.kind == kind
                                and record.locator == locator
                                and record.content_sha256 == digest
                            ),
                            None,
                        )
                    if (
                        existing is not None
                        and kind is SourceKind.LOCAL_REFERENCE
                        and existing.scope_note != entry["scope_note"]
                    ):
                        record_index = records.index(existing)
                        existing = existing.model_copy(
                            update={"scope_note": entry["scope_note"]}
                        )
                        records[record_index] = existing
                    if existing is None:
                        source_id = (
                            entry["evidence_id"]
                            if kind is SourceKind.PROJECT_EVIDENCE
                            else self._next_id(records, kind)
                        )
                        existing = SourceRecord(
                            id=source_id,
                            kind=kind,
                            title=entry["title"],
                            locator=locator,
                            publisher=entry.get("publisher"),
                            published_at=entry.get("published_at"),
                            accessed_at=(
                                datetime.now(UTC).date().isoformat()
                                if kind is SourceKind.WEB
                                else None
                            ),
                            scope_note=entry.get("scope_note"),
                            content_sha256=digest,
                        )
                        records.append(existing)
                    results.append(existing)
                    pending_contents.append((existing.id, entry["content"]))
                for source_id, content in pending_contents:
                    self._persist_content(source_id, content)
                self._persist(records)
                return results

    def register_local(
        self,
        title: str,
        locator: str,
        content: str,
        *,
        namespace: Literal["project", "global"] = "project",
    ) -> SourceRecord:
        return self.register_many(
            [
                {
                    "kind": SourceKind.LOCAL_REFERENCE,
                    "title": title,
                    "locator": locator,
                    "content": content,
                    "namespace": namespace,
                }
            ]
        )[0]

    def register_project(
        self, evidence_id: str, title: str, locator: str, content: str
    ) -> SourceRecord:
        return self.register_many(
            [
                {
                    "kind": SourceKind.PROJECT_EVIDENCE,
                    "evidence_id": evidence_id,
                    "title": title,
                    "locator": locator,
                    "content": content,
                }
            ]
        )[0]

    def register_web(
        self,
        title: str,
        url: str,
        content: str,
        publisher: str | None = None,
        published_at: str | None = None,
        scope_note: str | None = None,
    ) -> SourceRecord:
        return self.register_many(
            [
                {
                    "kind": SourceKind.WEB,
                    "title": title,
                    "locator": url,
                    "content": content,
                    "publisher": publisher,
                    "published_at": published_at,
                    "scope_note": scope_note,
                }
            ]
        )[0]
