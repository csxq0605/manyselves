"""Main's report command: one operation per user-facing flow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting.adapters.main_tool import (
    RunReportingWorkflowTool,
    attach_main_reporting_tool,
)
from manyselves.capabilities.distribution_reporting.runtime.template_skills import (
    TEMPLATE_SKILL_ROOT,
    run_root,
    skill_order,
    template_skill_ref,
)
from manyselves.runtime.prompts.loader import PromptLoader


class _Projection:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path(".")
        self.started: list[tuple[UUID, str, object]] = []
        self.reruns: list[tuple[str, object]] = []

    async def start(self, command_id: UUID, workflow_id: str, values: object) -> dict:
        self.started.append((command_id, workflow_id, values))
        return {"status": "accepted", "run_id": f"{workflow_id}-command"}

    async def rerun(self, command_id: UUID, run_id: str, values: object = None) -> dict:
        self.reruns.append((run_id, values))
        return {"status": "accepted", "run_id": run_id}


class _Conversations:
    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind_run_to_active_conversation(self, run_id: str, agent_id: str = "main") -> str:
        self.bound.append(run_id)
        return "conversation-main"


def _tool(projection: _Projection) -> tuple[RunReportingWorkflowTool, _Conversations]:
    conversations = _Conversations()
    return (
        RunReportingWorkflowTool(
            projection_resolver=lambda: projection,
            conversation_resolver=lambda: conversations,
        ),
        conversations,
    )


@pytest.mark.asyncio
async def test_full_report_starts_a_new_run() -> None:
    projection = _Projection()
    tool, conversations = _tool(projection)

    result = await tool(operation="full_report", instruction="从 Inputs 生成完整报告。")

    _command, workflow_id, values = projection.started[0]
    assert workflow_id == "full-report"
    assert values["instruction"] == "从 Inputs 生成完整报告。"
    assert values["target_modules"] == ["2.1", "2.2", "2.3", "2.4", "2.5"]
    assert not {"baseline_run_id", "requested_changes", "impact_mode"} & set(values)
    assert conversations.bound == ["full-report-command"]
    assert result["conversation_id"] == "conversation-main"


@pytest.mark.asyncio
async def test_revise_report_reruns_the_same_run_as_a_new_version() -> None:
    projection = _Projection()
    tool, _conversations = _tool(projection)

    result = await tool(
        operation="revise_report",
        run_id="full-report-abc",
        instruction="按新资料修订。",
        changes=[{"target": "2.3.1", "instruction": "语气更客观", "scope": "text"}],
        new_inputs=True,
    )

    assert projection.started == []
    run_id, values = projection.reruns[0]
    assert run_id == "full-report-abc" and result["run_id"] == "full-report-abc"
    assert values == {
        "instruction": "按新资料修订。",
        "changes": [{"target": "2.3.1", "instruction": "语气更客观", "scope": "text"}],
        "new_inputs": True,
    }


@pytest.mark.asyncio
async def test_revise_report_needs_the_run_to_change() -> None:
    tool, _conversations = _tool(_Projection())
    with pytest.raises(ValueError, match="run_id"):
        await tool(operation="revise_report", instruction="改一下")


@pytest.mark.asyncio
async def test_extract_and_render_start_their_own_workflows() -> None:
    projection = _Projection()
    tool, _conversations = _tool(projection)

    await tool(operation="extract_skills", roles=["author-2.1"])
    await tool(operation="render_existing", source_markdown_ref="Outputs/a.md")

    assert [item[1] for item in projection.started] == [
        "extract-template-skills",
        "render-existing",
    ]
    assert projection.started[0][2] == {"roles": ["author-2.1"]}
    assert projection.started[1][2]["source_markdown_ref"] == "Outputs/a.md"


@pytest.mark.asyncio
async def test_publish_skills_previews_until_confirmed(tmp_path: Path) -> None:
    roles = skill_order()
    root = tmp_path / run_root("extract-1")
    candidates = {}
    for role in roles:
        published = tmp_path / template_skill_ref(role)
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_text("旧写法\n", encoding="utf-8")
        candidate = root / "candidates" / f"{role}.md"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("旧写法\n" if role == roles[0] else "新写法\n", encoding="utf-8")
        candidates[role] = candidate.relative_to(tmp_path).as_posix()
    (root / "summary.json").write_text(
        json.dumps({"candidates": candidates}, ensure_ascii=False), encoding="utf-8"
    )
    tool, _conversations = _tool(_Projection(tmp_path))

    preview = await tool(operation="publish_skills", run_id="extract-1")

    assert preview["status"] == "preview"
    statuses = {row["role"]: row["status"] for row in preview["roles"]}
    assert statuses[roles[0]] == "same"
    assert statuses[roles[1]] == "changed"
    # Nothing was published by the preview.
    assert (tmp_path / template_skill_ref(roles[1])).read_text(encoding="utf-8") == "旧写法\n"

    published = await tool(operation="publish_skills", run_id="extract-1", confirm=True)

    assert published["status"] == "published"
    assert (tmp_path / template_skill_ref(roles[1])).read_text(encoding="utf-8") == "新写法\n"
    assert (tmp_path / TEMPLATE_SKILL_ROOT / "boundary.json").is_file()


def test_attaches_reporting_tool_to_main_runtime() -> None:
    registered: list[tuple[str, object]] = []
    manager = SimpleNamespace(
        register_agent_tool=lambda agent_id, tool: registered.append((agent_id, tool)) or True
    )

    attached = attach_main_reporting_tool(
        SimpleNamespace(loop_manager=manager),
        projection_resolver=lambda: _Projection(),
        conversation_resolver=lambda: _Conversations(),
    )

    assert attached is True
    assert registered[0][0] == "main"
    assert registered[0][1].name == "run_reporting_workflow"


def test_main_prompt_gets_the_report_part_from_the_capability() -> None:
    prompt = PromptLoader().load_prompt("main")

    assert "resume_run" in prompt
    assert "run_reporting_workflow" in prompt
    assert prompt.index("resume_run") < prompt.index("run_reporting_workflow")
