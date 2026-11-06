"""Start, resume, status, outputs and the Word file of render-existing.

The first group fixes what rendering an existing report does through the
public Reporting binding: the Word file it returns, how an interrupted
render continues, and how the Run is projected.  The second group covers
resume after the render workflow's definitions or its steps' code changed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from docx import Document

from manyselves.capabilities.distribution_reporting.adapters.runtime import (
    DistributionReportingRuntimeBinding,
)
from manyselves.capabilities.distribution_reporting.runtime import render_existing
from manyselves.capabilities.distribution_reporting.runtime.rendering.handoff_docx import (
    PackagedV2DocxCore,
)
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.definitions import DefinitionKind
from manyselves.runtime import generic_binding
from manyselves.runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView

COMMAND = UUID("50000000-0000-4000-8000-0000000000a1")
RUN_ID = f"render-existing-{COMMAND.hex}"
SOURCE = "Inputs/approved.md"
OUTPUT = "Outputs/Reports/approved.docx"
REQUEST = {
    "instruction": "Render the approved Markdown as DOCX.",
    "source_markdown_ref": SOURCE,
    "output_filename": "approved.docx",
}


def _binding(tmp_path: Path) -> DistributionReportingRuntimeBinding:
    return DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=None,
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )


def _write_source(tmp_path: Path, paragraph: str) -> None:
    path = tmp_path / SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Approved\n\n{paragraph}\n", encoding="utf-8")


def _run_dir(tmp_path: Path) -> Path:
    return tmp_path / "Work" / "runs" / RUN_ID


def _docx_text(tmp_path: Path) -> str:
    return "\n".join(p.text for p in Document(str(tmp_path / OUTPUT)).paragraphs)


def _started_actions(tmp_path: Path) -> list[str]:
    path = _run_dir(tmp_path) / "workflow-events.jsonl"
    return [
        event["action_id"]
        for event in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if event["kind"] == "action.started"
    ]


def _fail_render_once(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A passing fault in the render step: the code does not change."""

    original = PackagedV2DocxCore.render_approved_prose
    calls: list[int] = []

    def flaky(self, *args: Any, **kwargs: Any):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PackagedV2DocxCore, "render_approved_prose", flaky)
    return calls


async def _interrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> DistributionReportingRuntimeBinding:
    _write_source(tmp_path, "Frozen approved prose.")
    _fail_render_once(monkeypatch)
    binding = _binding(tmp_path)
    with pytest.raises(Exception, match="disk full"):
        await binding.start(COMMAND, "render-existing", REQUEST)
    assert binding.get_run(RUN_ID)["run"]["status"] == "failed"
    # The project file changes after the run froze it.
    _write_source(tmp_path, "Edited after the run started.")
    return binding


def _assert_word_output(binding: DistributionReportingRuntimeBinding, tmp_path: Path) -> None:
    result = json.loads(
        (_run_dir(tmp_path) / "render-result.json").read_text(encoding="utf-8")
    )
    assert binding.get_outputs(RUN_ID) == {
        "run_id": RUN_ID,
        "outputs": [
            {"id": "result", "kind": "value", "value": result},
            {
                "id": OUTPUT,
                "kind": "artifact",
                "path": OUTPUT,
                "exists": True,
                "size": (tmp_path / OUTPUT).stat().st_size,
            },
        ],
    }
    assert result["status"] == "completed"
    assert result["output_ref"] == OUTPUT
    assert result["source_snapshot_ref"] == (
        f"Work/runs/{RUN_ID}/frozen-project/{SOURCE}"
    )


# -- the behaviour that stays -------------------------------------------------


@pytest.mark.asyncio
async def test_started_render_returns_the_word_file_and_projects_the_run(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path, "Existing approved prose.")
    binding = _binding(tmp_path)

    accepted = await binding.start(COMMAND, "render-existing", REQUEST)

    assert accepted == {"run_id": RUN_ID, "task_id": None}
    projection = binding.get_run(RUN_ID)
    assert projection["run"] == {
        "run_id": RUN_ID,
        "capability_id": "distribution-reporting",
        "workflow_id": "render-existing",
        "status": "completed",
        "active": False,
        "task_id": None,
    }
    assert projection["waiting_input"] == []
    assert projection["state"]["status"] == "completed"
    _assert_word_output(binding, tmp_path)
    assert "Existing approved prose." in _docx_text(tmp_path)
    assert binding.get_cost(RUN_ID)["run_id"] == RUN_ID
    await binding.close()


