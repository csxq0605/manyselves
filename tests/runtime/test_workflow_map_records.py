"""Map, keyed record and conversation fork primitives on the generic Runtime."""

import time
from pathlib import Path
from typing import Any

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    CompilerError,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
)
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)


def _registry() -> tuple[DefinitionRegistry, dict[str, Any]]:
    registry = DefinitionRegistry()
    value = ContractDefinition(
        id="value",
        version="1.0.0",
        description="Any JSON value",
        adapter="json_schema",
        schema={},
    )
    record = ContractDefinition(
        id="record",
        version="1.0.0",
        description="One object record with an id",
        adapter="json_schema",
        schema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    )
    registry.register(value)
    registry.register(record)
    registry.register(
        ToolDefinition(
            id="score",
            version="1.0.0",
            description="Score one item",
            implementation="fixture:score",
            input_contract="value",
            output_contract="value",
        )
    )
    registry.register(
        AgentDefinition(
            id="reader",
            version="1.0.0",
            description="Neutral reader",
            instructions="Read.",
        )
    )
    contracts = {
        contract.id: build_contract_adapter(contract) for contract in (value, record)
    }
    return registry, contracts


def _item_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="score-item",
        version="1.0.0",
        description="Score one item",
        state={"item": None, "item_key": None, "factor": None},
        actions=[
            {
                "id": "score",
                "kind": "invoke_tool",
                "tool": "score",
                "input_variables": {
                    "item": "item",
                    "key": "item_key",
                    "factor": "factor",
                },
                "output_variable": "scored",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "scored"},
        ],
    )


def _map_parent(**map_overrides: Any) -> WorkflowDefinition:
    map_action = {
        "id": "score-all",
        "kind": "map",
        "items_variable": "items",
        "key_field": "id",
        "where": [{"field": "skip", "operator": "falsy"}],
        "workflow": "score-item",
        "key_input_variable": "item_key",
        "input_variables": {"factor": "factor"},
        "output_variable": "scores",
        "max_concurrency": 2,
    }
    map_action.update(map_overrides)
    return WorkflowDefinition(
        id="score-parent",
        version="1.0.0",
        description="Map over items",
        state={
            "factor": 10,
            "items": [
                {"id": "a", "n": 1},
                {"id": "b", "n": 2, "skip": True},
                {"id": "c", "n": 3},
            ],
        },
        actions=[
            map_action,
            {"id": "finish", "kind": "end_workflow", "output_variable": "scores"},
        ],
    )


def _compile(registry: DefinitionRegistry, workflow: WorkflowDefinition):
    registry.register(workflow)
    return WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


def _host(tmp_path: Path) -> tuple[WorkflowRuntimeHost, FileWorkflowStateStore]:
    store = FileWorkflowStateStore(tmp_path)
    return (
        WorkflowRuntimeHost(
            build_builtin_executor_registry(),
            store,
            InMemoryWorkflowEventSink(),
        ),
        store,
    )


