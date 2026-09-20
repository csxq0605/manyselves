"""State persistence port for the declarative runtime."""

from typing import Protocol

from manyselves.kernel.workflow import ResolvedPlan, WorkflowState


class WorkflowStateStore(Protocol):
    def save(self, state: WorkflowState) -> None: ...

    def load(self, run_id: str) -> WorkflowState: ...


class ResolvedPlanStore(Protocol):
    def save_plan(self, run_id: str, plan: ResolvedPlan) -> None: ...

    def load_plan(self, run_id: str) -> ResolvedPlan: ...
