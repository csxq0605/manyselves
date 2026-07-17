from pathlib import Path

from manyselves.core.artifacts import ArtifactGateway, ArtifactGrant
from manyselves.core.reporting.agentic_models import TaskEnvelope
from manyselves.core.reporting.capabilities import compile_agent_access
from manyselves.core.reporting.config import load_packaged_agents


def test_planner_and_chief_have_executable_artifact_readers(tmp_path: Path) -> None:
    artifact = tmp_path / "Work/input.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("evidence", encoding="utf-8")
    root = ArtifactGateway(tmp_path, ArtifactGrant("wf", "task", "agent", "session"))
    agents = load_packaged_agents()
    for agent_id, output in (("report-planner", "plan_submission"), ("chief-editor", "edited_report_submission")):
        envelope = TaskEnvelope(
            task_id="task",
            run_id="run",
            agent_id=agent_id,
            objective="work",
            input_refs=["Work/input.txt"],
            allowed_outputs=[output],
        )
        access = compile_agent_access(agents[agent_id], envelope, envelope.input_refs, gateway=root)
        assert access.unreadable_refs == ()
        assert {"open_artifact", "search_text"}.issubset(access.tool_names)
