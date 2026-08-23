"""Capability-owned deterministic tools for Reporting preparation.

The tools in this module are intentionally small transformations over a typed
``PreparationContext``.  The run-input snapshot, content view operation and
photo-id projection are supplied by the capability binding, so this module
does not own snapshot/CAS/locking lifecycle code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from manyselves.capabilities.distribution_reporting.domain.coverage import (
    evaluate_coverage,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    activate_report_taxonomy,
    parse_report_taxonomy_workbook,
)
from manyselves.capabilities.distribution_reporting.runtime.intake import (
    manifest as manifest_runtime,
)
from manyselves.capabilities.distribution_reporting.runtime.intake.special_topics import (
    load_special_topic_plan,
)
from manyselves.capabilities.distribution_reporting.runtime.intake.wps_images import (
    canonicalize_photo_bindings,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    FilePreparationResult,
    ManifestFile,
    PreparationContext,
    ProjectManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
)
from manyselves.capabilities.distribution_reporting.runtime.preparation import (
    prepare_manifest_file,
)
from manyselves.capabilities.distribution_reporting.runtime.preparation_snapshot import (
    PreparationSnapshotStore,
)
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import (
    ProjectEvidenceIndex,
    project_evidence_locator,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


class InputSnapshotLoader(Protocol):
    """Load the already-created input snapshot projection for one run."""

    def __call__(self, run_id: str) -> Any: ...


class SnapshotContent(Protocol):
    """Expose the binding's existing content-view operation."""

    def __call__(self, source: Path, target: Path) -> tuple[Path, str, Path]: ...


