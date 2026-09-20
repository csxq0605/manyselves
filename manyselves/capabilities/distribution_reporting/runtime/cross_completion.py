"""Capability-owned Cross owner lane completion construction."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossReviewFinding,
    ModuleSubmission,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    _CrossOwnerLaneResult,
)
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    ArtifactRef,
    CrossOwnerCompletion,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _artifact_ref(workspace: Path, ref: str) -> ArtifactRef:
    content = (workspace / ref).read_bytes()
    return ArtifactRef(
        ref=ref,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type="application/json",
    )


def _artifact_ref_value(value: ArtifactRef | str | None) -> str | None:
    if isinstance(value, ArtifactRef):
        return value.ref
    if isinstance(value, str) and value:
        return value
    return None


def _subject_revision(ref: str) -> int | None:
    match = re.search(r"-r([0-9]+)\.json$", ref)
    return int(match.group(1)) if match is not None else None


def _completion_business_key(completion: CrossOwnerCompletion) -> dict:
    """Project the old completion's stable business identity.

    The excluded fields are existing semantic-key, attempt, lease, and schema
    metadata.  They remain in the persisted format but do not participate in
    same-run completion reuse.
    """

    payload = completion.model_dump(mode="json")
    for field in (
        "owner_input",
        "initial_result",
        "verdict_result",
        "subject",
        "local_review_completion",
    ):
        payload[field] = _artifact_ref_value(getattr(completion, field))
    for field in (
        "semantic_key",
        "author_task_attempt_id",
        "reviewer_session_id",
        "lease_epoch",
        "schema_version",
    ):
        payload.pop(field, None)
    return payload


def _validate_existing_completion(
    workspace: Path,
    completion: CrossOwnerCompletion,
    *,
    run_id: str,
    review_round: int,
    owner_module_id: str,
    owner_input_ref: str | None,
) -> None:
    if (
        completion.run_id != run_id
        or completion.stage != "cross"
        or completion.module_id != owner_module_id
        or completion.review_round != review_round
        or completion.status != "completed"
        or completion.lane_id != f"cross-r{review_round}-module-{owner_module_id}"
    ):
        raise ValueError(
            f"Cross owner completion business identity mismatch: {owner_module_id}"
        )

    subject_ref = _artifact_ref_value(completion.subject)
    if subject_ref is None:
        raise ValueError(
            f"Cross owner completion has no subject artifact: {owner_module_id}"
        )
    try:
        subject = ModuleSubmission.model_validate_json(
            (workspace / subject_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Cross owner completion subject is invalid: {owner_module_id}"
        ) from exc
    if (
        subject.module_id != owner_module_id
        or _subject_revision(subject_ref) != subject.revision
        or (
            completion.subject_revision is not None
            and completion.subject_revision != subject.revision
        )
    ):
        raise ValueError(
            f"Cross owner completion subject identity mismatch: {owner_module_id}"
        )
    if owner_input_ref is not None and _artifact_ref_value(completion.owner_input) != owner_input_ref:
        raise ValueError(
            f"Cross owner completion input identity mismatch: {owner_module_id}"
        )

    local_review_ref = _artifact_ref_value(completion.local_review_completion)
    if local_review_ref is None:
        raise ValueError(
            f"Cross owner completion has no local review completion: {owner_module_id}"
        )
    try:
        local_completion = ReviewCompletionRecord.model_validate_json(
            (workspace / local_review_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Cross owner local review completion is not typed: {owner_module_id}"
        ) from exc
    if (
        local_completion.lifecycle != "module"
        or local_completion.run_id != run_id
        or local_completion.reviewer_agent_id != "evidence-auditor"
        or local_completion.reviewer_session_key != f"module-auditor-{owner_module_id}"
        or local_completion.subject_refs != [subject_ref]
    ):
        raise ValueError(
            f"Cross owner local review completion does not bind subject: {owner_module_id}"
        )


def build_cross_owner_lane_completion(
    *,
    workspace: Path,
    store: ReportingStore,
    run_id: str,
    review_round: int,
    owner_module_id: str,
    owner_input_ref: str | None,
    revised: ModuleSubmission,
    cross_responses: list[RevisionResponse],
    local_review_completion_ref: str,
    findings: list[CrossReviewFinding],
) -> _CrossOwnerLaneResult:
    """Persist or reuse the existing typed Cross owner lane completion."""

    workspace = Path(workspace)
    subject_ref = (
        f"Work/runs/{run_id}/modules/{owner_module_id}-r{revised.revision}.json"
    )
    semantic_payload = {
        "run_id": run_id,
        "review_round": review_round,
        "module_id": owner_module_id,
        "owner_input_ref": owner_input_ref,
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "schema_version": "1",
    }
    semantic_key = hashlib.sha256(
        json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    completion = CrossOwnerCompletion(
        lane_id=f"cross-r{review_round}-module-{owner_module_id}",
        run_id=run_id,
        review_round=review_round,
        module_id=owner_module_id,
        semantic_key=semantic_key,
        subject_revision=revised.revision,
        owner_input=(
            _artifact_ref(workspace, owner_input_ref)
            if owner_input_ref is not None
            else None
        ),
        subject=_artifact_ref(workspace, subject_ref),
        local_review_completion=_artifact_ref(
            workspace,
            local_review_completion_ref,
        ),
        author_task_attempt_id=f"cross-owner-attempt-{uuid4().hex}",
        reviewer_session_id=f"cross-owner-{owner_module_id}",
        lease_epoch=1,
    )
    completion_ref = (
        f"Work/runs/{run_id}/lanes/cross-r{review_round}/"
        f"module-{owner_module_id}/completion-r{revised.revision}.json"
    )
    completion_path = workspace / completion_ref
    if completion_path.is_file():
        try:
            existing = CrossOwnerCompletion.model_validate_json(
                completion_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Cross owner completion is invalid: {owner_module_id}"
            ) from exc
        _validate_existing_completion(
            workspace,
            existing,
            run_id=run_id,
            review_round=review_round,
            owner_module_id=owner_module_id,
            owner_input_ref=owner_input_ref,
        )
        if _completion_business_key(existing) != _completion_business_key(completion):
            raise ValueError(
                f"Cross owner completion changed for the same input: {owner_module_id}"
            )
        completion = existing
    else:
        store.write_json(completion_ref, completion.model_dump(mode="json"))
    return _CrossOwnerLaneResult(
        module=revised,
        responses=list(cross_responses),
        local_review_ref=local_review_completion_ref,
        completion_ref=completion_ref,
        completion=completion,
    )


def promote_cross_owner_pipeline_completion(
    *,
    workspace: Path,
    store: ReportingStore,
    lane: _CrossOwnerLaneResult,
    initial_result_ref: str,
    verdict_ref: str,
) -> _CrossOwnerLaneResult:
    """Promote a closed owner round to the existing outer r1 barrier record."""

    workspace = Path(workspace)
    completion = lane.completion.model_copy(
        update={
            "lane_id": f"cross-r1-module-{lane.completion.module_id}",
            "review_round": 1,
            "initial_result": _artifact_ref(workspace, initial_result_ref),
            "verdict_result": _artifact_ref(workspace, verdict_ref),
            "schema_version": "3",
        }
    )
    completion_ref = (
        f"Work/runs/{completion.run_id}/lanes/cross-r1/"
        f"module-{completion.module_id}/pipeline-completion.json"
    )
    completion_path = workspace / completion_ref
    if completion_path.is_file():
        try:
            existing = CrossOwnerCompletion.model_validate_json(
                completion_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Cross owner pipeline completion is invalid: {completion.module_id}"
            ) from exc
        _validate_existing_completion(
            workspace,
            existing,
            run_id=completion.run_id,
            review_round=1,
            owner_module_id=str(completion.module_id),
            owner_input_ref=_artifact_ref_value(completion.owner_input),
        )
        if _completion_business_key(existing) != _completion_business_key(completion):
            raise ValueError(
                f"Cross owner pipeline completion changed: {completion.module_id}"
            )
        completion = existing
    else:
        store.write_json(completion_ref, completion.model_dump(mode="json"))
    return lane.model_copy(
        update={"completion_ref": completion_ref, "completion": completion}
    )


__all__ = [
    "build_cross_owner_lane_completion",
    "promote_cross_owner_pipeline_completion",
]
