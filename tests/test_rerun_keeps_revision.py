"""A revision interrupted and then executed again keeps its revision.

When a revision of a report stops part way and its definitions or step code
changed since, resume executes it again in place.  No new revision was
asked for, so the new version starts from the interrupted version's saved
start input: the same request and the same revision on the same base
version.  With nothing changed, resume continues the interrupted version.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting import CAPABILITY_FILE
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime import ledger_report as ledger_module
from manyselves.capabilities.distribution_reporting.runtime.ledger_report import (
    ledger_report_rerun_input,
    ledger_report_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import LedgerTools
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
    ReportRevision,
)
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.definitions import DefinitionKind
from manyselves.runtime import generic_binding
from manyselves.runtime.generic_binding import START_INPUT_FILE, GenericCapabilityRuntimeBinding
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from tests.capabilities.distribution_reporting.test_ledger_report import (
    _answer,
    _task,
    run_ledger_report,
)
from tests.capabilities.distribution_reporting.test_ledger_rerun import COMMAND, RUN_ID
from tests.runtime.scripted_loop import ScriptedLoop


def _any_fix(message):
    """``_answer``; a chief fix it was not written for changes nothing."""

    try:
        return _answer(message)
    except (KeyError, AssertionError):
        if _task(message.content) != "revise-chief-facts":
            raise
        return ("submit", [{}])


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(tmp_path: Path) -> Path:
    return tmp_path / "Work/runs" / RUN_ID


def _revision_values() -> dict[str, Any]:
    section = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    return {
        "instruction": f"改写 {section} 的措辞。",
        "changes": [{"target": section, "instruction": "语气更客观。", "scope": "text"}],
    }


async def _with_binding(tmp_path: Path, operation) -> tuple[GenericCapabilityRuntimeBinding, list[str]]:
    """Run ``operation(binding)`` on a fresh process with the real rerun input."""

    bus = MessageBus()
    loops: list[ScriptedLoop] = []

    def build(**kwargs: Any) -> ScriptedLoop:
        loop = ScriptedLoop(bus, _any_fix, kwargs)
        loops.append(loop)
        return loop

    real = ledger_report_rerun_input(tmp_path)

    def rerun_input(run_id, workflow_id, values, previous, archive):
        # The test starts report-ledger from "state"; the capability's rerun
        # input reads the request beside it.
        state = previous["state"]
        initial = real(
            run_id, workflow_id, values, {**previous, "request": state["request"]}, archive
        )
        return {**initial, "state": state}

    binding = GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=tmp_path,
        services=RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        tool_implementations=ledger_report_tool_implementations(
            tmp_path, input_snapshot=None, snapshot_content=None
        ),
        loop_builder=build,
        start_input=lambda run_id, workflow_id, values: {"state": values},
        rerun_input=rerun_input,
        kept_on_rerun=("preparation",),
    )
    binding.entrypoints = (*binding.entrypoints, "report-ledger")
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        await operation(binding)
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task
    return binding, [_task(m.content) for loop in loops for m in loop.received]


async def _interrupted_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault) -> dict:
    """Finish the report, start a revision of it and stop it in compose."""

    await run_ledger_report(tmp_path, run_id=RUN_ID)
    monkeypatch.setattr(LedgerTools, "compose_report", fault)
    # The revision executes detached: it stops failed, nothing is raised here.
    await _with_binding(tmp_path, lambda b: b.rerun(COMMAND, RUN_ID, _revision_values()))
    run_dir = _run_dir(tmp_path)
    started = _read(run_dir / START_INPUT_FILE)
    assert ReportRevision.model_validate(started["revision"]).base_version == 1
    assert _read(run_dir / "runtime-state.json")["status"] == "failed"
    return started


def _assert_same_revision_again(tmp_path: Path, binding, started: dict, reason: str) -> None:
    run_dir = _run_dir(tmp_path)
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed", binding.get_run(RUN_ID)[
        "state"
    ].get("error")
    # The interrupted revision is archived as v2 and executed again as v3.
    assert [row["version"] for row in binding.versions(RUN_ID)] == [1, 2]
    assert binding.versions(RUN_ID)[1]["reason"] == reason
    assert _read(run_dir / "versions/v2" / START_INPUT_FILE) == started
    now = _read(run_dir / START_INPUT_FILE)
    # Same request, same revision, same base version.
    assert now["revision"] == started["revision"]
    assert now["request"] == started["request"]
    assert ReportRevision.model_validate(now["revision"]).base_version == 1
    # The base version the revision rests on is still there, and the new
    # version is again a revision of it.
    assert (run_dir / "versions/v1/ledger/judgments.json").is_file()
    plan = _read(run_dir / "ledger/revision-plan.json")
    assert plan["base_version"] == 1


@pytest.mark.asyncio
async def test_revision_interrupted_then_definitions_changed_keeps_the_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LedgerTools.compose_report
    calls: list[int] = []

    def flaky(self, value):
        # A passing fault: the code of the step does not change.
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("compose: disk full")
        return original(self, value)

    started = await _interrupted_revision(tmp_path, monkeypatch, flaky)
    load = generic_binding.load_capability

    def changed(path):
        capability, registry = load(path)
        tasks = registry._definitions[DefinitionKind.TASK]
        task = tasks["write-section"]
        tasks["write-section"] = task.model_copy(
            update={"objective": task.objective + "\n每节先写结论。"}
        )
        return capability, registry

    monkeypatch.setattr(generic_binding, "load_capability", changed)
    binding, _tasks = await _with_binding(tmp_path, lambda b: b.resume(COMMAND, RUN_ID))

    _assert_same_revision_again(tmp_path, binding, started, "definitions changed")


@pytest.mark.asyncio
async def test_revision_interrupted_then_code_changed_keeps_the_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LedgerTools.compose_report

    def broken(self, value):
        raise RuntimeError("compose: bug")

    started = await _interrupted_revision(tmp_path, monkeypatch, broken)
    # The bug is fixed: compose's code is no longer what the run executed.
    monkeypatch.setattr(LedgerTools, "compose_report", original)
    binding, tasks = await _with_binding(tmp_path, lambda b: b.resume(COMMAND, RUN_ID))

    _assert_same_revision_again(tmp_path, binding, started, "code changed")
    # Every model turn of the revision is replayed from the memo.
    assert tasks == []


@pytest.mark.asyncio
async def test_revision_interrupted_with_nothing_changed_continues_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LedgerTools.compose_report
    calls: list[int] = []

    def flaky(self, value):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("compose: disk full")
        return original(self, value)

    started = await _interrupted_revision(tmp_path, monkeypatch, flaky)
    binding, tasks = await _with_binding(tmp_path, lambda b: b.resume(COMMAND, RUN_ID))

    run_dir = _run_dir(tmp_path)
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    # It went on from compose: no new version, nothing before it ran again.
    assert tasks == []
    assert [row["version"] for row in binding.versions(RUN_ID)] == [1]
    assert _read(run_dir / START_INPUT_FILE) == started
    last = _read(run_dir / "step-memo/last-execution.json")
    assert (last["skipped"], last["executed"]) == (0, 1)


def _archive(tmp_path: Path, number: int) -> Path:
    archive = _run_dir(tmp_path) / "versions" / f"v{number}"
    archive.mkdir(parents=True)
    return archive


def _request() -> dict[str, Any]:
    return ReportRequest(instruction="写一份完整报告。").model_dump(mode="json")


def test_rerun_input_without_values_keeps_the_saved_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_dir(tmp_path)
    # The interrupted revision froze new inputs; they stay where they are.
    (run_dir / "frozen-project").mkdir(parents=True)
    (run_dir / "frozen-project/table.txt").write_text("新资料", encoding="utf-8")
    (run_dir / "input-snapshot.json").write_text("{}", encoding="utf-8")
    _archive(tmp_path, 1)
    archive = _archive(tmp_path, 2)

    def no_freeze(self, run_id):
        raise AssertionError("inputs frozen again")

    monkeypatch.setattr(ledger_module.RunInputSnapshotStore, "freeze", no_freeze)
    revision = {**_revision_values(), "new_inputs": True, "base_version": 1}
    previous = {"request": _request(), "run-id": RUN_ID, "revision": revision}

    initial = ledger_report_rerun_input(tmp_path)(RUN_ID, "full-report", None, previous, archive)

    assert initial["request"] == _request() and initial["run-id"] == RUN_ID
    assert initial["revision"] == ReportRevision.model_validate(revision).model_dump(mode="json")
    assert initial["revision"]["base_version"] == 1
    assert (run_dir / "frozen-project/table.txt").read_text(encoding="utf-8") == "新资料"
    assert (run_dir / "input-snapshot.json").is_file()
    assert not (archive / "frozen-project").exists()


def test_rerun_input_without_values_or_saved_revision_is_the_request(tmp_path: Path) -> None:
    previous = {"request": _request(), "run-id": RUN_ID}

    initial = ledger_report_rerun_input(tmp_path)(
        RUN_ID, "full-report", None, previous, _archive(tmp_path, 1)
    )

    assert initial == {"request": _request(), "run-id": RUN_ID}


def test_new_revision_still_revises_the_version_just_archived(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 3)
    (archive / "ledger").mkdir()
    (archive / "ledger/judgments.json").write_text("[]", encoding="utf-8")
    old = {**_revision_values(), "base_version": 1}
    previous = {"request": _request(), "run-id": RUN_ID, "revision": old}
    values = {"instruction": "改第三章。", "changes": [{"target": "3", "instruction": "汇总更简洁。", "scope": "judgment"}]}

    initial = ledger_report_rerun_input(tmp_path)(RUN_ID, "full-report", values, previous, archive)

    assert initial["revision"]["base_version"] == 3
    assert initial["revision"]["instruction"] == "改第三章。"