class RuntimePhotoIDs(Protocol):
    """Project evidence/photo bindings through the capability asset contract."""

    def __call__(self, evidence: list[EvidenceItem], photos: list[PhotoAsset]) -> Any: ...


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class PreparationTools:
    """Fine-grained, file-defined preparation Tool implementations."""

    def __init__(
        self,
        *,
        workspace: Path,
        input_snapshot: InputSnapshotLoader | Any,
        snapshot_content: SnapshotContent,
        runtime_photo_ids: RuntimePhotoIDs,
        store: ReportingStore,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.input_snapshot = input_snapshot
        self.snapshot_content = snapshot_content
        self.runtime_photo_ids = runtime_photo_ids
        self.store = store

    def build_manifest(self, context: PreparationContext) -> PreparationContext:
        """Project the frozen ``Inputs`` inventory into the typed manifest."""

        snapshot = self._load_snapshot(context.run_id)
        files: list[ManifestFile] = []
        for frozen in _field(snapshot, "files", ()):
            logical_ref = Path(_field(frozen, "logical_ref"))
            if not logical_ref.parts or logical_ref.parts[0] != "Inputs":
                continue
            media_type = manifest_runtime._INPUT_MEDIA_TYPES.get(
                logical_ref.suffix.casefold()
            )
            if media_type is None:
                continue
            digest = str(_field(frozen, "sha256"))
            files.append(
                ManifestFile(
                    id=manifest_runtime._stable_file_id(logical_ref, digest),
                    path=logical_ref,
                    sha256=digest,
                    media_type=media_type,
                    purpose=manifest_runtime._purpose_for(logical_ref),
                    snapshot_ref=(
                        Path(_field(frozen, "snapshot_ref"))
                        if _field(frozen, "snapshot_ref") is not None
                        else None
                    ),
                )
            )
        context.input_snapshot_ref = (
            f"Work/runs/{context.run_id}/input-snapshot.json"
        )
        context.input_snapshot_digest = _field(snapshot, "inventory_digest")
        context.project_manifest = ProjectManifest(files=files)
        self.store.write_json(
            "Work/manifest.json",
            context.project_manifest.model_dump(mode="json"),
        )
        return context

    def prepare_report_taxonomy(
        self,
        context: PreparationContext,
    ) -> PreparationContext:
        """Bind the taxonomy parsed from the current run's frozen S4-6 file."""

        manifest = self._require_manifest(context)
        sources = [item for item in manifest.files if item.purpose == "s4-6"]
        if len(sources) > 1:
            raise ValueError(
                "report taxonomy requires exactly one S4-6 workbook; "
                f"found={[item.path.as_posix() for item in sources]}"
            )
        if not sources:
            raise ValueError(
                "report taxonomy requires the current run's S4-6 workbook snapshot"
            )
        source = sources[0]
        source_ref = (source.snapshot_ref or source.path).as_posix()
        payload = parse_report_taxonomy_workbook(
            self.workspace / source_ref,
            source_ref=source_ref,
            source_sha256=source.sha256,
        )
        context.report_taxonomy = payload
        # Coverage and downstream domain helpers read the run-local taxonomy
        # through this existing capability context.  The serial payload remains
        # the durable value carried by PreparationContext.
        activate_report_taxonomy(payload)
        return context

    async def parse_artifacts(
        self,
        context: PreparationContext,
    ) -> PreparationContext:
        """Parse each manifest file into ordered, capability-owned worker results."""

        manifest = self._require_manifest(context)
        indexed_files = list(enumerate(manifest.files))
        request = context.request

        async def run_one(order: int, manifest_file: ManifestFile) -> FilePreparationResult:
            return await asyncio.to_thread(
                prepare_manifest_file,
                self.workspace,
                context.run_id,
                manifest_file,
                order,
            )

        if request.preparation_mode == "deterministic_workers" and len(indexed_files) > 1:
            semaphore = asyncio.Semaphore(
                min(request.preparation_concurrency, len(indexed_files))
            )

            async def bounded(
                order: int, manifest_file: ManifestFile
            ) -> FilePreparationResult:
                async with semaphore:
                    return await run_one(order, manifest_file)

            results = await asyncio.gather(
                *(bounded(order, manifest_file) for order, manifest_file in indexed_files)
            )
        else:
            results = [
                prepare_manifest_file(
                    self.workspace,
                    context.run_id,
                    manifest_file,
                    order,
                )
                for order, manifest_file in indexed_files
            ]

        results = sorted(results, key=lambda item: item.manifest_order)
        if [item.manifest_order for item in results] != list(range(len(manifest.files))):
            raise RuntimeError("preparation worker results are not a complete manifest order")
        by_id = {item.file_id: item for item in results}
        if len(by_id) != len(results):
            raise RuntimeError("preparation worker returned duplicate file identity")
        for manifest_file in manifest.files:
            result = by_id.get(manifest_file.id)
            if result is None or result.source_sha256 != manifest_file.sha256:
                raise RuntimeError("preparation worker source identity mismatch")
            manifest_file.parse_status = result.status
            manifest_file.error = result.error
            self.store.write_run_model(
                context.run_id,
                f"preparation-workers/results/{result.manifest_order:04d}-"
                f"{result.file_id}.json",
                result,
            )
        context.preparation_worker_results = results
        context.parsed_artifacts = [
            artifact
            for result in results
            for artifact in result.parsed_artifacts
        ]
        context.preparation_parallelism = {
            "mode": request.preparation_mode,
            "worker_count": (
                min(request.preparation_concurrency, len(indexed_files))
                if request.preparation_mode == "deterministic_workers"
                else 1
            ),
            "file_count": len(indexed_files),
            "reducer_order": [item.file_id for item in results],
        }
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))
        return context

    def normalize_evidence(self, context: PreparationContext) -> PreparationContext:
        """Reduce worker results and generic parsed artifacts to evidence.

        The worker-result list is the only mapper path.  In particular, this
        method deliberately has no direct mapper fallback for older callers.
        """

        manifest = self._require_manifest(context)
        evidence: list[EvidenceItem] = []
        photo_assets: list[PhotoAsset] = []
        mapping_gaps: list[dict[str, Any]] = []
        for result in sorted(
            context.preparation_worker_results,
            key=lambda item: item.manifest_order,
        ):
            if result.status != "parsed":
                continue
            mapped_evidence = list(result.provisional_evidence)
            if any(item.photo_refs for item in mapped_evidence):
                mapped_evidence, normalized_assets = canonicalize_photo_bindings(
                    mapped_evidence,
                    result.raw_photo_assets,
                    start_index=len(photo_assets) + 1,
                )
                final_asset_root = (
                    self.workspace
                    / "Work"
                    / "runs"
                    / context.run_id
                    / "assets"
                    / result.file_id
                )
                for asset in normalized_assets:
                    final_path = final_asset_root / asset.path.name
                    final_path, _sha256, _blob_ref = self.snapshot_content(
                        asset.path,
                        final_path,
                    )
                    photo_assets.append(
                        asset.model_copy(
                            update={
                                "path": final_path.relative_to(self.workspace),
                            }
                        )
                    )
            evidence.extend(mapped_evidence)
            mapping_gaps.extend(
                {"file_id": result.file_id, **gap}
                for gap in result.mapping_gaps
            )

        for artifact in context.parsed_artifacts:
            if artifact.kind == "manual_required":
                mapping_gaps.append(
                    {
                        "file_id": artifact.source.file_id,
                        "kind": "manual_required",
                        **artifact.payload,
                    }
                )
                continue
            if artifact.kind == "workbook_row":
                pairs = [
                    f"{header}={value}"
                    for header, value in zip(
                        artifact.payload["headers"],
                        artifact.payload["values"],
                        strict=False,
                    )
                    if value not in (None, "")
                ]
                if not pairs:
                    continue
                subject = str(artifact.payload["values"][0])
                fact = "; ".join(pairs)
            elif artifact.kind in {"text", "document_paragraph", "pdf_page"}:
                fact = str(artifact.payload.get("text", "")).strip()
                if not fact:
                    continue
                subject = artifact.source.path.name
            elif artifact.kind == "image_metadata":
                subject = artifact.source.path.name
                fact = (
                    f"图片元数据：{artifact.payload['width']}x{artifact.payload['height']}，"
                    f"格式={artifact.payload['format']}，模式={artifact.payload['mode']}"
                )
            else:
                continue
            evidence.append(
                EvidenceItem(
                    id=f"ev-{len(evidence) + 1:04d}",
                    subject=subject,
                    fact=fact,
                    source=artifact.source,
                )
            )

        evidence_id_map: dict[str, str] = {}
        normalized_evidence: list[EvidenceItem] = []
        for index, item in enumerate(evidence, start=1):
            if item.id in evidence_id_map:
                raise ValueError(f"duplicate pre-normalization evidence id: {item.id}")
            normalized_id = f"E-{index:04d}"
            evidence_id_map[item.id] = normalized_id
            normalized_evidence.append(item.model_copy(update={"id": normalized_id}))
        evidence = normalized_evidence

        normalized_photo_assets: list[PhotoAsset] = []
        for asset in photo_assets:
            primary_evidence_id = asset.primary_evidence_id
            if primary_evidence_id is None:
                raise ValueError(f"photo {asset.id} is missing its primary evidence binding")
            normalized_primary_id = evidence_id_map.get(primary_evidence_id)
            if normalized_primary_id is None:
                raise ValueError(
                    f"photo {asset.id} references unknown primary evidence "
                    f"{primary_evidence_id}"
                )
            normalized_photo_assets.append(
                asset.model_copy(update={"primary_evidence_id": normalized_primary_id})
            )
        photo_assets = normalized_photo_assets
        self.runtime_photo_ids(evidence, photo_assets)
        photo_to_evidence: dict[str, list[str]] = {
            asset.id: [] for asset in photo_assets
        }
        evidence_to_photo: dict[str, list[str]] = {}
        for item in evidence:
            evidence_to_photo[item.id] = list(item.photo_refs)
            for photo_id in item.photo_refs:
                photo_to_evidence.setdefault(photo_id, []).append(item.id)
        photo_adjacency = {
            "schema_version": 1,
            "photo_to_evidence": photo_to_evidence,
            "evidence_to_photo": evidence_to_photo,
            "primary_evidence": {
                asset.id: asset.primary_evidence_id for asset in photo_assets
            },
        }
        context.evidence_items = evidence
        context.photo_assets = photo_assets
        context.photo_evidence_adjacency = photo_adjacency
        context.mapping_gaps = mapping_gaps
        self.store.write_jsonl(
            "Work/evidence.jsonl",
            [item.model_dump(mode="json") for item in evidence],
        )
        self.store.write_json(
            "Work/photo-manifest.json",
            {"assets": [asset.model_dump(mode="json") for asset in photo_assets]},
        )
        self.store.write_json("Work/photo-evidence-adjacency.json", photo_adjacency)
        self.store.write_json("Work/mapping-gaps.json", {"gaps": mapping_gaps})
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))
        return context

    def evaluate_coverage(self, context: PreparationContext) -> PreparationContext:
        """Evaluate coverage from normalized evidence without readiness policy."""

        coverage = evaluate_coverage(
            context.request,
            context.evidence_items,
            mapping_gaps=context.mapping_gaps,
        )
        context.coverage_matrix = coverage
        self.store.write_json("Work/coverage.json", coverage.model_dump(mode="json"))
        return context

    def load_special_topic_plan(self, context: PreparationContext) -> PreparationContext:
        """Load the optional Chapter 4 plan for full-report preparation only."""

        if context.request.operation == "full_report":
            context.special_topic_plan = load_special_topic_plan(self.workspace)
        else:
            context.special_topic_plan = None
        return context

    def finalize_preparation(self, context: PreparationContext) -> PreparationContext:
        """Materialize the normalized evidence index and source ledger after Persist."""

        evidence_index = ProjectEvidenceIndex(self.workspace, context.run_id)
        evidence_index.items()
        evidence_index_ref = evidence_index.snapshot_manifest_ref()
        if evidence_index_ref is not None:
            evidence_ref = evidence_index_ref.as_posix()
            context.evidence_index_ref = evidence_ref
            context.preparation_refs = {
                **context.preparation_refs,
                "evidence_index": evidence_ref,
            }

        ledger = SourceLedger(self.workspace, context.run_id)
        ledger.register_many(
            [
                {
                    "kind": "project_evidence",
                    "evidence_id": item.id,
                    "title": item.subject,
                    "locator": project_evidence_locator(item),
                    "content": item.model_dump_json(),
                }
                for item in context.evidence_items
            ]
        )
        if not ledger.path.is_file():
            self.store.write_json(
                ledger.path.relative_to(self.workspace).as_posix(),
                [],
            )
        source_ledger_ref = ledger.path.relative_to(self.workspace).as_posix()
        context.source_ledger_ref = source_ledger_ref
        context.preparation_refs = {
            **context.preparation_refs,
            "source_ledger": source_ledger_ref,
        }
        return context

    def _load_snapshot(self, run_id: str) -> Any:
        if callable(self.input_snapshot):
            return self.input_snapshot(run_id)
        return self.input_snapshot

    @staticmethod
    def _require_manifest(context: PreparationContext) -> ProjectManifest:
        if context.project_manifest is None:
            raise ValueError("preparation context has no project manifest")
        return context.project_manifest


