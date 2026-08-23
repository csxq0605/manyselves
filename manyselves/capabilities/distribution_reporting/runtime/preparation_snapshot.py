"""Canonical persistence for the distribution-reporting preparation context.

The preparation workflow publishes a small set of typed, run-local files.  A
completion record points at those files and is published together with them by
renaming one staging directory.  The record is deliberately a reference and
count summary: it does not duplicate the old workflow checkpoint or introduce
content hashes, CAS pointers, locks, or recovery gates.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import cast
from uuid import uuid4

from .models.preparation import PreparationContext, ProjectManifest
from .models.reporting import CoverageMatrix, EvidenceItem, PhotoAsset, SpecialTopicPlan
from .storage import ReportingStore

_SCHEMA_VERSION = 1


def preparation_refs(
    run_id: str,
    *,
    include_special_topic: bool = False,
) -> dict[str, str]:
    """Return the canonical relative refs for one run's preparation files."""

    root = f"Work/runs/{run_id}/preparation"
    refs = {
        "manifest": f"{root}/manifest.json",
        "evidence": f"{root}/evidence.jsonl",
        "photo_manifest": f"{root}/photo-manifest.json",
        "photo_adjacency": f"{root}/photo-evidence-adjacency.json",
        "mapping_gaps": f"{root}/mapping-gaps.json",
        "coverage": f"{root}/coverage.json",
        "report_taxonomy": f"{root}/report-taxonomy.json",
    }
    if include_special_topic:
        refs["special_topic_plan"] = f"{root}/special-topic-plan.json"
    return refs


def preparation_completion_ref(run_id: str) -> str:
    """Return the canonical completion reference for one run."""

    return f"Work/runs/{run_id}/preparation/completion.json"


