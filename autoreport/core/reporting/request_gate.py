"""Deterministic request/coverage gate before professional Agents start."""

from typing import Literal

from .models import CoverageMatrix, CoverageStatus, ReportingModel, ReportRequest


class GateDecision(ReportingModel):
    proceed: bool
    status: Literal["running", "blocked"]
    missing_evidence: list[str]


class ReportingBlockedError(RuntimeError):
    def __init__(self, missing_evidence: list[str]):
        self.missing_evidence = missing_evidence
        super().__init__("reporting requires evidence confirmation: " + ", ".join(missing_evidence))


class RequestGate:
    @staticmethod
    def evaluate(request: ReportRequest, coverage: CoverageMatrix) -> GateDecision:
        missing: list[str] = []
        for module_id in request.target_modules:
            entry = coverage.entries[module_id]
            for submodule_id, submodule in entry.submodules.items():
                if submodule.status is CoverageStatus.READY:
                    continue
                missing.extend(submodule.gaps or [f"{submodule_id} 缺少可追溯客户证据"])
        missing = list(dict.fromkeys(missing))
        blocked = bool(missing) and request.missing_evidence_policy in {"ask", "block"}
        return GateDecision(
            proceed=not blocked,
            status="blocked" if blocked else "running",
            missing_evidence=missing,
        )
