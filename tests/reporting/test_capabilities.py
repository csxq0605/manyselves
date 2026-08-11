from pathlib import Path

import pytest

from manyselves.core.artifacts import ArtifactGateway, ArtifactGrant
from manyselves.core.reporting.agentic_models import TaskEnvelope
from manyselves.core.reporting.capabilities import compile_agent_access
from manyselves.core.reporting.config import ConfigurationError, load_packaged_agents


def test_chief_has_executable_artifact_readers(tmp_path: Path) -> None:
    artifact = tmp_path / "Work/input.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("evidence", encoding="utf-8")
    root = ArtifactGateway(tmp_path, ArtifactGrant("wf", "task", "agent", "session"))
    agents = load_packaged_agents()
    for agent_id, output in (("chief-editor", "edited_report_submission"),):
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


def test_task_envelope_can_narrow_declared_tools_for_recovery(tmp_path: Path) -> None:
    root = ArtifactGateway(tmp_path, ArtifactGrant("wf", "task", "agent", "session"))
    definition = load_packaged_agents()["module-2.4-specialist"]
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id="run",
        agent_id="module-2.4-specialist",
        objective="resume missing parts",
        allowed_tools=["list_result_parts", "write_result_part", "submit_result"],
    )

    access = compile_agent_access(definition, envelope, [], gateway=root)

    assert access.tool_names == (
        "write_result_part",
        "list_result_parts",
        "submit_result",
        "open_tool_result",
    )


def test_task_envelope_rejects_undeclared_tool(tmp_path: Path) -> None:
    root = ArtifactGateway(tmp_path, ArtifactGrant("wf", "task", "agent", "session"))
    definition = load_packaged_agents()["module-2.4-specialist"]
    envelope = TaskEnvelope(
        task_id="module-2.4",
        run_id="run",
        agent_id="module-2.4-specialist",
        objective="resume missing parts",
        allowed_tools=["exec"],
    )

    with pytest.raises(ConfigurationError, match="undeclared tools"):
        compile_agent_access(definition, envelope, [], gateway=root)
