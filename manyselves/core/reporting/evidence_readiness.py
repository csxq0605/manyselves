"""Deterministic missing-evidence policy before professional work starts."""

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CoverageMatrix,
    CoverageStatus,
    ReportingModel,
    ReportRequest,
)


class EvidenceReadinessDecision(ReportingModel):
    """Whether the user's explicit missing-evidence policy requires a pause."""

    should_block: bool
    missing_evidence: list[str]
    affected_modules: list[str]


class ReportingBlockedError(RuntimeError):
    def __init__(
        self,
        missing_evidence: list[str],
        affected_modules: list[str] | None = None,
    ):
        self.missing_evidence = missing_evidence
        self.affected_modules = affected_modules or []
        super().__init__(
            "reporting requires evidence confirmation: "
            + ", ".join(missing_evidence)
        )


class EvidenceReadinessPolicy:
    """Apply only the request's explicit ask/block/skip/draft evidence policy."""

    @staticmethod
    def evaluate(
        request: ReportRequest,
        coverage: CoverageMatrix,
    ) -> EvidenceReadinessDecision:
        missing: list[str] = []
        affected: list[str] = []
        for module_id in request.target_modules:
            module_has_gap = False
            entry = coverage.entries[module_id]
            for submodule_id, submodule in entry.submodules.items():
                if submodule.status is CoverageStatus.READY:
                    continue
                module_has_gap = True
                missing.extend(
                    submodule.gaps
                    or [f"{submodule_id} 缺少可追溯客户证据"]
                )
            if module_has_gap:
                affected.append(module_id)
        missing = list(dict.fromkeys(missing))
        return EvidenceReadinessDecision(
            should_block=(
                bool(missing)
                and request.missing_evidence_policy in {"ask", "block"}
            ),
            missing_evidence=missing,
            affected_modules=affected,
        )