@pytest.mark.asyncio
async def test_map_runs_one_child_per_selected_item_keyed_by_field(
    tmp_path: Path,
) -> None:
    registry, contracts = _registry()
    registry.register(_item_workflow())
    plan = _compile(registry, _map_parent())
    calls: list[str] = []

    def score(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(arguments["key"])
        return {"total": arguments["item"]["n"] * arguments["factor"]}

    host, _store = _host(tmp_path)
    completed = await host.execute(
        plan,
        WorkflowState.for_plan("map-run", plan),
        RuntimeContext(tools={"score": score}, contracts=contracts, definitions=registry),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs["result"] == {"a": {"total": 10}, "c": {"total": 30}}
    assert sorted(calls) == ["a", "c"]
    assert completed.map_states == {}


@pytest.mark.asyncio
async def test_map_lead_item_starts_before_the_others(tmp_path: Path) -> None:
    registry, contracts = _registry()
    registry.register(_item_workflow())
    plan = _compile(registry, _map_parent(lead_seconds=60))
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    def score(arguments: dict[str, Any]) -> dict[str, Any]:
        started[arguments["key"]] = time.monotonic()
        finished[arguments["key"]] = time.monotonic()
        return {"total": arguments["item"]["n"]}

    host, _store = _host(tmp_path)
    completed = await host.execute(
        plan,
        WorkflowState.for_plan("map-lead", plan),
        RuntimeContext(tools={"score": score}, contracts=contracts, definitions=registry),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    # The second item waited for the lead, which finished well within its head start.
    assert started["c"] >= finished["a"]
    assert started["c"] - started["a"] < 30


@pytest.mark.asyncio
async def test_map_records_item_errors_and_completes_other_items(
    tmp_path: Path,
) -> None:
    registry, contracts = _registry()
    registry.register(_item_workflow())
    plan = _compile(registry, _map_parent(errors_variable="score_errors"))

    def score(arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments["key"] == "a":
            raise RuntimeError("item a is unreadable")
        return {"total": arguments["item"]["n"]}

    host, _store = _host(tmp_path)
    completed = await host.execute(
        plan,
        WorkflowState.for_plan("map-errors", plan),
        RuntimeContext(tools={"score": score}, contracts=contracts, definitions=registry),
    )

    assert completed.outputs["result"] == {"c": {"total": 3}}
    assert list(completed.variables["score_errors"]) == ["a"]
    assert "item a is unreadable" in completed.variables["score_errors"]["a"]


@pytest.mark.asyncio
async def test_map_same_run_resume_reuses_completed_items(tmp_path: Path) -> None:
    registry, contracts = _registry()
    registry.register(_item_workflow())
    plan = _compile(registry, _map_parent(max_concurrency=1))
    calls: dict[str, int] = {"a": 0, "c": 0}

    def score(arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments["key"]
        calls[key] += 1
        if key == "c" and calls[key] == 1:
            raise RuntimeError("injected item failure")
        return {"total": arguments["item"]["n"]}

    host, store = _host(tmp_path)
    context = RuntimeContext(
        tools={"score": score},
        contracts=contracts,
        definitions=registry,
    )
    with pytest.raises(RuntimeError, match="injected item failure"):
        await host.execute(plan, WorkflowState.for_plan("map-resume", plan), context)

    failed = store.load("map-resume")
    assert failed.map_states["score-all"]["a"]["status"] == "completed"
    assert failed.map_states["score-all"]["c"]["status"] == "failed"

    completed = await host.execute(plan, failed, context)

    assert completed.outputs["result"] == {"a": {"total": 1}, "c": {"total": 3}}
    assert calls == {"a": 1, "c": 2}


@pytest.mark.asyncio
async def test_records_are_upserted_by_key_and_selected_by_condition(
    tmp_path: Path,
) -> None:
    registry, contracts = _registry()
    workflow = WorkflowDefinition(
        id="ledger",
        version="1.0.0",
        description="Keyed record table",
        state={
            "table": {"x": {"id": "x", "status": "open", "note": "first"}},
            "incoming": [
                {"id": "x", "status": "done"},
                {"id": "y", "status": "open"},
            ],
        },
        actions=[
            {
                "id": "upsert",
                "kind": "upsert_records",
                "table_variable": "table",
                "records_variable": "incoming",
                "key_field": "id",
                "merge": "update",
                "record_contract": "record",
            },
            {
                "id": "open-keys",
                "kind": "select_records",
                "table_variable": "table",
                "where": [{"field": "status", "operator": "eq", "value": "open"}],
                "output": "keys",
                "output_variable": "open_keys",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "table",
            },
        ],
    )
    plan = _compile(registry, workflow)
    host, _store = _host(tmp_path)

    completed = await host.execute(
        plan,
        WorkflowState.for_plan("ledger-run", plan),
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert completed.outputs["result"] == {
        "x": {"id": "x", "status": "done", "note": "first"},
        "y": {"id": "y", "status": "open"},
    }
    assert completed.variables["open_keys"] == ["y"]


@pytest.mark.asyncio
async def test_record_contract_rejects_invalid_records(tmp_path: Path) -> None:
    registry, contracts = _registry()
    workflow = WorkflowDefinition(
        id="bad-ledger",
        version="1.0.0",
        description="Invalid record",
        state={"table": {}, "incoming": [{"status": "open"}]},
        actions=[
            {
                "id": "upsert",
                "kind": "upsert_records",
                "table_variable": "table",
                "records_variable": "incoming",
                "record_contract": "record",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "table"},
        ],
    )
    plan = _compile(registry, workflow)
    host, _store = _host(tmp_path)

    with pytest.raises(RuntimeError):
        await host.execute(
            plan,
            WorkflowState.for_plan("bad-ledger-run", plan),
            RuntimeContext(contracts=contracts, definitions=registry),
        )


@pytest.mark.asyncio
async def test_fork_inside_map_gives_each_item_its_own_child_conversation(
    tmp_path: Path,
) -> None:
    registry, contracts = _registry()
    child = WorkflowDefinition(
        id="fork-item",
        version="1.0.0",
        description="Fork one conversation per item",
        state={"item": None, "item_key": None, "shared": None},
        actions=[
            {
                "id": "fork",
                "kind": "fork_conversation",
                "source_variable": "shared",
                "conversation_key": "item-reader",
                "key_variable": "item_key",
                "output_variable": "conversation",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "conversation",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="fork-parent",
        version="1.0.0",
        description="Shared prefix, then one fork per item",
        state={"items": {"p": {"n": 1}, "q": {"n": 2}}},
        actions=[
            {
                "id": "shared",
                "kind": "create_conversation",
                "agent": "reader",
                "conversation_key": "shared-context",
                "output_variable": "shared",
            },
            {
                "id": "fan-out",
                "kind": "map",
                "items_variable": "items",
                "workflow": "fork-item",
                "key_input_variable": "item_key",
                "input_variables": {"shared": "shared"},
                "output_variable": "forks",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "forks"},
        ],
    )
    registry.register(child)
    plan = _compile(registry, parent)
    host, _store = _host(tmp_path)

    completed = await host.execute(
        plan,
        WorkflowState.for_plan("fork-run", plan),
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    shared_id = completed.variables["shared"].conversation_id
    forks = completed.outputs["result"]
    assert set(forks) == {"p", "q"}
    ids = {key: value["conversation_id"] for key, value in forks.items()}
    assert len(set(ids.values())) == 2
    assert all(value.endswith(f"item-reader:{key}") for key, value in ids.items())
    assert {value["forked_from"] for value in forks.values()} == {shared_id}
    assert {value["key"]["agent_id"] for value in forks.values()} == {"reader"}


def test_compiler_rejects_map_reading_undefined_items() -> None:
    registry, _contracts = _registry()
    registry.register(_item_workflow())
    parent = _map_parent(items_variable="missing")
    registry.register(parent)

    with pytest.raises(CompilerError, match="undefined variable: missing"):
        WorkflowCompiler(build_builtin_executor_registry()).compile(parent, registry)


def test_compiler_rejects_map_inputs_overwriting_the_item() -> None:
    registry, _contracts = _registry()
    registry.register(_item_workflow())
    parent = _map_parent(input_variables={"item": "factor"})
    registry.register(parent)

    with pytest.raises(CompilerError, match="overwrite the item"):
        WorkflowCompiler(build_builtin_executor_registry()).compile(parent, registry)


def test_map_child_plan_is_frozen_into_the_parent_plan() -> None:
    registry, _contracts = _registry()
    registry.register(_item_workflow())
    plan = _compile(registry, _map_parent())

    assert "score-item" in plan.workflow_ids
    assert "score-item" in plan.subworkflow_plans
    assert "workflow:score-item" in plan.definition_snapshots


@pytest.mark.asyncio
async def test_set_variable_copies_a_field_of_another_variable(tmp_path: Path) -> None:
    registry, contracts = _registry()
    workflow = WorkflowDefinition(
        id="copy-field",
        version="1.0.0",
        description="Copy one field",
        state={"request": {"inner": {"items": [1, 2]}}},
        actions=[
            {
                "id": "copy",
                "kind": "set_variable",
                "variable": "items",
                "value_variable": "request",
                "field": "inner.items",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "items"},
        ],
    )
    plan = _compile(registry, workflow)
    host, _store = _host(tmp_path)

    completed = await host.execute(
        plan,
        WorkflowState.for_plan("copy-run", plan),
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert completed.outputs["result"] == [1, 2]


@pytest.mark.asyncio
async def test_map_keeps_child_conversations_so_a_later_map_resolves_them(
    tmp_path: Path,
) -> None:
    """Forks made inside a Map survive into the parent state.

    A later Map, run with a fresh conversation registry as after a restart,
    resolves each item's own conversation by key.
    """

    registry, contracts = _registry()
    registry.register(
        WorkflowDefinition(
            id="fork-item",
            version="1.0.0",
            description="Fork one conversation per item",
            state={"item": None, "item_key": None, "shared": None},
            actions=[
                {
                    "id": "fork",
                    "kind": "fork_conversation",
                    "source_variable": "shared",
                    "conversation_key": "item-reader",
                    "key_variable": "item_key",
                    "output_variable": "conversation",
                },
                {"id": "finish", "kind": "end_workflow", "output_variable": "conversation"},
            ],
        )
    )
    registry.register(
        WorkflowDefinition(
            id="resolve-item",
            version="1.0.0",
            description="Resolve the item's own conversation",
            state={"item": None, "item_key": None},
            actions=[
                {
                    "id": "resolve",
                    "kind": "resolve_conversation",
                    "agent": "reader",
                    "conversation_key": "item-reader",
                    "key_variable": "item_key",
                    "output_variable": "conversation",
                },
                {"id": "finish", "kind": "end_workflow", "output_variable": "conversation"},
            ],
        )
    )
    first = _compile(
        registry,
        WorkflowDefinition(
            id="fork-parent",
            version="1.0.0",
            description="Fork one conversation per item",
            state={"items": {"p": {"n": 1}, "q": {"n": 2}}},
            actions=[
                {
                    "id": "shared",
                    "kind": "create_conversation",
                    "agent": "reader",
                    "conversation_key": "shared-context",
                    "output_variable": "shared",
                },
                {
                    "id": "fan-out",
                    "kind": "map",
                    "items_variable": "items",
                    "workflow": "fork-item",
                    "key_input_variable": "item_key",
                    "input_variables": {"shared": "shared"},
                    "output_variable": "forks",
                },
                {"id": "finish", "kind": "end_workflow", "output_variable": "forks"},
            ],
        ),
    )
    host, _store = _host(tmp_path)
    forked = await host.execute(
        first,
        WorkflowState.for_plan("resume-run", first),
        RuntimeContext(contracts=contracts, definitions=registry),
    )
    assert {"fan-out[p].conversation", "fan-out[q].conversation"} <= set(
        forked.conversations
    )

    second = _compile(
        registry,
        WorkflowDefinition(
            id="resolve-parent",
            version="1.0.0",
            description="Resolve each item's conversation",
            state={"items": {"p": {"n": 1}, "q": {"n": 2}}},
            actions=[
                {
                    "id": "fan-in",
                    "kind": "map",
                    "items_variable": "items",
                    "workflow": "resolve-item",
                    "key_input_variable": "item_key",
                    "output_variable": "resolved",
                },
                {"id": "finish", "kind": "end_workflow", "output_variable": "resolved"},
            ],
        ),
    )
    state = WorkflowState.for_plan("resume-run", second)
    state.conversations = forked.conversations
    restarted_host, _restarted_store = _host(tmp_path / "restarted")
    resolved = await restarted_host.execute(
        second,
        state,
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert {
        key: value["conversation_id"] for key, value in resolved.outputs["result"].items()
    } == {key: value["conversation_id"] for key, value in forked.outputs["result"].items()}


@pytest.mark.asyncio
async def test_set_variable_builds_an_object_from_variables(tmp_path: Path) -> None:
    registry, contracts = _registry()
    workflow = WorkflowDefinition(
        id="build-object",
        version="1.0.0",
        description="Build one object",
        state={"left": 1, "right": {"n": 2}},
        actions=[
            {
                "id": "build",
                "kind": "set_variable",
                "variable": "pair",
                "value_variables": {"a": "left", "b": "right"},
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "pair"},
        ],
    )
    plan = _compile(registry, workflow)
    host, _store = _host(tmp_path)

    completed = await host.execute(
        plan,
        WorkflowState.for_plan("build-run", plan),
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert completed.outputs["result"] == {"a": 1, "b": {"n": 2}}
