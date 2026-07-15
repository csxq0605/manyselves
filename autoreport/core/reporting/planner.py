"""Turn deterministic coverage into evidence-scoped module tasks."""

from .models import (
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ModuleTask,
    ReportRequest,
)


class CoveragePlanningError(RuntimeError):
    def __init__(self, missing_submodules: list[str]):
        self.missing_submodules = missing_submodules
        super().__init__("missing evidence for submodules: " + ", ".join(missing_submodules))


def plan_modules(
    request: ReportRequest,
    coverage: CoverageMatrix,
    evidence_items: list[EvidenceItem],
) -> list[ModuleTask]:
    """Plan only evidence that belongs to the requested module/submodule."""

    known_evidence = {item.id: item for item in evidence_items}
    tasks: list[ModuleTask] = []
    for module_id in request.target_modules:
        module = coverage.entries[module_id]
        ready = {
            submodule_id: entry.evidence_ids
            for submodule_id, entry in module.submodules.items()
            if entry.status is CoverageStatus.READY
        }
        missing = [
            submodule_id
            for submodule_id, entry in module.submodules.items()
            if entry.status is not CoverageStatus.READY
        ]
        if missing and request.missing_evidence_policy in {"ask", "block"}:
            raise CoveragePlanningError(missing)

        for submodule_id, evidence_ids in ready.items():
            for evidence_id in evidence_ids:
                item = known_evidence.get(evidence_id)
                if item is None or item.module_id != module_id or item.submodule_id != submodule_id:
                    raise ValueError(
                        f"coverage assigned invalid evidence {evidence_id} to {submodule_id}"
                    )

        if request.missing_evidence_policy == "skip" and not ready:
            continue
        evidence_ids = list(
            dict.fromkeys(
                evidence_id for submodule_ids in ready.values() for evidence_id in submodule_ids
            )
        )
        tasks.append(
            ModuleTask(
                id=f"module-{module_id}",
                module_id=module_id,
                evidence_ids=evidence_ids,
                submodule_evidence=ready,
                missing_submodules=missing if request.missing_evidence_policy == "draft" else [],
                skipped_submodules=missing if request.missing_evidence_policy == "skip" else [],
                allow_unverified=request.missing_evidence_policy == "draft",
            )
        )
    return tasks
