from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.core.artifacts import ArtifactGateway, ArtifactGrant
from manyselves.core.reporting.capabilities import (
    collect_photo_ids,
    collect_reference_refs,
    compile_agent_access,
)
from manyselves.core.reporting.config import ConfigurationError, load_packaged_agents
from manyselves.core.reporting.input_contracts import (
    TEMPLATE_ROLE_SKILL_IDS,
    TemplateDistillationInput,
)


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


def test_nested_typed_refs_compile_to_descriptor_capabilities(tmp_path: Path) -> None:
    subject = tmp_path / "Work/runs/run-nested/subjects/subject.json"
    finding = tmp_path / "Work/runs/run-nested/reviews/finding.json"
    photo = tmp_path / "Work/runs/run-nested/photos/P-001.png"
    for path, payload in (
        (subject, b'{"subject": true}'),
        (finding, b'{"finding": true}'),
        (photo, b"\x89PNG\r\n\x1a\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("wf", "task", "module-2.1-specialist", "session"),
    )
    definition = load_packaged_agents()["module-2.1-specialist"]
    envelope = TaskEnvelope(
        task_id="task",
        run_id="run-nested",
        agent_id="module-2.1-specialist",
        objective="compile nested references",
        input_refs=[
            "Work/runs/run-nested/subjects/subject.json",
            "Work/runs/run-nested/reviews/finding.json",
            "Work/runs/run-nested/photos/P-001.png",
        ],
        artifact_delivery_modes={
            "Work/runs/run-nested/subjects/subject.json": "reference",
            "Work/runs/run-nested/reviews/finding.json": "reference",
            "Work/runs/run-nested/photos/P-001.png": "reference",
        },
    )
    typed_input = {
        "subject": {
            "subject_ref": "Work/runs/run-nested/subjects/subject.json",
            "finding": {
                "finding_ref": "Work/runs/run-nested/reviews/finding.json",
            },
        },
        "photos": [{"id": "P-001", "path": "Work/runs/run-nested/photos/P-001.png"}],
    }

    refs = collect_reference_refs(envelope, typed_input=typed_input)
    assert "Work/runs/run-nested/reviews/finding.json" in refs
    assert collect_photo_ids(typed_input) == ("P-001",)
    access = compile_agent_access(
        definition,
        envelope,
        gateway=gateway,
        typed_input=typed_input,
    )

    assert {item.canonical_ref for item in access.capabilities} >= {
        "Work/runs/run-nested/subjects/subject.json",
        "Work/runs/run-nested/reviews/finding.json",
        "Work/runs/run-nested/photos/P-001.png",
    }
    image = access.get("Work/runs/run-nested/photos/P-001.png")
    assert image is not None
    assert image.media_type == "image/png"
    assert image.allows_operation("inspect_image")
    assert access.photo_map()["P-001"] == "Work/runs/run-nested/photos/P-001.png"


def test_capability_scope_rejects_traversal_and_cross_run_refs(tmp_path: Path) -> None:
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("wf", "task", "module-2.1-specialist", "session"),
    )
    definition = load_packaged_agents()["module-2.1-specialist"]
    for bad_ref in ("../outside.txt", "Work/runs/other-run/result.json"):
        envelope = TaskEnvelope(
            task_id="task",
            run_id="run-current",
            agent_id="module-2.1-specialist",
            objective="reject out-of-scope ref",
            input_refs=[bad_ref],
        )
        with pytest.raises(ConfigurationError):
            compile_agent_access(definition, envelope, gateway=gateway)


def test_empty_task_does_not_receive_generic_artifact_readers(tmp_path: Path) -> None:
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("wf", "task", "module-2.4-specialist", "session"),
    )
    definition = load_packaged_agents()["module-2.4-specialist"]
    envelope = TaskEnvelope(
        task_id="task",
        run_id="run",
        agent_id="module-2.4-specialist",
        objective="continue from durable parts",
        allowed_tools=["list_result_parts", "write_result_part", "submit_result"],
    )
    access = compile_agent_access(definition, envelope, gateway=gateway)
    assert "open_artifact" not in access.tool_names
    assert "search_text" not in access.tool_names
    assert access.tool_names[-1] == "open_tool_result"


def test_isolated_template_snapshot_is_reserved_for_distiller_tool(
    tmp_path: Path,
) -> None:
    run_id = "run-template-access"
    template_ref = (
        f"Work/runs/{run_id}/templates/template-for-skill.docx"
    )
    template = tmp_path / template_ref
    template.parent.mkdir(parents=True)
    template.write_bytes(b"isolated template snapshot")
    contract_ref = (
        f"Work/runs/{run_id}/context/template-distillation-input.json"
    )
    contract = tmp_path / contract_ref
    contract.parent.mkdir(parents=True)
    contract.write_text(
        TemplateDistillationInput(
            run_id=run_id,
            template_ref=template_ref,
            inspect_max_chars=100_000,
            required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
        ).model_dump_json(),
        encoding="utf-8",
    )
    distiller = load_packaged_agents()["template-distiller"]
    distiller_envelope = TaskEnvelope(
        task_id="template-skill-distillation",
        run_id=run_id,
        agent_id="template-distiller",
        objective="distill the isolated template",
        input_refs=[contract_ref, template_ref],
        input_contract_kind="template_distillation_input",
        input_contract_ref=contract_ref,
    )
    distiller_gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant(
            "wf",
            "template-skill-distillation",
            "template-distiller",
            "session",
        ),
    )

    access = compile_agent_access(
        distiller,
        distiller_envelope,
        gateway=distiller_gateway,
    )

    assert template_ref not in access.readable_refs
    assert access.get(template_ref) is None

    other_agent = load_packaged_agents()["module-2.1-specialist"]
    other_envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id=run_id,
        agent_id="module-2.1-specialist",
        objective="must not read the isolated template",
        input_refs=[template_ref],
    )
    other_gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("wf", "module-2.1", "module-2.1-specialist", "session"),
    )
    with pytest.raises(ConfigurationError, match="EXPERT_TEMPLATE_AGENT_ACCESS_FORBIDDEN"):
        compile_agent_access(
            other_agent,
            other_envelope,
            gateway=other_gateway,
        )