def build_preparation_tools(
    *,
    workspace: Path,
    input_snapshot: InputSnapshotLoader | Any,
    snapshot_content: SnapshotContent,
    runtime_photo_ids: RuntimePhotoIDs,
    store: ReportingStore,
) -> PreparationTools:
    """Construct the capability Tool implementation bundle."""

    return PreparationTools(
        workspace=workspace,
        input_snapshot=input_snapshot,
        snapshot_content=snapshot_content,
        runtime_photo_ids=runtime_photo_ids,
        store=store,
    )


def build_preparation_tool_implementations(
    *,
    workspace: Path,
    input_snapshot: InputSnapshotLoader | Any,
    snapshot_content: SnapshotContent,
    runtime_photo_ids: RuntimePhotoIDs,
    store: ReportingStore,
) -> dict[str, Any]:
    """Bind the nine file-declared preparation Tool implementation IDs."""

    tools = build_preparation_tools(
        workspace=workspace,
        input_snapshot=input_snapshot,
        snapshot_content=snapshot_content,
        runtime_photo_ids=runtime_photo_ids,
        store=store,
    )
    snapshots = PreparationSnapshotStore(store)
    return {
        "build-manifest": tools.build_manifest,
        "prepare-report-taxonomy": tools.prepare_report_taxonomy,
        "parse-artifacts": tools.parse_artifacts,
        "normalize-evidence": tools.normalize_evidence,
        "evaluate-coverage": tools.evaluate_coverage,
        "load-special-topic-plan": tools.load_special_topic_plan,
        "persist-preparation-snapshot": snapshots.persist,
        "finalize-preparation": tools.finalize_preparation,
        "restore-preparation-snapshot": snapshots.restore,
    }


__all__ = [
    "InputSnapshotLoader",
    "PreparationTools",
    "RuntimePhotoIDs",
    "SnapshotContent",
    "build_preparation_tool_implementations",
    "build_preparation_tools",
]
