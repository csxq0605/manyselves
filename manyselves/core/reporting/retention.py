"""Read-only storage accounting and retention previews.

The reporting runtime has had several storage formats over its lifetime.  A
delivery may be a legacy materialized copy (v1), an all-CAS package (v2), or a
mixed package (v3).  This module deliberately treats those formats as input
data only.  It never removes, moves, quarantines, or rewrites an artifact.

``ReportingRetentionPlanner.generate`` is kept for the existing workflow API:
it returns the same ``{"usage": ..., "plan": ...}`` shape and writes the two
summary files used by the workflow.  ``preview``/``scan`` are the strict
read-only entry points used by the storage preview command and by maintenance
audits.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, TypeAlias

from .store import ReportingStore


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_CAS_PREFIX = "Work/content/sha256/"
_GENERATED_SUMMARIES = {
    "Work/storage-usage.json",
    "Work/retention-plan.json",
}
_MANIFEST_WORDS = (
    "manifest",
    "receipt",
    "retention-plan",
    "storage-usage",
    "storage-compaction",
    "template-provenance",
    "conversation",
    "session",
    "provenance",
    "workflow",
    "storage",
    "version",
    "workflow-state",
    "checkpoint",
    "marker",
    "active",
    "pinned",
)
_REFERENCE_KEY_WORDS = (
    "ref",
    "path",
    "file",
    "artifact",
    "source",
    "summary",
    "document",
    "module",
    "delivery",
    "session",
    "template",
    "blob",
    "view",
)
_HASH_KEY_WORDS = ("sha", "hash", "digest", "checksum")


# ``RetentionPlan`` was used as a loose dictionary by an early preview
# prototype.  Keep the importable name as a type alias without imposing a
# schema on callers that still pass/return ordinary dictionaries.
RetentionPlan: TypeAlias = dict[str, Any]


@dataclass
class _BlobRecord:
    ref: str
    path: Path
    digest: str
    logical_bytes: int
    allocated_bytes: int
    mtime: float
    mtime_ns: int
    references: set[str] = field(default_factory=set)
    all_reference_sources: set[str] = field(default_factory=set)
    active_or_pinned: bool = False


@dataclass(frozen=True)
class _PathReference:
    path: Path
    key: str


class ReportingRetentionPlanner:
    """Account for CAS/materialized storage and produce a safe preview.

    The scanner only reads files.  A preview never creates a marker, alters a
    manifest, or mutates a CAS inode.  ``generate`` additionally persists the
    two legacy summary JSON files; those are new summaries, not rewrites of
    lifecycle manifests.
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)
        self.content_root = self.workspace / "Work" / "content" / "sha256"
        self._digest_cache: dict[Path, tuple[str, int]] = {}

    def generate(
        self,
        *,
        grace_days: int = 7,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate and persist the legacy usage/retention summaries.

        This compatibility method retains the previous API.  Use
        :meth:`preview` when a completely non-writing call is required.
        """

        result = self.preview(grace_days=grace_days, now=now)
        self.store.write_json("Work/storage-usage.json", result["usage"])
        self.store.write_json("Work/retention-plan.json", result["plan"])
        return result

    def preview(
        self,
        *,
        grace_days: int = 7,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return a read-only storage usage and candidate preview.

        ``grace_days`` is measured against the canonical blob mtime.  Active
        and pinned markers always override the grace calculation: a protected
        blob is never marked eligible, even when it is old and otherwise
        unreferenced.
        """

        if grace_days < 0:
            raise ValueError("grace_days must be non-negative")
        generated_at = self._normalise_now(now)
        self._digest_cache.clear()

        blobs = self._scan_blobs()
        references: dict[str, set[str]] = {ref: set() for ref in blobs}
        all_reference_sources: dict[str, set[str]] = {
            ref: set() for ref in blobs
        }
        active_refs: set[str] = set()
        materialized_paths: set[Path] = set()
        view_paths: dict[Path, str] = {}
        view_logical_bytes = 0
        view_allocated_bytes = 0

        # Visible output directories can contain a CAS symlink, a fallback
        # copy, or an ordinary materialized artifact.  Work/Outputs is kept as
        # a compatibility root for older workspaces; Outputs is the current
        # project root.
        for root in self._view_roots():
            for path in self._regular_files(root):
                if path.is_symlink():
                    ref = self._cas_ref_from_path(path, blobs)
                    if ref is None:
                        continue
                    view_paths[path] = ref
                    self._register_reference(
                        ref,
                        path,
                        blobs,
                        references,
                        all_reference_sources,
                    )
                    view_logical_bytes += blobs[ref].logical_bytes
                    view_allocated_bytes += self._allocated_bytes(path)
                    continue
                materialized_paths.add(path)

                # A hard link (or a filesystem copy fallback whose hash is
                # exactly the canonical blob) is still a compatibility view,
                # not a second canonical blob.  The path is reclassified below
                # after manifest references have been read as well.
                ref = self._cas_ref_by_identity(path, blobs)
                if ref is not None and self._looks_like_view_path(path):
                    view_paths[path] = ref
                    materialized_paths.discard(path)
                    self._register_reference(
                        ref,
                        path,
                        blobs,
                        references,
                        all_reference_sources,
                    )
                    view_logical_bytes += blobs[ref].logical_bytes
                    view_allocated_bytes += self._allocated_bytes(path)

        manifest_paths = self._manifest_paths()
        manifest_refs: dict[Path, set[str]] = {}
        for manifest_path in manifest_paths:
            payloads = self._read_manifest_payloads(manifest_path)
            refs_for_manifest: set[str] = set()
            for payload in payloads:
                refs, path_refs, hashes = self._extract_references(payload)
                resolved_hash_refs = {
                    resolved
                    for raw_hash in hashes
                    if (resolved := self._blob_ref_for_digest(raw_hash, blobs))
                    is not None
                }
                for ref in refs | resolved_hash_refs:
                    if ref not in blobs:
                        continue
                    refs_for_manifest.add(ref)
                    self._register_reference(
                        ref,
                        manifest_path,
                        blobs,
                        references,
                        all_reference_sources,
                    )
                for path_ref in path_refs:
                    ref = self._register_path_reference(
                        path_ref,
                        manifest_path,
                        blobs,
                        references,
                        all_reference_sources,
                        materialized_paths,
                        view_paths,
                    )
                    if ref is not None:
                        refs_for_manifest.add(ref)
            manifest_refs[manifest_path] = refs_for_manifest

        # Active/pinned markers are a separate protection set.  A direct
        # marker reference protects a blob but does not turn a stale orphan
        # into a normal manifest reference; this lets the preview explain why
        # it is retained and guarantees ``eligible_after_grace == false``.
        self._scan_protection_markers(
            blobs=blobs,
            manifest_refs=manifest_refs,
            active_refs=active_refs,
            all_reference_sources=all_reference_sources,
        )
        for ref in active_refs:
            if ref in blobs:
                blobs[ref].active_or_pinned = True
        # Keep the per-record source copy used by candidate serialization in
        # sync with the scanner's de-duplicated reference maps.
        for ref, sources in all_reference_sources.items():
            if ref in blobs:
                blobs[ref].all_reference_sources.update(sources)
        for ref, sources in references.items():
            if ref in blobs:
                blobs[ref].references.update(sources)

        # Reclassify materialized fallback copies that are referenced by a
        # manifest and contain exactly one canonical blob.  This catches v3
        # ``artifact_storage=cas`` on filesystems where symlinks are disabled.
        for path in tuple(materialized_paths):
            ref = self._cas_ref_by_identity(path, blobs)
            if ref is None or not self._looks_like_view_path(path):
                continue
            materialized_paths.discard(path)
            view_paths[path] = ref
            self._register_reference(
                ref,
                path,
                blobs,
                references,
                all_reference_sources,
            )
            view_logical_bytes += blobs[ref].logical_bytes
            view_allocated_bytes += self._allocated_bytes(path)

        # Materialized paths referenced by manifests are counted once by
        # inode/path.  Metadata files (the manifests themselves, receipts, and
        # generated summaries) are not report artifacts and are intentionally
        # excluded from this metric.  Visible Outputs remain counted because
        # they are user-facing materialized views even without a manifest.
        materialized_logical_bytes = 0
        materialized_allocated_bytes = 0
        for path in sorted(materialized_paths):
            if path in view_paths or not path.is_file() or path.is_symlink():
                continue
            if self._is_metadata_path(path) and not self._is_visible_output(path):
                continue
            materialized_logical_bytes += path.stat().st_size
            materialized_allocated_bytes += self._allocated_bytes(path)

        cutoff = generated_at - timedelta(days=grace_days)
        referenced = {
            ref: record for ref, record in blobs.items() if references[ref]
        }
        unreferenced = {
            ref: record for ref, record in blobs.items() if not references[ref]
        }

        candidates: list[dict[str, Any]] = []
        potential_reclaim_bytes = 0
        potential_reclaim_allocated_bytes = 0
        for ref, record in sorted(unreferenced.items()):
            old_enough = record.mtime <= cutoff.timestamp()
            eligible = old_enough and not record.active_or_pinned
            if record.active_or_pinned:
                recommendation = "retain_active_or_pinned"
            elif old_enough:
                recommendation = "eligible_for_reclaim_preview"
            else:
                recommendation = "retain_until_grace"
            if eligible:
                potential_reclaim_bytes += record.logical_bytes
                potential_reclaim_allocated_bytes += record.allocated_bytes
            source_names = sorted(
                set(record.references) | set(record.all_reference_sources)
            )
            candidates.append(
                {
                    "blob_ref": ref,
                    "hash": record.digest,
                    "bytes": record.logical_bytes,
                    "mtime": record.mtime,
                    "mtime_ns": record.mtime_ns,
                    "modified_at": datetime.fromtimestamp(
                        record.mtime, timezone.utc
                    ).isoformat(),
                    "reference_sources": source_names,
                    "active_or_pinned": record.active_or_pinned,
                    # Protected blobs are never eligible, regardless of age.
                    "eligible_after_grace": eligible,
                    # This is intentionally a non-mutating action.  The
                    # recommendation describes a future, separately
                    # authorized operation without performing one here.
                    "action": "preview_only",
                    "recommendation": recommendation,
                }
            )

        canonical_logical_bytes = sum(
            record.logical_bytes for record in blobs.values()
        )
        canonical_allocated_bytes = sum(
            record.allocated_bytes for record in blobs.values()
        )
        referenced_blob_bytes = sum(
            record.logical_bytes for record in referenced.values()
        )
        unreferenced_blob_bytes = sum(
            record.logical_bytes for record in unreferenced.values()
        )
        generated_iso = generated_at.isoformat()
        usage: dict[str, Any] = {
            "generated_at": generated_iso,
            "content_store": "Work/content/sha256",
            "blob_count": len(blobs),
            "referenced_blob_count": len(referenced),
            "unreferenced_blob_count": len(unreferenced),
            "active_or_pinned_blob_count": sum(
                1 for record in blobs.values() if record.active_or_pinned
            ),
            # New explicit metric names.
            "canonical_logical_bytes": canonical_logical_bytes,
            "canonical_allocated_bytes": canonical_allocated_bytes,
            "materialized_logical_bytes": materialized_logical_bytes,
            "materialized_allocated_bytes": materialized_allocated_bytes,
            "compatibility_view_count": len(view_paths),
            "view_logical_bytes": view_logical_bytes,
            "view_allocated_bytes": view_allocated_bytes,
            "view_reused_bytes": view_logical_bytes,
            "referenced_blob_bytes": referenced_blob_bytes,
            "unreferenced_blob_bytes": unreferenced_blob_bytes,
            "potential_reclaim_bytes": potential_reclaim_bytes,
            "potential_reclaim_allocated_bytes": potential_reclaim_allocated_bytes,
            # Existing aliases retained for callers and old persisted reports.
            "canonical_bytes": canonical_logical_bytes,
            "allocated_bytes": canonical_allocated_bytes,
            "compatibility_view_logical_bytes": view_logical_bytes,
            "compatibility_view_allocated_bytes": view_allocated_bytes,
            "reused_bytes": view_logical_bytes,
            "materialized_bytes": materialized_logical_bytes,
            "manifest_count": len(manifest_paths),
        }
        plan: RetentionPlan = {
            "generated_at": generated_iso,
            "mode": "dry_run",
            "automatic_deletion": False,
            "grace_days": grace_days,
            "mark_sources": [
                "Work/content canonical CAS blobs",
                "Work and Outputs content-backed compatibility views",
                "v1/v2/v3 report-version manifests",
                "v1/v2/v3 delivery manifests",
                "typed delivery receipts",
                "conversation, template, session, and storage manifests",
                "active and pinned protection markers",
            ],
            "candidate_count": len(candidates),
            "candidate_bytes": sum(item["bytes"] for item in candidates),
            "candidate_allocated_bytes": sum(
                blobs[item["blob_ref"]].allocated_bytes for item in candidates
            ),
            "potential_reclaim_bytes": potential_reclaim_bytes,
            "potential_reclaim_allocated_bytes": potential_reclaim_allocated_bytes,
            "candidates": candidates,
            "execution_requirements": [
                "preview is read-only: no delete, move, quarantine, or manifest rewrite",
                "re-scan references immediately before any separately authorized mutation",
                "active or pinned blobs are never eligible",
                "require an explicit cleanup action outside this preview",
            ],
        }
        return {"usage": usage, "plan": plan}

    # ``scan`` is a descriptive alias used by maintenance callers.  Keep it
    # side-effect-free just like ``preview``.
    scan = preview

    def _content_refs(self, payload: Any) -> set[str]:
        """Compatibility helper returning canonical refs from one payload."""

        refs, _paths, _hashes = self._extract_references(payload)
        return refs

    def _scan_blobs(self) -> dict[str, _BlobRecord]:
        blobs: dict[str, _BlobRecord] = {}
        if not self.content_root.is_dir():
            return blobs
        for path in sorted(self.content_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.workspace).as_posix()
            # ``.staging`` and ``verified`` hold temporary/handle metadata,
            # never canonical content blobs.  Accept both the normal sharded
            # layout and a flat fixture layout used by offline audits.
            if ".staging" in path.parts or "verified" in path.parts:
                continue
            if not _SHA256.fullmatch(path.name):
                continue
            digest, size = self._digest(path)
            stat_result = path.stat()
            blobs[relative] = _BlobRecord(
                ref=relative,
                path=path,
                digest=digest,
                logical_bytes=size,
                allocated_bytes=self._allocated_bytes(path),
                mtime=stat_result.st_mtime,
                mtime_ns=stat_result.st_mtime_ns,
            )
        return blobs

    def _manifest_paths(self) -> list[Path]:
        """Return known lifecycle manifests without reading generated plans."""

        roots = (
            self.workspace / "Work" / "report-versions",
            self.workspace / "Work" / "runs",
            self.workspace / "Work" / "delivery",
            self.workspace / "Work" / "deliveries",
            self.workspace / "Work" / "report-delivery",
            self.workspace / "Work" / "report-template-writing",
            self.workspace / "Work" / "content",
            self.workspace / "Work" / "content" / "verified",
            self.workspace / "Outputs",
            self.workspace / "Work" / "Outputs",
        )
        paths: set[Path] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                if path.suffix.lower() not in {".json", ".jsonl"}:
                    continue
                if self._relative(path) in _GENERATED_SUMMARIES:
                    continue
                paths.add(path)

        # A few historical workspaces placed storage/template/delivery
        # manifests outside the standard roots.  Restrict this supplemental
        # scan by filename so ordinary workflow JSON is not mistaken for a
        # manifest.
        work_root = self.workspace / "Work"
        if work_root.is_dir():
            for path in work_root.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                name = path.name.lower()
                if path.suffix.lower() in {".json", ".jsonl"} and any(
                    word in name
                    for word in (
                        "manifest",
                        "receipt",
                        "version",
                        "template",
                        "session",
                        "conversation",
                        "storage",
                        "active",
                        "pinned",
                        "marker",
                    )
                ):
                    if self._relative(path) not in _GENERATED_SUMMARIES:
                        paths.add(path)
        return sorted(paths)

    def _view_roots(self) -> tuple[Path, ...]:
        return (
            self.workspace / "Work",
            self.workspace / "Outputs",
        )

    def _regular_files(self, root: Path) -> Iterable[Path]:
        if not root.is_dir():
            return ()
        paths: list[Path] = []
        try:
            iterator = root.rglob("*")
            for path in iterator:
                if path.is_dir():
                    continue
                # Use the lexical path here.  Resolving a CAS compatibility
                # symlink would make the view appear to be inside
                # ``Work/content`` and silently hide it from the view scan.
                if self._is_lexically_under(
                    path, self.workspace / "Work" / "content"
                ):
                    continue
                if path.is_file() or path.is_symlink():
                    paths.append(path)
        except OSError:
            return ()
        return paths

    def _read_manifest_payloads(self, path: Path) -> list[Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        if not text.strip():
            return []
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            if path.suffix.lower() != ".jsonl":
                return []
            payloads: list[Any] = []
            for line in text.splitlines():
                try:
                    payloads.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return payloads

    def _extract_references(
        self,
        payload: Any,
    ) -> tuple[set[str], list[_PathReference], set[str]]:
        refs: set[str] = set()
        path_refs: list[_PathReference] = []
        hashes: set[str] = set()

        def visit(
            value: Any,
            key: str = "",
            parent_keys: tuple[str, ...] = (),
        ) -> None:
            key_context = (key, *parent_keys)
            lowered = " ".join(part.lower() for part in key_context)
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(child, str(child_key), key_context)
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, key, key_context)
                return
            if not isinstance(value, str):
                return
            raw = value.strip()
            canonical = self._normalise_cas_ref(raw)
            if canonical is not None:
                refs.add(canonical)
                return
            if _SHA256.fullmatch(raw.lower()) and any(
                word in lowered for word in _HASH_KEY_WORDS
            ):
                hashes.add(raw.lower())
                return
            if self._looks_like_path_value(raw, lowered):
                candidate = self._workspace_path(raw)
                if candidate is not None:
                    path_refs.append(_PathReference(candidate, key))

        visit(payload)
        return refs, path_refs, hashes

    def _register_path_reference(
        self,
        path_ref: _PathReference,
        source: Path,
        blobs: dict[str, _BlobRecord],
        references: dict[str, set[str]],
        all_reference_sources: dict[str, set[str]],
        materialized_paths: set[Path],
        view_paths: dict[Path, str],
    ) -> str | None:
        path = path_ref.path
        try:
            if path.is_symlink():
                ref = self._cas_ref_from_path(path, blobs)
                if ref is not None:
                    view_paths.setdefault(path, ref)
                    self._register_reference(
                        ref,
                        path,
                        blobs,
                        references,
                        all_reference_sources,
                    )
                    return ref
                return None
            if not path.is_file():
                return None
            ref = self._cas_ref_from_path(path, blobs)
            if ref is not None:
                self._register_reference(
                    ref,
                    source,
                    blobs,
                    references,
                    all_reference_sources,
                )
                return ref
            if self._is_under(path, self.workspace / "Work" / "content"):
                return None
            # Do not turn a receipt/manifest path into a materialized report
            # artifact.  Referenced report-state/session/module paths remain
            # eligible for this metric.
            if not self._is_metadata_path(path):
                materialized_paths.add(path)
            digest, _size = self._digest(path)
            ref = self._blob_ref_for_digest(digest, blobs)
            if ref is not None and self._looks_like_view_path(path):
                view_paths.setdefault(path, ref)
                materialized_paths.discard(path)
                self._register_reference(
                    ref,
                    path,
                    blobs,
                    references,
                    all_reference_sources,
                )
                return ref
            return None
        except OSError:
            return None

    def _scan_protection_markers(
        self,
        *,
        blobs: dict[str, _BlobRecord],
        manifest_refs: dict[Path, set[str]],
        active_refs: set[str],
        all_reference_sources: dict[str, set[str]],
    ) -> None:
        marker_paths: set[Path] = set()
        roots = (self.workspace / "Work", self.workspace / "Outputs")
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if path.is_symlink():
                    continue
                lower = path.name.lower()
                if (
                    lower in {".active", ".pinned", ".keep", ".retain"}
                    or any(word in lower for word in ("active", "pinned"))
                    or lower.endswith(".marker")
                    or lower in {"workflow-state.json", "status.json", "state.json"}
                ):
                    if path.is_file() or path.is_dir():
                        marker_paths.add(path)

        for marker in sorted(marker_paths):
            source = self._relative(marker)
            payloads = (
                self._read_manifest_payloads(marker) if marker.is_file() else []
            )
            direct_refs: set[str] = set()
            marker_active = (
                marker.name.lower() in {".active", ".pinned", ".keep", ".retain"}
                or any(word in marker.name.lower() for word in ("active", "pinned"))
                or marker.name.lower().endswith(".marker")
            )
            for payload in payloads:
                refs, _paths, hashes = self._extract_references(payload)
                direct_refs.update(refs)
                direct_refs.update(
                    resolved
                    for raw_hash in hashes
                    if (resolved := self._blob_ref_for_digest(raw_hash, blobs))
                    is not None
                )
                marker_active = marker_active or self._payload_is_protected(payload)
                marker_active = marker_active or self._payload_is_active(payload)
            # A marker filename is itself enough to protect its scope.  If a
            # JSON marker explicitly says inactive=false, do not infer scope.
            if payloads and all(
                isinstance(payload, dict)
                and any(key in payload for key in ("active", "pinned", "protected"))
                and not self._payload_is_protected(payload)
                for payload in payloads
            ):
                marker_active = False
            if not marker_active:
                continue

            scoped_refs = set(direct_refs)
            # A dot marker is commonly a file/directory beside a run.  A
            # named ``active``/``pinned`` directory is itself the protected
            # scope, while ``.active``/``.pinned`` protects its parent run.
            scope = (
                marker.parent
                if marker.name.startswith(".")
                else marker if marker.is_dir() else marker.parent
            )
            for manifest, refs in manifest_refs.items():
                if self._is_under(manifest, scope):
                    scoped_refs.update(refs)
            for ref in scoped_refs:
                if ref not in blobs:
                    continue
                active_refs.add(ref)
                all_reference_sources[ref].add(source)

    def _register_reference(
        self,
        ref: str,
        source: Path,
        blobs: dict[str, _BlobRecord],
        references: dict[str, set[str]],
        all_reference_sources: dict[str, set[str]],
    ) -> None:
        if ref not in blobs:
            return
        name = source if isinstance(source, str) else self._relative(source)
        references[ref].add(name)
        all_reference_sources[ref].add(name)

    def _cas_ref_from_path(
        self,
        path: Path,
        blobs: dict[str, _BlobRecord],
    ) -> str | None:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not self._is_under(resolved, self.content_root) or not resolved.is_file():
            return None
        ref = self._relative(resolved)
        return ref if ref in blobs else None

    def _cas_ref_by_identity(
        self,
        path: Path,
        blobs: dict[str, _BlobRecord],
    ) -> str | None:
        try:
            stat_result = path.stat()
        except OSError:
            return None
        for ref, record in blobs.items():
            try:
                canonical_stat = record.path.stat()
            except OSError:
                continue
            if (
                stat_result.st_dev == canonical_stat.st_dev
                and stat_result.st_ino == canonical_stat.st_ino
            ):
                return ref
        digest, _size = self._digest(path)
        return self._blob_ref_for_digest(digest, blobs)

    def _blob_ref_for_digest(
        self,
        digest: str,
        blobs: dict[str, _BlobRecord],
    ) -> str | None:
        for ref, record in blobs.items():
            if record.digest == digest:
                return ref
        return None

    def _normalise_cas_ref(self, raw: str) -> str | None:
        value = raw.replace("\\", "/")
        if value.startswith("./"):
            value = value[2:]
        if value.startswith("content/sha256/"):
            value = "Work/" + value
        if not value.startswith(_CAS_PREFIX):
            return None
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            return None
        return path.as_posix()

    def _workspace_path(self, raw: str) -> Path | None:
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                return None
            return resolved if self._is_under(resolved, self.workspace) else None
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        value = candidate.as_posix()
        if not (
            value.startswith(("Work/", "Outputs/", "Inputs/", "Knowledge/", "Templates/"))
            or value.startswith(".manyselves/")
        ):
            return None
        resolved = (self.workspace / candidate).resolve(strict=False)
        return resolved if self._is_under(resolved, self.workspace) else None

    @staticmethod
    def _looks_like_path_value(value: str, key: str) -> bool:
        if value.startswith(("Work/", "Outputs/", "Inputs/", "Knowledge/", "Templates/")):
            return True
        if "/" in value or "\\" in value:
            return any(word in key for word in _REFERENCE_KEY_WORDS)
        return any(word in key for word in _REFERENCE_KEY_WORDS) and (
            value.endswith(('.json', '.jsonl', '.md', '.docx', '.xlsx', '.png', '.jpg', '.bin'))
        )

    def _payload_is_protected(self, payload: Any) -> bool:
        if isinstance(payload, dict):
            for key, value in payload.items():
                lowered = str(key).lower()
                if lowered in {"active", "pinned", "protected", "keep"}:
                    if value is True or str(value).lower() in {"true", "active", "pinned", "keep"}:
                        return True
                if self._payload_is_protected(value):
                    return True
        elif isinstance(payload, list):
            return any(self._payload_is_protected(value) for value in payload)
        return False

    def _payload_is_active(self, payload: Any) -> bool:
        """Recognize an active/running workflow state marker."""

        if isinstance(payload, dict):
            for key, value in payload.items():
                lowered = str(key).lower()
                if lowered in {"status", "state", "activity", "lifecycle"}:
                    if str(value).lower() in {
                        "active",
                        "running",
                        "in_progress",
                        "in-progress",
                        "pending",
                    }:
                        return True
                if self._payload_is_active(value):
                    return True
        elif isinstance(payload, list):
            return any(self._payload_is_active(value) for value in payload)
        return False

    def _is_metadata_path(self, path: Path) -> bool:
        lower = path.name.lower()
        return (
            any(word in lower for word in _MANIFEST_WORDS)
            or lower in {"version.json", "latest.json", "workflow-state.json"}
            or lower.endswith((".marker", ".lock"))
        )

    def _looks_like_view_path(self, path: Path) -> bool:
        rel = self._relative(path).lower()
        return rel.startswith((
            "outputs/",
            "work/outputs/",
            "work/report-versions/",
            "work/runs/",
        ))

    def _is_visible_output(self, path: Path) -> bool:
        return self._is_under(path, self.workspace / "Outputs") or self._is_under(
            path, self.workspace / "Work" / "Outputs"
        )

    def _normalise_now(self, now: datetime | None) -> datetime:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve(strict=False).relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
        except (OSError, RuntimeError):
            return False

    @staticmethod
    def _is_lexically_under(path: Path, root: Path) -> bool:
        try:
            Path(path).relative_to(Path(root))
            return True
        except ValueError:
            return False

    def _digest(self, path: Path) -> tuple[str, int]:
        cached = self._digest_cache.get(path)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            return "", 0
        result = (digest.hexdigest(), size)
        self._digest_cache[path] = result
        return result

    @staticmethod
    def _allocated_bytes(path: Path) -> int:
        try:
            stat_result = path.stat()
        except OSError:
            return 0
        blocks = int(getattr(stat_result, "st_blocks", 0) or 0)
        # st_blocks is 512-byte units on POSIX.  For filesystems that do not
        # expose it, logical size is the least surprising allocation estimate.
        return blocks * 512 if blocks else stat_result.st_size


__all__ = ["ReportingRetentionPlanner", "RetentionPlan"]
