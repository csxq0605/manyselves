"""File-backed implementation of the Kernel workflow-state port."""

from pathlib import Path

from manyselves.kernel.workflow import ResolvedPlan, WorkflowState


class FileWorkflowStateStore:
    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    def save(self, state: WorkflowState) -> None:
        path = self._state_path(state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def load(self, run_id: str) -> WorkflowState:
        return WorkflowState.model_validate_json(
            self._state_path(run_id).read_text(encoding="utf-8")
        )

    def save_plan(self, run_id: str, plan: ResolvedPlan) -> None:
        path = self._plan_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    def load_plan(self, run_id: str) -> ResolvedPlan:
        return ResolvedPlan.model_validate_json(
            self._plan_path(run_id).read_text(encoding="utf-8")
        )

    def _state_path(self, run_id: str) -> Path:
        return self._workspace / "Work" / "runs" / run_id / "workflow-state.json"

    def _plan_path(self, run_id: str) -> Path:
        return self._workspace / "Work" / "runs" / run_id / "resolved-plan.json"
