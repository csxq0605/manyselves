"""Capability-owned review artifact loading and final-audit binding."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .assets import validate_final_report_markdown
from .delivery_projection import build_delivery_projection
from .models.agentic import CrossSynthesisInput, EditedReportSubmission
from .models.inputs import (
    FinalAuditSnapshot,
    ReviewCompletionRecord,
    ValidationReport,
)
from .storage import ReportingStore

FINAL_REVIEW_COMPLETION_SESSION_KEYS = frozenset(
    {"chief-editor-auditor", "final-chapter-wave"}
)


def load_current_review_completion(
    *,
    workspace: Path,
    run_id: str,
    completion_ref: str,
    lifecycle: str,
    reviewer_agent_id: str,
    reviewer_session_key: str | set[str] | frozenset[str],
    subject_refs: list[str] | None = None,
) -> tuple[ReviewCompletionRecord, list[object]]:
    """Load one exact current-protocol completion and its referenced artifacts."""

    workspace = Path(workspace)
    run_prefix = f"Work/runs/{run_id}/"

    def read_ref(ref: str) -> tuple[dict, Path]:
        if not ref.startswith(run_prefix):
            raise ValueError(f"review ref is outside current run: {ref}")
        path = (workspace / ref).resolve()
        run_root = (workspace / f"Work/runs/{run_id}").resolve()
        if not path.is_relative_to(run_root) or not path.is_file():
            raise ValueError(f"review ref is not a readable current-run artifact: {ref}")
        return json.loads(path.read_text(encoding="utf-8")), path

    raw, _ = read_ref(completion_ref)
    completion = ReviewCompletionRecord.model_validate(raw)
    chapter_scoped_final = (
        lifecycle == "final"
        and completion.reviewer_session_key == "final-chapter-wave"
    )
    reviewer_session_matches = (
        completion.reviewer_session_key == reviewer_session_key
        if isinstance(reviewer_session_key, str)
        else completion.reviewer_session_key in reviewer_session_key
    )
    if (
        completion.lifecycle != lifecycle
        or completion.run_id != run_id
        or completion.reviewer_agent_id != reviewer_agent_id
        or not reviewer_session_matches
    ):
        raise ValueError("review completion identity does not match the active lifecycle")
    if subject_refs is not None and completion.subject_refs != subject_refs:
        raise ValueError("review completion subject refs do not match current subjects")
    for ref in completion.subject_refs:
        read_ref(ref)
    lifecycle_kinds = {
        "module": {
            "finding": "module_review_finding_submission",
            "verdict": "module_review_verdict_submission",
        },
        "cross": {
            "finding": "cross_review_finding_submission",
            "verdict": "cross_review_verdict_submission",
        },
        "final": {
            "finding": "final_review_finding_submission",
            "verdict": "final_review_verdict_submission",
        },
    }
    artifacts: list[object] = []
    findings_by_id: dict[str, dict] = {}
    regression_findings_by_id: dict[str, dict] = {}
    embedded_new_findings_by_id: dict[str, dict] = {}
    verdict_ids: set[str] = set()

    def artifact_values(
        payload: dict,
        *,
        field: str,
        id_field: str,
        ref: str,
    ) -> list[tuple[str, dict]]:
        values = payload.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"review completion artifact has non-list {field}: {ref}")
        identified = [
            (value.get(id_field), value) for value in values if isinstance(value, dict)
        ]
        if len(identified) != len(values) or any(
            not isinstance(value_id, str) or not value_id for value_id, _ in identified
        ):
            raise ValueError(f"review completion artifact has invalid {field} ids: {ref}")
        return identified

    expected_finding_kind = (
        "final_chapter_lane_finding_submission"
        if chapter_scoped_final
        else lifecycle_kinds[lifecycle]["finding"]
    )
    for index, ref in enumerate(completion.finding_refs):
        payload, _ = read_ref(ref)
        artifact_kind = str(payload.get("kind", ""))
        if artifact_kind != expected_finding_kind:
            raise ValueError(
                f"review completion finding ref has the wrong artifact kind: {ref}"
            )
        artifacts.append(payload)
        for finding_id, finding in artifact_values(
            payload,
            field="findings",
            id_field="id",
            ref=ref,
        ):
            if finding_id in findings_by_id:
                raise ValueError("review completion contains duplicate immutable finding ids")
            findings_by_id[finding_id] = finding
            if index > 0 and not chapter_scoped_final:
                regression_findings_by_id[finding_id] = finding

    expected_verdict_kind = (
        "final_chapter_lane_verdict_submission"
        if chapter_scoped_final
        else lifecycle_kinds[lifecycle]["verdict"]
    )
    for ref in completion.verdict_refs:
        payload, _ = read_ref(ref)
        artifact_kind = str(payload.get("kind", ""))
        if artifact_kind != expected_verdict_kind:
            raise ValueError(
                f"review completion verdict ref has the wrong artifact kind: {ref}"
            )
        artifacts.append(payload)
        verdict_ids.update(
            verdict_id
            for verdict_id, _ in artifact_values(
                payload,
                field="verdicts",
                id_field="finding_id",
                ref=ref,
            )
        )
        for finding_id, finding in artifact_values(
            payload,
            field="new_findings",
            id_field="id",
            ref=ref,
        ):
            if finding_id in embedded_new_findings_by_id:
                raise ValueError("review completion verdicts repeat a new immutable finding id")
            embedded_new_findings_by_id[finding_id] = finding
            if chapter_scoped_final:
                if finding_id in findings_by_id:
                    raise ValueError(
                        "review completion contains duplicate immutable finding ids"
                    )
                findings_by_id[finding_id] = finding

    if (
        not chapter_scoped_final
        and set(regression_findings_by_id) != set(embedded_new_findings_by_id)
    ):
        raise ValueError(
            "review completion regression finding refs do not match verdict new findings"
        )
    if (
        not chapter_scoped_final
        and regression_findings_by_id != embedded_new_findings_by_id
    ):
        raise ValueError(
            "review completion regression findings differ from verdict new findings"
        )

    finding_ids = set(findings_by_id)
    resolved_ids = set(completion.resolved_finding_ids)
    if resolved_ids != finding_ids:
        raise ValueError("review completion resolved ids do not equal all immutable findings")
    if not finding_ids.issubset(verdict_ids):
        raise ValueError("review completion lacks reviewer verdicts for findings")
    return completion, artifacts


def latest_cross_synthesis(artifacts: list[object]) -> list:
    """Return the latest Cross synthesis inputs from loaded artifacts."""

    synthesis = []
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("kind") in {
            "cross_review_finding_submission",
            "cross_review_verdict_submission",
        }:
            synthesis = [
                CrossSynthesisInput.model_validate(item)
                for item in artifact.get("synthesis_inputs", [])
            ]
    return synthesis


def latest_final_residual_risks(artifacts: list[object]) -> list[str]:
    """Return the latest Final residual risks from loaded artifacts."""

    residual_risks: list[str] = []
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("kind") in {
            "final_review_finding_submission",
            "final_review_verdict_submission",
            "final_chapter_lane_finding_submission",
            "final_chapter_lane_verdict_submission",
        }:
            values = artifact.get("residual_risks", [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise ValueError("final review residual_risks must be a string list")
            residual_risks = values
    return residual_risks


def validated_final_audit_subject(
    *,
    workspace: Path,
    store: ReportingStore,
    state: dict[str, Any],
    reviewer_session_key: str | set[str] | frozenset[str] = (
        FINAL_REVIEW_COMPLETION_SESSION_KEYS
    ),
) -> tuple[EditedReportSubmission, str]:
    """Load the exact audited subject and bind its canonical prose to delivery."""

    workspace = Path(workspace)
    run_id = state["run_id"]
    completion_ref = state["final_review_completion_ref"]
    completion, _ = load_current_review_completion(
        workspace=workspace,
        run_id=run_id,
        completion_ref=completion_ref,
        lifecycle="final",
        reviewer_agent_id="chief-editor-auditor",
        reviewer_session_key=reviewer_session_key,
    )
    if len(completion.subject_refs) != 1:
        raise ValueError("final audit completion must bind exactly one subject")
    subject_ref = completion.subject_refs[0]
    subject_path = workspace / subject_ref
    audited = EditedReportSubmission.model_validate_json(
        subject_path.read_text(encoding="utf-8")
    )
    subject_revision_match = re.search(r"chief-r(\d+)\.json$", subject_ref)
    subject_revision = (
        int(subject_revision_match.group(1))
        if subject_revision_match is not None
        else 0
    )
    snapshot_ref = f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
    snapshot_path = workspace / snapshot_ref
    if not snapshot_path.is_file():
        # Legacy same-run recovery: the final completion already binds the
        # exact edited JSON. Reconstruct only its deterministic Markdown
        # projection; no provider or reviewer call is repeated.
        _, canonical = build_delivery_projection(workspace, state, audited)
        validate_final_report_markdown(canonical, audited.special_topic_plan)
        canonical_ref = (
            f"Work/runs/{run_id}/validation/report-final-audit-legacy.md"
        )
        validation_ref = (
            f"Work/runs/{run_id}/reviews/report-integrity-final-audit-legacy.json"
        )
        store.write_text(canonical_ref, canonical)
        store.write_json(
            validation_ref,
            ValidationReport(
                validation_protocol_version=2,
                run_id=run_id,
                subject_ref=canonical_ref,
                subject_revision=subject_revision,
                validator="final-audit-legacy-snapshot/v2",
                check_ids=["final_report.fixed_sections_and_markdown"],
                passed=True,
            ).model_dump(mode="json"),
        )
        snapshot_path = store.write_json(
            snapshot_ref,
            FinalAuditSnapshot(
                run_id=run_id,
                subject_ref=subject_ref,
                subject_revision=subject_revision,
                canonical_markdown_ref=canonical_ref,
                validation_report_ref=validation_ref,
                completion_ref=completion_ref,
            ).model_dump(mode="json"),
        )
    snapshot = FinalAuditSnapshot.model_validate_json(
        snapshot_path.read_text(encoding="utf-8")
    )
    run_root = (workspace / f"Work/runs/{run_id}").resolve()
    for ref in (
        snapshot.subject_ref,
        snapshot.canonical_markdown_ref,
        snapshot.validation_report_ref,
        snapshot.completion_ref,
    ):
        path = (workspace / ref).resolve()
        if not path.is_relative_to(run_root) or not path.is_file():
            raise ValueError(
                f"final audit snapshot ref is outside the current run: {ref}"
            )
    if (
        snapshot.run_id != run_id
        or snapshot.subject_ref != subject_ref
        or snapshot.completion_ref != completion_ref
        or snapshot.subject_revision != subject_revision
    ):
        raise ValueError("final audit snapshot identity is stale")
    validation = ValidationReport.model_validate_json(
        (workspace / snapshot.validation_report_ref).read_text(encoding="utf-8")
    )
    if (
        not validation.passed
        or validation.run_id != run_id
        or validation.subject_ref != snapshot.canonical_markdown_ref
        or validation.subject_revision != snapshot.subject_revision
    ):
        raise ValueError("final audit snapshot validation identity is stale")
    _, audited_canonical = build_delivery_projection(workspace, state, audited)
    validate_final_report_markdown(
        audited_canonical,
        audited.special_topic_plan,
    )
    current = state.get("edited_report")
    if (
        current is not None
        and current.model_dump(mode="json") != audited.model_dump(mode="json")
    ):
        raise ValueError("in-memory edited report changed after final audit completion")
    state["final_audit_snapshot_ref"] = snapshot_ref
    return audited, snapshot_ref


__all__ = [
    "FINAL_REVIEW_COMPLETION_SESSION_KEYS",
    "latest_cross_synthesis",
    "latest_final_residual_risks",
    "load_current_review_completion",
    "validated_final_audit_subject",
]
