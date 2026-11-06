"""Same-version reuse of a model result checks the data the call read.

A completed result is taken again for the same conversation, step and input
only while what the call read still holds: the content of the images its
request carried and, when it called Tools, the files those Tools could read.
Only what a Tool declared ``pure_read`` reads is covered by that check.  A
record that does not say what was read, whose reads cannot be checked, or
whose call used a Tool with any other declaration, is not taken: the step runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    TaskDefinition,
    ToolDefinition,
)
from manyselves.runtime.agent_results import FileAgentResultStore
from manyselves.runtime.definition_agent import DefinitionAgentInvoker
from manyselves.runtime.step_memo import RunStepMemo
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from tests.runtime.test_definition_agent import (
    POLICY,
    RUN_ID,
    Harness,
    _conversation,
    _run,
    _task,
)

# Where an "ask" step of task "answer-question" keeps its result.
STORE_KEY = "ask--answer-question"


def _image_task(*, listed: bool = False, tools: list[str] | None = None) -> TaskDefinition:
    image = (
        {"list_field": "photos", "path_field": "path", "media_type_field": "media"}
        if listed
        else {"path_field": "photo", "media_type_field": "media"}
    )
    return TaskDefinition.model_validate(
        {**_task(tools=tools).model_dump(), "image": image}
    )


async def _ask(harness: Harness, task: TaskDefinition, value: Any, **options: Any):
    invoker = harness.invoker(**options)
    try:
        return await invoker.invoke_with_recovery(
            harness.agent, task, value, _conversation("a"),
            task_id="ask", recovery_policy=POLICY,
        )
    finally:
        await invoker.close()


def _answers(*answers: str) -> list[Any]:
    return [("submit", [{"answer": answer}]) for answer in answers]


def _tool_answers(*answers: str) -> list[Any]:
    return [("tool_then_submit", {"text": "a"}, {"answer": answer}) for answer in answers]


class ReadingHarness(Harness):
    """A Harness whose ``lookup`` Tool reads a workspace file the model names."""

    def __init__(self, tmp_path: Path, script: list[Any]) -> None:
        super().__init__(tmp_path, script, [])
        self.reads: list[str] = []

        def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
            path = tmp_path / "data" / f"{arguments['text']}.txt"
            self.reads.append(path.name)
            return {"answer": path.read_text(encoding="utf-8")}

        self.factory = CapabilityToolAdapterFactory(
            "neutral", {"lookup": lookup}, self.contracts
        )

    def invoker(self, *, basis: bool = True) -> DefinitionAgentInvoker:  # type: ignore[override]
        invoker = super().invoker()
        if basis:
            memo = RunStepMemo(self.tmp_path, RUN_ID, replay=False)
            invoker.read_basis = memo.read_basis
        return invoker


# -- characterization: what reuse did before reads were checked ---------------


@pytest.mark.asyncio
async def test_the_same_input_again_in_the_same_conversation_is_reused(
    tmp_path: Path,
) -> None:
    """A loop that reaches the same step with the same input takes its result."""

    harness = Harness(tmp_path, _answers("first", "other"), [])

    async def steps():
        invoker = harness.invoker()
        try:
            outcomes = [
                await invoker.invoke_with_recovery(
                    harness.agent, _task(), {"text": "q"}, _conversation("a"),
                    task_id="ask", recovery_policy=POLICY,
                )
                for _ in range(2)
            ]
        finally:
            await invoker.close()
        return outcomes

    first, again = await _run(harness, steps)

    assert first.result == again.result == {"answer": "first"}
    assert len(harness.loops[0].received) == 1


@pytest.mark.asyncio
async def test_an_unchanged_image_task_is_reused_on_resume(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos/a.png").write_bytes(b"png")
    harness = Harness(tmp_path, _answers("seen", "again"), [])
    value = {"text": "q", "photo": "photos/a.png", "media": "image/png"}

    async def steps():
        return await _ask(harness, _image_task(), value), await _ask(
            harness, _image_task(), value
        )

    first, again = await _run(harness, steps)

    assert first.result == again.result == {"answer": "seen"}
    assert len(harness.loops) == 1


# -- what the call read decides whether its result is taken again -------------


@pytest.mark.asyncio
async def test_an_image_changed_at_the_same_path_runs_the_call_again(
    tmp_path: Path,
) -> None:
    (tmp_path / "photos").mkdir()
    photo = tmp_path / "photos/a.png"
    photo.write_bytes(b"old")
    harness = Harness(tmp_path, _answers("old view", "new view"), [])
    value = {"text": "q", "photo": "photos/a.png", "media": "image/png"}

    async def steps():
        first = await _ask(harness, _image_task(), value)
        photo.write_bytes(b"new")
        return first, await _ask(harness, _image_task(), value)

    first, again = await _run(harness, steps)

    assert first.result == {"answer": "old view"}
    assert again.result == {"answer": "new view"}
    assert len(harness.loops) == 2


@pytest.mark.asyncio
async def test_an_image_rewritten_with_the_same_content_is_still_reused(
    tmp_path: Path,
) -> None:
    (tmp_path / "photos").mkdir()
    photo = tmp_path / "photos/a.png"
    photo.write_bytes(b"same")
    harness = Harness(tmp_path, _answers("seen", "again"), [])
    value = {"text": "q", "photo": "photos/a.png", "media": "image/png"}

    async def steps():
        first = await _ask(harness, _image_task(), value)
        photo.unlink()
        photo.write_bytes(b"same")
        return first, await _ask(harness, _image_task(), value)

    first, again = await _run(harness, steps)

    assert first.result == again.result == {"answer": "seen"}
    assert len(harness.loops) == 1


@pytest.mark.asyncio
async def test_a_file_a_tool_read_changed_at_the_same_path_runs_the_call_again(
    tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    source = tmp_path / "data/a.txt"
    source.write_text("old", encoding="utf-8")
    harness = ReadingHarness(tmp_path, _tool_answers("from old", "from new"))
    task = _task(tools=["lookup"])

    async def steps():
        first = await _ask(harness, task, {"text": "q"})
        unchanged = await _ask(harness, task, {"text": "q"})
        source.write_text("new!", encoding="utf-8")
        return first, unchanged, await _ask(harness, task, {"text": "q"})

    first, unchanged, changed = await _run(harness, steps)

    # The file is the same: the result is taken and the Tool does not run.
    assert first.result == unchanged.result == {"answer": "from old"}
    # The file changed: the call runs again and its Tool reads the new content.
    assert changed.result == {"answer": "from new"}
    assert len(harness.loops) == 2
    assert harness.reads == ["a.txt", "a.txt"]
    assert harness.loops[1].feedback[0] == {"answer": "new!"}


@pytest.mark.asyncio
async def test_a_tool_task_whose_model_called_no_tool_depends_only_on_its_input(
    tmp_path: Path,
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/a.txt").write_text("old", encoding="utf-8")
    harness = ReadingHarness(tmp_path, _answers("no tool", "again"))
    task = _task(tools=["lookup"])

    async def steps():
        first = await _ask(harness, task, {"text": "q"})
        (tmp_path / "data/a.txt").write_text("new!", encoding="utf-8")
        return first, await _ask(harness, task, {"text": "q"})

    first, again = await _run(harness, steps)

    assert first.result == again.result == {"answer": "no tool"}
    assert len(harness.loops) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, "photos/b.jpg", "data/a.txt"])
async def test_every_source_the_call_read_must_hold(
    tmp_path: Path, changed: str | None
) -> None:
    """Two images and a Tool file: a change in any one of them runs the call."""

    (tmp_path / "photos").mkdir()
    (tmp_path / "data").mkdir()
    for name, content in (("photos/a.png", b"a"), ("photos/b.jpg", b"b")):
        (tmp_path / name).write_bytes(content)
    (tmp_path / "data/a.txt").write_text("text", encoding="utf-8")
    harness = ReadingHarness(tmp_path, _tool_answers("first", "second"))
    task = _image_task(listed=True, tools=["lookup"])
    value = {
        "text": "q",
        "photos": [
            {"path": "photos/a.png", "media": "image/png"},
            {"path": "photos/b.jpg", "media": "image/jpeg"},
        ],
    }

    async def steps():
        first = await _ask(harness, task, value)
        if changed is not None:
            (tmp_path / changed).write_bytes(b"changed")
        return first, await _ask(harness, task, value)

    first, again = await _run(harness, steps)

    assert first.result == {"answer": "first"}
    if changed is None:
        assert again.result == {"answer": "first"} and len(harness.loops) == 1
    else:
        assert again.result == {"answer": "second"} and len(harness.loops) == 2
    stored = FileAgentResultStore(tmp_path, RUN_ID)
    path = stored.path(_conversation("a").conversation_id, STORE_KEY)
    reads = json.loads(path.read_text(encoding="utf-8"))["reads"]
    assert [image["path"] for image in reads["images"]] == ["photos/a.png", "photos/b.jpg"]
    assert reads["tools"] == ["lookup"] and isinstance(reads["tool_basis"], str)


@pytest.mark.asyncio
async def test_a_record_that_does_not_say_what_was_read_runs_the_call(
    tmp_path: Path,
) -> None:
    """A result saved before reads were recorded cannot be checked."""

    harness = Harness(tmp_path, _answers("first", "second"), [])

    async def steps():
        first = await _ask(harness, _task(), {"text": "q"})
        path = FileAgentResultStore(tmp_path, RUN_ID).path(
            _conversation("a").conversation_id, STORE_KEY
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["reads"]
        path.write_text(json.dumps(record), encoding="utf-8")
        return first, await _ask(harness, _task(), {"text": "q"})

    first, again = await _run(harness, steps)

    assert first.result == {"answer": "first"}
    assert again.result == {"answer": "second"}


@pytest.mark.asyncio
async def test_tool_reads_without_a_basis_to_check_run_the_call(tmp_path: Path) -> None:
    """No basis for the files a Tool could read: nothing tells they did not change."""

    (tmp_path / "data").mkdir()
    (tmp_path / "data/a.txt").write_text("text", encoding="utf-8")
    harness = ReadingHarness(tmp_path, _tool_answers("first", "second", "third"))
    task = _task(tools=["lookup"])

    async def steps():
        unchecked = await _ask(harness, task, {"text": "q"}, basis=False)
        # Recorded without a basis: even a checking invoker cannot take it.
        recorded = await _ask(harness, task, {"text": "q"})
        # Recorded with a basis, but this invoker has none to compare with.
        return unchecked, recorded, await _ask(harness, task, {"text": "q"}, basis=False)

    outcomes = await _run(harness, steps)

    assert [o.result["answer"] for o in outcomes] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_a_missing_image_is_not_taken_as_unchanged(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    photo = tmp_path / "photos/a.png"
    photo.write_bytes(b"png")
    harness = Harness(tmp_path, _answers("seen", "again"), [])
    value = {"text": "q", "photo": "photos/a.png", "media": "image/png"}

    async def steps():
        first = await _ask(harness, _image_task(), value)
        photo.unlink()
        return first, await _ask(harness, _image_task(), value)

    first, again = await _run(harness, steps)

    assert first.result == {"answer": "seen"}
    assert again.status == "failed" and "not a workspace file" in (again.error or "")


class DeclaredHarness(Harness):
    """A Harness whose ``lookup`` Tool has the given ``side_effect``.

    It returns a value held outside the workspace, as a Tool reading external
    data would; ``reuse_result`` is off so every call reaches it.
    """

    def __init__(self, tmp_path: Path, script: list[Any], side_effect: str) -> None:
        super().__init__(tmp_path, script, [])
        self.remote = {"value": "v1"}
        self.calls = 0
        registry = DefinitionRegistry()
        for definition in self.registry.all():
            if definition.kind == DefinitionKind.TOOL and definition.id == "lookup":
                definition = ToolDefinition.model_validate(
                    {
                        **definition.model_dump(),
                        "side_effect": side_effect,
                        "reuse_result": False,
                    }
                )
            registry.register(definition)
        self.registry = registry

        def lookup(arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            return {"answer": self.remote["value"]}

        self.factory = CapabilityToolAdapterFactory(
            "neutral", {"lookup": lookup}, self.contracts
        )

    def invoker(self) -> DefinitionAgentInvoker:  # type: ignore[override]
        invoker = super().invoker()
        invoker.read_basis = RunStepMemo(self.tmp_path, RUN_ID, replay=False).read_basis
        return invoker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect", ["external_network", "ordered_state", "shared_write", "run_local_write"]
)
async def test_a_call_whose_tool_reads_beyond_the_basis_runs_again(
    tmp_path: Path, side_effect: str
) -> None:
    """A Tool not declared ``pure_read`` read what the basis cannot check.

    External data changed while no workspace file did: the call runs again
    and its Tool reads the new value.
    """

    harness = DeclaredHarness(tmp_path, _tool_answers("from v1", "from v2"), side_effect)
    task = _task(tools=["lookup"])

    async def steps():
        first = await _ask(harness, task, {"text": "q"})
        harness.remote["value"] = "v2"
        return first, await _ask(harness, task, {"text": "q"})

    first, again = await _run(harness, steps)

    assert first.result == {"answer": "from v1"}
    assert again.result == {"answer": "from v2"}
    assert harness.calls == 2 and len(harness.loops) == 2
    assert harness.loops[1].feedback[0] == {"answer": "v2"}
    path = FileAgentResultStore(tmp_path, RUN_ID).path(
        _conversation("a").conversation_id, STORE_KEY
    )
    reads = json.loads(path.read_text(encoding="utf-8"))["reads"]
    assert reads["tools"] == ["lookup"] and reads["unverifiable"] == ["lookup"]


@pytest.mark.asyncio
async def test_a_pure_read_tool_call_is_checked_by_the_basis(tmp_path: Path) -> None:
    """The same Tool declared ``pure_read``: the basis held, so the result is taken."""

    harness = DeclaredHarness(tmp_path, _tool_answers("from v1", "from v2"), "pure_read")
    task = _task(tools=["lookup"])

    async def steps():
        return await _ask(harness, task, {"text": "q"}), await _ask(harness, task, {"text": "q"})

    first, again = await _run(harness, steps)

    assert first.result == again.result == {"answer": "from v1"}
    assert harness.calls == 1 and len(harness.loops) == 1
