"""Each archived version keeps the start input it was executed from.

A rerun in place moves the previous version's files to ``versions/v<n>``
and writes the new version's start input over the current one.  The
archived version keeps its own copy of the start input as it was, so a
revision passed to an earlier version can still be read after a later one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

# The neutral second Capability: one Tool, no report concept.
from manyselves.capabilities.parameter_adjustment import CAPABILITY_FILE
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.capability_binding import CapabilityRunInputError
from manyselves.runtime.generic_binding import START_INPUT_FILE, GenericCapabilityRuntimeBinding
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView

COMMAND = UUID("70000000-0000-4000-8000-000000000001")
WORKFLOW = "parameter-adjustment"


def _rerun_input(run_id, workflow_id, values, previous, archive):
    del run_id, workflow_id
    if values.get("reject"):
        raise ValueError("revision rejected")
    return {
        "parameters": previous["parameters"],
        "revision": {**values, "base_version": int(archive.name[1:])},
    }


async def _with_binding(tmp_path: Path, operation) -> GenericCapabilityRuntimeBinding:
    bus = MessageBus()
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
        tool_implementations={"normalize-parameter": lambda arguments: arguments["value"]},
        rerun_input=_rerun_input,
    )
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        await operation(binding)
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task
    return binding


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_each_archived_version_keeps_the_start_input_it_ran_from(tmp_path: Path) -> None:
    run_id = f"{WORKFLOW}-{COMMAND.hex}"
    # A value of 12 needs no model turn: the plan runs its tool and ends.
    await _with_binding(tmp_path, lambda b: b.start(COMMAND, WORKFLOW, {"value": 12}))
    first = {"instruction": "第一次修订", "changes": [{"target": "A", "scope": "text"}]}
    second = {"instruction": "第二次修订", "changes": [{"target": "B", "scope": "judgment"}]}
    await _with_binding(tmp_path, lambda b: b.rerun(COMMAND, run_id, first))
    binding = await _with_binding(tmp_path, lambda b: b.rerun(COMMAND, run_id, second))

    run_dir = tmp_path / "Work/runs" / run_id
    assert binding.get_run(run_id)["run"]["status"] == "completed"
    assert [row["version"] for row in binding.versions(run_id)] == [1, 2]
    # The first version ran from the start input alone.
    assert _read(run_dir / "versions/v1" / START_INPUT_FILE) == {"parameters": {"value": 12}}
    # The second version's archive holds the revision it was executed with.
    assert _read(run_dir / "versions/v2" / START_INPUT_FILE) == {
        "parameters": {"value": 12},
        "revision": {**first, "base_version": 1},
    }
    # The current version reads this revision, from its start input and its state.
    assert _read(run_dir / START_INPUT_FILE)["revision"] == {**second, "base_version": 2}
    assert binding.get_run(run_id)["state"]["variables"]["revision"] == {
        **second,
        "base_version": 2,
    }
    # The archived state of each version agrees with its archived start input.
    archived_state = _read(run_dir / "versions/v2/runtime-state.json")
    assert archived_state["variables"]["revision"] == {**first, "base_version": 1}


@pytest.mark.asyncio
async def test_rejected_revision_leaves_the_current_start_input_and_no_archive(
    tmp_path: Path,
) -> None:
    run_id = f"{WORKFLOW}-{COMMAND.hex}"
    await _with_binding(tmp_path, lambda b: b.start(COMMAND, WORKFLOW, {"value": 12}))
    first = {"instruction": "第一次修订"}
    await _with_binding(tmp_path, lambda b: b.rerun(COMMAND, run_id, first))
    run_dir = tmp_path / "Work/runs" / run_id
    before = (run_dir / START_INPUT_FILE).read_bytes()

    async def rejected(binding: GenericCapabilityRuntimeBinding) -> None:
        with pytest.raises(CapabilityRunInputError, match="revision rejected"):
            await binding.rerun(COMMAND, run_id, {"reject": True})

    binding = await _with_binding(tmp_path, rejected)

    assert (run_dir / START_INPUT_FILE).read_bytes() == before
    assert [row["version"] for row in binding.versions(run_id)] == [1]
    assert not (run_dir / "versions/v2").exists()
    assert binding.get_run(run_id)["run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_rejected_revision_never_moves_the_current_start_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring a rejected revision leaves the start input file itself alone.

    The archive holds a copy of it; the restore drops that copy with the
    archive instead of putting it back, so the file in the run directory is
    never removed, not even for a moment, just as before the archive kept a
    copy of it.
    """

    import manyselves.runtime.generic_binding as generic_binding

    run_id = f"{WORKFLOW}-{COMMAND.hex}"
    await _with_binding(tmp_path, lambda b: b.start(COMMAND, WORKFLOW, {"value": 12}))
    run_dir = tmp_path / "Work/runs" / run_id
    path = run_dir / START_INPUT_FILE
    before = path.read_bytes()
    stat_before = path.stat()

    real_move = generic_binding.shutil.move

    def move(source: str, destination: str) -> Any:
        # Any move of a start input during the restore fails here.
        if Path(source).name == START_INPUT_FILE:
            raise OSError("start input moved")
        return real_move(source, destination)

    monkeypatch.setattr(generic_binding.shutil, "move", move)

    async def rejected(binding: GenericCapabilityRuntimeBinding) -> None:
        with pytest.raises(CapabilityRunInputError, match="revision rejected"):
            await binding.rerun(COMMAND, run_id, {"reject": True})

    binding = await _with_binding(tmp_path, rejected)

    assert path.read_bytes() == before
    stat_now = path.stat()
    assert (stat_now.st_ino, stat_now.st_mtime_ns) == (
        stat_before.st_ino,
        stat_before.st_mtime_ns,
    )
    assert binding.versions(run_id) == []
    assert not (run_dir / "versions/v1").exists()
    assert binding.get_run(run_id)["run"]["status"] == "completed"
