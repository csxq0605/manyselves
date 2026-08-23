"""Deterministic module checks that run before paid semantic review.

The checks in this module only evaluate explicit, reproducible predicates.  They
may return a module to its original author, but they never create or close a
reviewer finding and never imply semantic approval.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ModuleSubmission
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ValidationFailure,
    ValidationReport,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger

_CHECK_IDS = (
    "module_preflight.control_markers",
    "module_preflight.claim_source_ids_unique",
    "module_preflight.sources_registered",
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
    """Evaluate structural prerequisites against one exact persisted module."""

    workspace = Path(workspace).resolve()
    subject_path = (workspace / subject_ref).resolve()
    if not subject_path.is_relative_to(workspace) or not subject_path.is_file():
        raise ValueError(f"module preflight subject is unreadable: {subject_ref}")
    if (
        not upstream_report.passed
        or upstream_report.subject_ref != subject_ref
        or upstream_report.subject_revision != subject.revision
    ):
        raise ValueError("module preflight requires a passed structural report for its subject")

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
    registered = {record.id for record in ledger.records}
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

    report = ValidationReport(
        validation_protocol_version=2,
        run_id=run_id,
        subject_ref=subject_ref,
        subject_revision=subject.revision,
        validator="module-review-preflight/v1",
        check_ids=list(dict.fromkeys([*upstream_report.check_ids, *_CHECK_IDS])),
        failures=failures,
        passed=not failures,
    )
    return ModuleReviewPreflightResult(
        report=report,
        target_submodule_ids=frozenset(target_submodule_ids),
    )