@pytest.mark.asyncio
async def test_interrupted_render_continues_from_its_unfinished_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = await _interrupted_run(tmp_path, monkeypatch)
    assert _started_actions(tmp_path) == ["prepare-render", "render-document"]

    accepted = await binding.resume(COMMAND, RUN_ID)
    await binding.close()

    assert accepted == {"run_id": RUN_ID, "task_id": None}
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    # Nothing before the failed step ran again; the run's frozen source stays.
    assert _started_actions(tmp_path) == [
        "prepare-render",
        "render-document",
        "render-document",
        "finish-render",
    ]
    assert not (_run_dir(tmp_path) / "versions").exists()
    assert "Frozen approved prose." in _docx_text(tmp_path)
    assert "Edited after the run started." not in _docx_text(tmp_path)
    _assert_word_output(binding, tmp_path)


def _block_render(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the render step open until released (its code stays the same)."""

    original = render_existing._render_document
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held(*args: Any, **kwargs: Any):
        entered.set()
        await release.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(render_existing, "_render_document", held)
    return entered, release


@pytest.mark.asyncio
async def test_resumed_render_is_active_and_can_be_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = await _interrupted_run(tmp_path, monkeypatch)
    entered, release = _block_render(monkeypatch)

    accepted = await binding.resume(COMMAND, RUN_ID)
    await asyncio.wait_for(entered.wait(), timeout=5)

    assert accepted == {"run_id": RUN_ID, "task_id": None}
    assert binding.get_run(RUN_ID)["run"]["status"] == "running"
    assert binding.get_run(RUN_ID)["run"]["active"] is True
    settled = binding.cancel(RUN_ID)
    assert settled is not None
    await asyncio.wait_for(settled, timeout=5)
    await binding.close()
    assert binding.get_run(RUN_ID)["run"]["status"] == "failed"
    assert binding.get_run(RUN_ID)["run"]["active"] is False
    release.set()


@pytest.mark.asyncio
async def test_render_is_not_executed_again_while_its_start_still_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_source(tmp_path, "Existing approved prose.")
    entered, release = _block_render(monkeypatch)
    binding = _binding(tmp_path)
    await binding.start_detached(COMMAND, "render-existing", REQUEST)
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert binding.get_run(RUN_ID)["run"]["active"] is True

    with pytest.raises(CapabilityRunStateError, match="still executing"):
        await binding.rerun(UUID(int=7), RUN_ID)

    # The executing version is neither archived nor joined by a second one.
    assert binding.versions(RUN_ID) == []
    assert not (_run_dir(tmp_path) / "versions").exists()
    release.set()
    await binding.close()
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    _assert_word_output(binding, tmp_path)


@pytest.mark.asyncio
async def test_resume_of_a_completed_render_leaves_it_as_it_is(tmp_path: Path) -> None:
    _write_source(tmp_path, "Existing approved prose.")
    binding = _binding(tmp_path)
    await binding.start(COMMAND, "render-existing", REQUEST)
    events = (_run_dir(tmp_path) / "workflow-events.jsonl").read_text(encoding="utf-8")

    accepted = await binding.resume(COMMAND, RUN_ID)
    await binding.close()

    assert accepted == {"run_id": RUN_ID, "task_id": None}
    assert (_run_dir(tmp_path) / "workflow-events.jsonl").read_text(
        encoding="utf-8"
    ) == events
    assert not (_run_dir(tmp_path) / "versions").exists()


@pytest.mark.asyncio
async def test_render_takes_no_input_and_unknown_runs_are_not_found(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path, "Existing approved prose.")
    binding = _binding(tmp_path)
    await binding.start(COMMAND, "render-existing", REQUEST)

    for input_id in (None, "ask-evidence"):
        with pytest.raises(ValueError):
            await binding.provide_input(
                COMMAND, RUN_ID, input_id=input_id, values={"answer": "x"}
            )
    with pytest.raises(CapabilityRunInputError):
        await binding.provide_input(COMMAND, RUN_ID, input_id=None, values={})
    for call in (binding.get_run, binding.get_outputs, binding.get_cost):
        with pytest.raises(CapabilityRunNotFoundError):
            call("render-existing-missing")
    with pytest.raises(CapabilityRunNotFoundError):
        await binding.resume(COMMAND, "render-existing-missing")
    await binding.close()


# -- resume after the render's definitions or code changed --------------------


def _change_render_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    original = generic_binding.load_capability

    def changed(path):
        capability, registry = original(path)
        tools = registry._definitions[DefinitionKind.TOOL]
        tool = tools["render-existing"]
        tools["render-existing"] = tool.model_copy(
            update={"description": tool.description + " Changed."}
        )
        return capability, registry

    monkeypatch.setattr(generic_binding, "load_capability", changed)


def _assert_executed_again(tmp_path: Path, reason: str) -> None:
    version = json.loads(
        (_run_dir(tmp_path) / "versions/v1/version.json").read_text(encoding="utf-8")
    )
    assert version["reason"] == reason
    assert version["status"] == "failed"
    # The interrupted version keeps its own record of what it did.
    assert (_run_dir(tmp_path) / "versions/v1/workflow-events.jsonl").is_file()
    # The current version ran every step under the current definitions and code.
    assert _started_actions(tmp_path) == [
        "prepare-render",
        "render-document",
        "finish-render",
    ]


@pytest.mark.asyncio
async def test_changed_render_definition_executes_the_run_again_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _interrupted_run(tmp_path, monkeypatch)
    _change_render_definition(monkeypatch)
    binding = _binding(tmp_path)

    accepted = await binding.resume(COMMAND, RUN_ID)
    await binding.close()

    assert accepted == {"run_id": RUN_ID, "task_id": None}
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    _assert_executed_again(tmp_path, "definitions changed")
    saved = (_run_dir(tmp_path) / "resolved-plan.json").read_text(encoding="utf-8")
    assert "Changed." in saved
    # The same frozen source is rendered; the Word file is returned as before.
    assert "Frozen approved prose." in _docx_text(tmp_path)
    assert "Edited after the run started." not in _docx_text(tmp_path)
    _assert_word_output(binding, tmp_path)


@pytest.mark.asyncio
async def test_changed_step_code_executes_the_run_again_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _interrupted_run(tmp_path, monkeypatch)
    step_code = _run_dir(tmp_path) / "step-code.json"
    saved = json.loads(step_code.read_text(encoding="utf-8"))
    assert set(saved["tools"]) == {"prepare-render-existing", "render-existing"}
    # The run executed an earlier version of the render step's code.
    saved["tools"]["render-existing"] = "earlier"
    step_code.write_text(json.dumps(saved), encoding="utf-8")
    binding = _binding(tmp_path)

    await binding.resume(COMMAND, RUN_ID)
    await binding.close()

    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    _assert_executed_again(tmp_path, "code changed")
    assert "Frozen approved prose." in _docx_text(tmp_path)
    _assert_word_output(binding, tmp_path)


@pytest.mark.asyncio
async def test_render_started_before_this_change_is_executed_again_from_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _interrupted_run(tmp_path, monkeypatch)
    # A run of the earlier render runtime saved neither of these.
    (_run_dir(tmp_path) / "start-input.json").unlink()
    (_run_dir(tmp_path) / "step-code.json").unlink()
    _change_render_definition(monkeypatch)
    binding = _binding(tmp_path)

    await binding.resume(COMMAND, RUN_ID)
    await binding.close()

    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    _assert_executed_again(tmp_path, "definitions changed")
    assert "Frozen approved prose." in _docx_text(tmp_path)
    _assert_word_output(binding, tmp_path)


@pytest.mark.asyncio
async def test_completed_render_is_not_executed_again_after_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_source(tmp_path, "Existing approved prose.")
    binding = _binding(tmp_path)
    await binding.start(COMMAND, "render-existing", REQUEST)
    _change_render_definition(monkeypatch)
    binding = _binding(tmp_path)

    await binding.resume(COMMAND, RUN_ID)
    await binding.close()

    assert not (_run_dir(tmp_path) / "versions").exists()
    _assert_word_output(binding, tmp_path)


@pytest.mark.asyncio
async def test_render_takes_no_revision(tmp_path: Path) -> None:
    _write_source(tmp_path, "Existing approved prose.")
    binding = _binding(tmp_path)
    await binding.start(COMMAND, "render-existing", REQUEST)

    with pytest.raises(CapabilityRunInputError, match="no revision"):
        await binding.rerun(COMMAND, RUN_ID, {"instruction": "change it"})
    await binding.close()

    # The version it archived to try is put back.
    assert binding.versions(RUN_ID) == []
    assert not (_run_dir(tmp_path) / "versions/v1").exists()
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    _assert_word_output(binding, tmp_path)