class PreparationSnapshotStore:
    """Persist and restore typed preparation files through ``ReportingStore``."""

    def __init__(self, store: ReportingStore | Path):
        self.store = store if isinstance(store, ReportingStore) else ReportingStore(store)

    @property
    def workspace(self) -> Path:
        return self.store.workspace

    def persist(self, context: PreparationContext) -> PreparationContext:
        """Publish ``context`` as one canonical same-run preparation snapshot."""

        include_special_topic = context.special_topic_plan is not None
        refs = preparation_refs(
            context.run_id,
            include_special_topic=include_special_topic,
        )
        completion_ref = preparation_completion_ref(context.run_id)
        run_root = self.workspace / "Work" / "runs" / context.run_id
        final_root = run_root / "preparation"
        staging_root = run_root / f".preparation-{uuid4().hex}"
        staging_preparation = staging_root / "preparation"
        staging_preparation.mkdir(parents=True, exist_ok=False)

        def stage_ref(name: str) -> str:
            return (
                staging_preparation / Path(refs[name]).name
            ).relative_to(self.workspace).as_posix()

        try:
            manifest = cast(ProjectManifest, context.project_manifest)
            coverage = cast(CoverageMatrix, context.coverage_matrix)

            self.store.write_json(
                stage_ref("manifest"),
                manifest.model_dump(mode="json"),
            )
            self.store.write_jsonl(
                stage_ref("evidence"),
                [item.model_dump(mode="json") for item in context.evidence_items],
            )
            self.store.write_json(
                stage_ref("photo_manifest"),
                {
                    "assets": [
                        item.model_dump(mode="json")
                        for item in context.photo_assets
                    ]
                },
            )
            self.store.write_json(
                stage_ref("photo_adjacency"),
                context.photo_evidence_adjacency,
            )
            self.store.write_json(
                stage_ref("mapping_gaps"),
                {"gaps": context.mapping_gaps},
            )
            self.store.write_json(
                stage_ref("coverage"),
                coverage.model_dump(mode="json"),
            )
            self.store.write_json(
                stage_ref("report_taxonomy"),
                context.report_taxonomy,
            )
            if include_special_topic:
                self.store.write_json(
                    stage_ref("special_topic_plan"),
                    context.special_topic_plan.model_dump(mode="json")
                    if context.special_topic_plan is not None
                    else {},
                )

            manifest_order = [item.id for item in manifest.files]
            reducer_order = context.preparation_parallelism.get(
                "reducer_order",
                manifest_order,
            )
            completion = {
                "schema_version": _SCHEMA_VERSION,
                "run_id": context.run_id,
                "status": "completed",
                "input_snapshot_ref": context.input_snapshot_ref,
                "input_snapshot_digest": context.input_snapshot_digest,
                "preparation_refs": refs,
                "manifest_order": manifest_order,
                "reducer_order": list(reducer_order),
                "evidence_count": len(context.evidence_items),
                "photo_count": len(context.photo_assets),
            }
            self.store.write_json(
                (staging_preparation / "completion.json")
                .relative_to(self.workspace)
                .as_posix(),
                completion,
            )
            os.replace(staging_preparation, final_root)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

        return context.model_copy(
            update={
                "preparation_refs": refs,
                "preparation_completion_ref": completion_ref,
            }
        )

    def restore(self, context: PreparationContext) -> PreparationContext:
        """Restore a context from its canonical completion and typed files."""

        completion_ref = preparation_completion_ref(context.run_id)
        completion_path = self.workspace / completion_ref
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        declared_refs = dict(completion["preparation_refs"])
        refs = preparation_refs(
            context.run_id,
            include_special_topic="special_topic_plan" in declared_refs,
        )
        manifest = ProjectManifest.model_validate_json(
            (self.workspace / refs["manifest"]).read_text(encoding="utf-8")
        )
        evidence = [
            EvidenceItem.model_validate_json(line)
            for line in (self.workspace / refs["evidence"])
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        photo_payload = json.loads(
            (self.workspace / refs["photo_manifest"]).read_text(encoding="utf-8")
        )
        photos = [PhotoAsset.model_validate(item) for item in photo_payload.get("assets", [])]
        adjacency = json.loads(
            (self.workspace / refs["photo_adjacency"]).read_text(encoding="utf-8")
        )
        gaps = json.loads(
            (self.workspace / refs["mapping_gaps"]).read_text(encoding="utf-8")
        ).get("gaps", [])
        coverage = CoverageMatrix.model_validate_json(
            (self.workspace / refs["coverage"]).read_text(encoding="utf-8")
        )
        taxonomy = json.loads(
            (self.workspace / refs["report_taxonomy"]).read_text(encoding="utf-8")
        )
        special_topic = None
        if "special_topic_plan" in refs:
            special_topic = SpecialTopicPlan.model_validate_json(
                (self.workspace / refs["special_topic_plan"]).read_text(
                    encoding="utf-8"
                )
            )

        manifest_order = completion.get("manifest_order", [])
        reducer_order = completion.get("reducer_order", manifest_order)
        return context.model_copy(
            update={
                "input_snapshot_ref": completion.get("input_snapshot_ref"),
                "input_snapshot_digest": completion.get("input_snapshot_digest"),
                "project_manifest": manifest,
                "preparation_worker_results": [],
                "parsed_artifacts": [],
                "preparation_parallelism": {"reducer_order": list(reducer_order)},
                "evidence_items": evidence,
                "photo_assets": photos,
                "photo_evidence_adjacency": adjacency,
                "mapping_gaps": gaps,
                "coverage_matrix": coverage,
                "report_taxonomy": taxonomy,
                "special_topic_plan": special_topic,
                "preparation_refs": refs,
                "preparation_completion_ref": completion_ref,
            }
        )


def persist_preparation_snapshot(
    context: PreparationContext,
    store: ReportingStore | Path,
) -> PreparationContext:
    """Convenience wrapper for publishing one preparation context."""

    return PreparationSnapshotStore(store).persist(context)


def restore_preparation_snapshot(
    context: PreparationContext,
    store: ReportingStore | Path,
) -> PreparationContext:
    """Convenience wrapper for restoring one preparation context."""

    return PreparationSnapshotStore(store).restore(context)


__all__ = [
    "PreparationSnapshotStore",
    "persist_preparation_snapshot",
    "preparation_completion_ref",
    "preparation_refs",
    "restore_preparation_snapshot",
]
