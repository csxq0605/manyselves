"""Deterministic module checks that run before paid semantic review.

The checks in this module only evaluate explicit, reproducible predicates.  They
may return a module to its original author, but they never create or close a
reviewer finding and never imply semantic approval.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .agentic_models import ModuleSubmission
from .input_contracts import ValidationFailure, ValidationReport
from .source_ledger import SourceLedger

_CHECK_IDS = (
    "module_preflight.control_markers",
    "module_preflight.claim_source_ids_unique",
    "module_preflight.sources_registered",
    "module_preflight.source_content_readable",
    "module_preflight.source_content_matches_ledger",
)
_CLAIM_MARKER = re.compile(r"\[\[CLAIM:C-[^\]\s]+\]\]")


@dataclass(frozen=True)
class ModuleReviewPreflightResult:
    """A content-bound machine report plus its exact author-correction scope."""

    report: ValidationReport
    target_submodule_ids: frozenset[str]


def _unsupported_markers(narrative: str) -> list[str]:
    """Return every ``[[...`` token that is not one valid Claim marker."""

    invalid: list[str] = []
    offset = 0
    while True:
        start = narrative.find("[[", offset)
        if start < 0:
            return invalid
        allowed = _CLAIM_MARKER.match(narrative, start)
        if allowed is not None:
            offset = allowed.end()
            continue
        end = narrative.find("]]", start + 2)
        if end < 0:
            end = min(len(narrative), start + 120)
        else:
            end += 2
        invalid.append(narrative[start:end])
        offset = max(end, start + 2)


def evaluate_module_review_preflight(
    workspace: Path,
    *,
    run_id: str,
    subject: ModuleSubmission,
    subject_ref: str,
    upstream_report: ValidationReport,
) -> ModuleReviewPreflightResult:
    """Evaluate machine-only predicates against one exact persisted module."""

    workspace = Path(workspace).resolve()
    subject_path = (workspace / subject_ref).resolve()
    if not subject_path.is_relative_to(workspace) or not subject_path.is_file():
        raise ValueError(f"module preflight subject is unreadable: {subject_ref}")
    subject_bytes = subject_path.read_bytes()
    content_sha256 = hashlib.sha256(subject_bytes).hexdigest()
    try:
        persisted_subject = ModuleSubmission.model_validate_json(subject_bytes)
    except ValueError as exc:
        raise ValueError(
            f"module preflight subject is not a valid module artifact: {subject_ref}"
        ) from exc
    if persisted_subject != subject:
        raise ValueError(
            "module preflight in-memory subject differs from its persisted artifact"
        )
    if (
        not upstream_report.passed
        or upstream_report.subject_ref != subject_ref
        or upstream_report.subject_revision != subject.revision
        or upstream_report.content_sha256 != content_sha256
    ):
        raise ValueError("module preflight requires a passed, content-bound upstream report")

    failures: list[ValidationFailure] = []
    target_submodule_ids: set[str] = set()
    for submodule_id, narrative in subject.submodule_narratives.items():
        invalid_markers = _unsupported_markers(narrative)
        if invalid_markers:
            failures.append(
                ValidationFailure(
                    check_id="module_preflight.control_markers",
                    target_path=f"submodule_narratives.{submodule_id}",
                    message=(
                        "reader-visible prose contains unsupported runtime markers: "
                        f"{invalid_markers}"
                    ),
                )
            )
            target_submodule_ids.add(submodule_id)

    ledger = SourceLedger(workspace, run_id)
    records = {record.id: record for record in ledger.records}
    registered = set(records)
    for claim in subject.claims:
        duplicates = sorted(
            source_id
            for source_id, count in Counter(claim.source_ids).items()
            if count > 1
        )
        if duplicates:
            failures.append(
                ValidationFailure(
                    check_id="module_preflight.claim_source_ids_unique",
                    target_path=f"claims.{claim.id}.source_ids",
                    message=f"Claim source_ids contain duplicates: {duplicates}",
                )
            )
            target_submodule_ids.add(claim.submodule_id)
        unknown = sorted(set(claim.source_ids) - registered)
        if unknown:
            failures.append(
                ValidationFailure(
                    check_id="module_preflight.sources_registered",
                    target_path=f"claims.{claim.id}.source_ids",
                    message=f"Claim references unregistered current-run sources: {unknown}",
                )
            )
            target_submodule_ids.add(claim.submodule_id)
        unreadable: list[str] = []
        stale: list[str] = []
        for source_id in sorted(set(claim.source_ids) & registered):
            content_ref = ledger.content_ref(source_id)
            if content_ref is None:
                unreadable.append(source_id)
                continue
            try:
                content = (workspace / content_ref).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                unreadable.append(source_id)
                continue
            expected_sha256 = records[source_id].content_sha256
            if (
                expected_sha256 is None
                or hashlib.sha256(content.encode("utf-8")).hexdigest()
                != expected_sha256
            ):
                stale.append(source_id)
        if unreadable:
            failures.append(
                ValidationFailure(
                    check_id="module_preflight.source_content_readable",
                    target_path=f"claims.{claim.id}.source_ids",
                    message=f"Claim source content is not readable: {unreadable}",
                )
            )
            target_submodule_ids.add(claim.submodule_id)
        if stale:
            failures.append(
                ValidationFailure(
                    check_id="module_preflight.source_content_matches_ledger",
                    target_path=f"claims.{claim.id}.source_ids",
                    message=(
                        "Claim source content does not match the current-run ledger: "
                        f"{stale}"
                    ),
                )
            )
            target_submodule_ids.add(claim.submodule_id)

    observations = list(upstream_report.observations)
    duplicate_questions = sorted(
        value
        for value, count in Counter(subject.unresolved_questions).items()
        if value.strip() and count > 1
    )
    if duplicate_questions:
        observations.append(
            "duplicate_unresolved_questions:" + " | ".join(duplicate_questions)
        )
    duplicate_claim_text = sorted(
        text
        for text, count in Counter(
            re.sub(r"\s+", " ", claim.text).strip() for claim in subject.claims
        ).items()
        if text and count > 1
    )
    if duplicate_claim_text:
        observations.append(
            "duplicate_claim_text:" + " | ".join(duplicate_claim_text)
        )

    report = ValidationReport(
        validation_protocol_version=2,
        run_id=run_id,
        subject_ref=subject_ref,
        subject_revision=subject.revision,
        content_sha256=content_sha256,
        validator="module-review-preflight/v1",
        check_ids=list(dict.fromkeys([*upstream_report.check_ids, *_CHECK_IDS])),
        failures=failures,
        observations=observations,
        passed=not failures,
    )
    return ModuleReviewPreflightResult(
        report=report,
        target_submodule_ids=frozenset(target_submodule_ids),
    )
