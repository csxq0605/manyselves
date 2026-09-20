"""Physical ownership characterization for Reporting evidence readiness policy."""

from __future__ import annotations

from importlib.util import find_spec


def test_legacy_policy_symbols_are_owned_by_distribution_capability() -> None:
    from manyselves.capabilities.distribution_reporting.domain.evidence_readiness import (
        EvidenceReadinessDecision,
        EvidenceReadinessPolicy,
        ReportingBlockedError,
    )

    expected_module = (
        "manyselves.capabilities.distribution_reporting.domain.evidence_readiness"
    )
    assert EvidenceReadinessDecision.__module__ == expected_module
    assert EvidenceReadinessPolicy.__module__ == expected_module
    assert ReportingBlockedError.__module__ == expected_module


def test_old_core_readiness_module_is_absent() -> None:
    assert find_spec("manyselves.core.reporting") is None
