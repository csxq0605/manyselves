import pytest

from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    GateDefinition,
    InteractionDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    RuntimeExecutionError,
    build_builtin_executor_registry,
)
from manyselves.kernel.workflow import (
    CompilerError,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    restore_plan_definition_registry,
    resume_waiting_input,
)
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost


def _contract(contract_id: str, schema_type: str) -> ContractDefinition:
    return ContractDefinition(
        id=contract_id,
        version="1.0.0",
        description=contract_id,
        adapter="json_schema",
        schema={"type": schema_type},
    )


def test_resolved_plan_freezes_recovery_and_conversation_bindings() -> None:
    registry = DefinitionRegistry()
    contract = _contract("text", "string")
    recovery = RecoveryPolicyDefinition(
        id="standard-recovery",
        version="1.0.0",
        description="Neutral recovery policy",
        rules={},
    )
    agent = AgentDefinition(
        id="writer",
        version="1.0.0",
        description="Neutral writer",
        instructions="Return the input.",
        tools=["lookup"],
        accepts=[contract.id],
        produces=[contract.id],
    )
    task = TaskDefinition(
        id="write",
        version="1.0.0",
        description="Write text",
        agent=agent.id,
        objective="Return text",
        input_contract=contract.id,
        output_contract=contract.id,
        tools=["lookup"],
        recovery=recovery.id,
    )
    tool = ToolDefinition(
        id="lookup",
        version="1.0.0",
        description="Neutral lookup",
        implementation="capability:neutral:lookup",
        input_contract=contract.id,
        output_contract=contract.id,
    )
    for definition in (contract, recovery, tool, agent, task):
        registry.register(definition)
    workflow = WorkflowDefinition(
        id="conversation-plan",
        version="1.0.0",
        description="Freeze runtime references",
        recovery=[recovery.id],
        state={"input": "hello"},
        actions=[
            {
                "id": "conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "writer-session",
                "mode": "run",
                "output_variable": "conversation",
            },
            {
                "id": "invoke",
                "kind": "invoke_agent",
                "agent": agent.id,
                "task": task.id,
                "conversation_variable": "conversation",
                "input_variable": "input",
                "output_variable": "result",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)

    assert plan.recovery_ids == [recovery.id]
    assert plan.agent_tool_ids == [tool.id]
    assert plan.tool_implementations == {tool.id: tool.implementation}
    assert plan.control_flow_edges == {
        "conversation": ["invoke"],
        "invoke": ["finish"],
        "finish": [],
    }
    assert plan.conversation_bindings == {
        "conversation": {
            "agent": agent.id,
            "conversation_key": "writer-session",
            "mode": "run",
            "output_variable": "conversation",
        }
    }
    registry._definitions["task"][task.id] = task.model_copy(
        update={"objective": "Changed after compile"}
    )
    restored = restore_plan_definition_registry(plan)
    assert restored.require("task", task.id).objective == "Return text"
    assert restored.require("contract", contract.id) == contract


def test_compiler_rejects_incompatible_declared_contract_flow() -> None:
    registry = DefinitionRegistry()
    number = _contract("number", "integer")
    text = _contract("text", "string")
    producer = ToolDefinition(
        id="produce-number",
        version="1.0.0",
        description="Produce a number",
        implementation="tests:produce-number",
        input_contract=number.id,
        output_contract=number.id,
    )
    consumer = ToolDefinition(
        id="consume-text",
        version="1.0.0",
        description="Consume text",
        implementation="tests:consume-text",
        input_contract=text.id,
        output_contract=text.id,
    )
    for definition in (number, text, producer, consumer):
        registry.register(definition)
    workflow = WorkflowDefinition(
        id="invalid-contract-flow",
        version="1.0.0",
        description="Wire incompatible declared contracts",
        state={"seed": 1},
        actions=[
            {
                "id": "produce",
                "kind": "invoke_tool",
                "tool": producer.id,
                "input_variable": "seed",
                "output_variable": "number_result",
            },
            {
                "id": "consume",
                "kind": "invoke_tool",
                "tool": consumer.id,
                "input_variable": "number_result",
                "output_variable": "text_result",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "text_result",
            },
        ],
    )

    with pytest.raises(
        CompilerError,
        match="contract number is not assignable to text for action consume",
    ):
        WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


def test_compiler_rejects_a_reachable_non_terminal_control_flow_exit() -> None:
    registry = DefinitionRegistry()
    workflow = WorkflowDefinition(
        id="missing-terminal-branch",
        version="1.0.0",
        description="Every reachable branch must end explicitly",
        state={"choose_end": False, "result": 1},
        actions=[
            {
                "id": "choose",
                "kind": "if",
                "condition": {"variable": "choose_end", "operator": "truthy"},
                "then": "finish",
                "otherwise": "fall-off",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            {
                "id": "fall-off",
                "kind": "set_variable",
                "variable": "result",
                "value": 2,
            },
        ],
    )

    with pytest.raises(CompilerError, match="reachable action fall-off has no explicit terminal"):
        WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


@pytest.mark.asyncio
async def test_end_workflow_validates_the_declared_final_output_contract(tmp_path) -> None:
    registry = DefinitionRegistry()
    number = _contract("number", "integer")
    registry.register(number)
    workflow = WorkflowDefinition(
        id="invalid-final-output",
        version="1.0.0",
        description="Runtime validates untyped state at the final boundary",
        output_contract=number.id,
        state={"result": "not-an-integer"},
        actions=[
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)

    with pytest.raises(RuntimeExecutionError, match="final output contract"):
        await WorkflowRuntimeHost(
            executors,
            FileWorkflowStateStore(tmp_path),
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan("invalid-final-output-run", plan),
            RuntimeContext(
                contracts=build_contract_catalog(registry),
                definitions=registry,
            ),
        )


def test_compiler_rejects_a_conversation_bound_to_a_different_agent() -> None:
    registry = DefinitionRegistry()
    contract = _contract("text", "string")
    writer = AgentDefinition(
        id="writer",
        version="1.0.0",
        description="Writer",
        instructions="Write.",
    )
    reviewer = AgentDefinition(
        id="reviewer",
        version="1.0.0",
        description="Reviewer",
        instructions="Review.",
        accepts=[contract.id],
        produces=[contract.id],
    )
    task = TaskDefinition(
        id="review",
        version="1.0.0",
        description="Review text",
        agent=reviewer.id,
        objective="Review text",
        input_contract=contract.id,
        output_contract=contract.id,
    )
    for definition in (contract, writer, reviewer, task):
        registry.register(definition)
    workflow = WorkflowDefinition(
        id="invalid-conversation-agent",
        version="1.0.0",
        description="Do not cross agent conversation identities",
        state={"input": "hello"},
        actions=[
            {
                "id": "conversation",
                "kind": "create_conversation",
                "agent": writer.id,
                "conversation_key": "writer-session",
                "mode": "run",
                "output_variable": "conversation",
            },
            {
                "id": "invoke",
                "kind": "invoke_agent",
                "agent": reviewer.id,
                "task": task.id,
                "conversation_variable": "conversation",
                "input_variable": "input",
                "output_variable": "result",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )

    with pytest.raises(
        CompilerError,
        match="conversation for agent writer with agent reviewer",
    ):
        WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


@pytest.mark.parametrize(
    ("agent_accepts", "agent_produces", "agent_tools", "message"),
    [
        ([], ["text"], ["lookup"], "does not accept contract text"),
        (["text"], [], ["lookup"], "does not produce contract text"),
        (["text"], ["text"], [], "does not declare task tool lookup"),
    ],
)
def test_compiler_rejects_task_capabilities_outside_agent_declaration(
    agent_accepts: list[str],
    agent_produces: list[str],
    agent_tools: list[str],
    message: str,
) -> None:
    registry = DefinitionRegistry()
    contract = _contract("text", "string")
    tool = ToolDefinition(
        id="lookup",
        version="1.0.0",
        description="Lookup text",
        implementation="fixture:lookup",
        input_contract=contract.id,
        output_contract=contract.id,
    )
    agent = AgentDefinition(
        id="writer",
        version="1.0.0",
        description="Writer",
        instructions="Write.",
        accepts=agent_accepts,
        produces=agent_produces,
        tools=agent_tools,
    )
    task = TaskDefinition(
        id="write",
        version="1.0.0",
        description="Write text",
        agent=agent.id,
        objective="Write text",
        input_contract=contract.id,
        output_contract=contract.id,
        tools=[tool.id],
    )
    for definition in (contract, tool, agent, task):
        registry.register(definition)
    workflow = WorkflowDefinition(
        id="invalid-agent-task-capability",
        version="1.0.0",
        description="Reject undeclared Agent capabilities",
        state={"input": "hello"},
        actions=[
            {
                "id": "conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "writer",
                "output_variable": "conversation",
            },
            {
                "id": "invoke",
                "kind": "invoke_agent",
                "agent": agent.id,
                "task": task.id,
                "conversation_variable": "conversation",
                "input_variable": "input",
                "output_variable": "result",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )

    with pytest.raises(CompilerError, match=message):
        WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


@pytest.mark.parametrize(
    ("task_tools", "message"),
    [
        (["internal-helper"], "task write exposes non-model-visible tool internal-helper"),
        ([], "agent writer exposes non-model-visible tool internal-helper"),
    ],
)
def test_compiler_rejects_an_agent_tool_that_is_not_model_visible(
    task_tools: list[str],
    message: str,
) -> None:
    registry = DefinitionRegistry()
    contract = _contract("text", "string")
    tool = ToolDefinition(
        id="internal-helper",
        version="1.0.0",
        description="Runtime-only helper",
        implementation="fixture:internal-helper",
        input_contract=contract.id,
        output_contract=contract.id,
        model_visible=False,
    )
    agent = AgentDefinition(
        id="writer",
        version="1.0.0",
        description="Writer",
        instructions="Write.",
        accepts=[contract.id],
        produces=[contract.id],
        tools=[tool.id],
    )
    task = TaskDefinition(
        id="write",
        version="1.0.0",
        description="Write text",
        agent=agent.id,
        objective="Write text",
        input_contract=contract.id,
        output_contract=contract.id,
        tools=task_tools,
    )
    for definition in (contract, tool, agent, task):
        registry.register(definition)
    workflow = WorkflowDefinition(
        id="invalid-hidden-agent-tool",
        version="1.0.0",
        description="Reject runtime-only tools in model-facing tasks",
        state={"input": "hello"},
        actions=[
            {
                "id": "conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "writer",
                "output_variable": "conversation",
            },
            {
                "id": "invoke",
                "kind": "invoke_agent",
                "agent": agent.id,
                "task": task.id,
                "conversation_variable": "conversation",
                "input_variable": "input",
                "output_variable": "result",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )

    with pytest.raises(
        CompilerError,
        match=message,
    ):
        WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


@pytest.mark.asyncio
async def test_complete_generic_action_vocabulary_executes_without_domain_logic(
    tmp_path,
) -> None:
    registry = DefinitionRegistry()
    number = _contract("number", "integer")
    agent = AgentDefinition(
        id="neutral-agent",
        version="1.0.0",
        description="Neutral identity",
        instructions="No provider invocation is required.",
    )
    gate = GateDefinition(
        id="minimum",
        version="1.0.0",
        description="Require a minimum value",
        contract=number.id,
        expression="value >= 3",
        on_pass="finish",
        on_fail="fail",
    )
    for definition in (number, agent, gate):
        registry.register(definition)
    workflow = WorkflowDefinition(
        id="generic-actions",
        version="1.0.0",
        description="Exercise state, conversation, gate, and failure actions",
        gates=[gate.id],
        state={
            "items": [1],
            "item": 2,
            "record": {"left": 1},
            "patch": {"right": 2},
            "value": 4,
        },
        actions=[
            {
                "id": "append",
                "kind": "append_variable",
                "variable": "items",
                "value_variable": "item",
            },
            {
                "id": "merge",
                "kind": "merge_variable",
                "variable": "record",
                "value_variable": "patch",
            },
            {
                "id": "create",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "neutral",
                "mode": "run",
                "output_variable": "conversation",
            },
            {
                "id": "reset",
                "kind": "reset_conversation",
                "agent": agent.id,
                "conversation_key": "neutral",
                "mode": "run",
                "output_variable": "conversation",
            },
            {
                "id": "gate",
                "kind": "evaluate_gate",
                "gate": gate.id,
                "input_variable": "value",
                "output_variable": "gate_result",
            },
            {"id": "fail", "kind": "fail_workflow", "message": "below minimum"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "record"},
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)

    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan("generic-actions-run", plan),
        RuntimeContext(
            contracts=build_contract_catalog(registry),
            definitions=registry,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.variables["items"] == [1, 2]
    assert completed.outputs == {"result": {"left": 1, "right": 2}}
    assert completed.variables["gate_result"]["status"] == "pass"
    assert completed.conversations["conversation"].conversation_id.endswith("reset:1")


@pytest.mark.asyncio
async def test_wait_input_alias_uses_the_same_persisted_resume_protocol(tmp_path) -> None:
    registry = DefinitionRegistry()
    number = _contract("number", "integer")
    interaction = InteractionDefinition(
        id="number-input",
        version="1.0.0",
        description="Provide a number",
        input_contract=number.id,
        title="Number",
    )
    registry.register(number)
    registry.register(interaction)
    workflow = WorkflowDefinition(
        id="wait-for-number",
        version="1.0.0",
        description="Wait using the explicit action vocabulary",
        interactions=[interaction.id],
        actions=[
            {
                "id": "wait",
                "kind": "wait_input",
                "interaction": interaction.id,
                "output_variable": "number",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "number"},
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    contracts = build_contract_catalog(registry)
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())

    waiting = await host.execute(
        plan,
        WorkflowState.for_plan("wait-run", plan),
        RuntimeContext(contracts=contracts, definitions=registry),
    )
    resumed = resume_waiting_input(
        plan,
        waiting,
        input_id="wait",
        values=7,
        contracts=contracts,
    )
    completed = await host.execute(
        plan,
        resumed,
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert waiting.status is WorkflowStatus.WAITING
    assert completed.outputs == {"result": 7}


@pytest.mark.asyncio
async def test_declared_gate_failure_routes_to_fail_workflow_and_persists_failure(
    tmp_path,
) -> None:
    registry = DefinitionRegistry()
    number = _contract("number", "integer")
    gate = GateDefinition(
        id="minimum",
        version="1.0.0",
        description="Require a minimum value",
        contract=number.id,
        expression="value >= 3",
        on_pass="finish",
        on_fail="fail",
    )
    registry.register(number)
    registry.register(gate)
    workflow = WorkflowDefinition(
        id="gate-failure",
        version="1.0.0",
        description="Route a deterministic gate failure",
        gates=[gate.id],
        state={"value": 1},
        actions=[
            {
                "id": "gate",
                "kind": "evaluate_gate",
                "gate": gate.id,
                "input_variable": "value",
                "output_variable": "gate_result",
            },
            {"id": "fail", "kind": "fail_workflow", "message": "below minimum"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "value"},
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    store = FileWorkflowStateStore(tmp_path)

    with pytest.raises(RuntimeExecutionError, match="below minimum"):
        await WorkflowRuntimeHost(
            executors,
            store,
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan("gate-failure-run", plan),
            RuntimeContext(
                contracts=build_contract_catalog(registry),
                definitions=registry,
            ),
        )

    failed = store.load("gate-failure-run")
    assert failed.status is WorkflowStatus.FAILED
    assert failed.actions["gate"].status == "completed"
    assert failed.actions["fail"].status == "failed"
