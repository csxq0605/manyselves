"""Capability-owned template Skill distillation entrypoint primitives.

The module describes the generic Agent dispatch and owns the deterministic
materialization of the resulting fourteen Skill parts.  The file Workflow's
preparation Tool reuses the existing run snapshot service; this module does
not calculate hashes or own a Provider/Conversation implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
    StrictModel,
    TaskEnvelope,
    TemplateSkillSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    TemplateDistillationInput,
)
from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.conversations import ConversationRegistry
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.workflow import (
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    restore_plan_definition_registry,
)
from manyselves.runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import (
    CapabilityToolAdapter,
    CapabilityToolAdapterFactory,
)
from manyselves.runtime.workflow_host import FileWorkflowEventSink, WorkflowRuntimeHost

from .. import load_distribution_reporting_capability
from .agent_bridge import TemplateDistillationAgentBridge
from .input_snapshot import RunInputSnapshotStore
from .public_entrypoints import project_public_template_distillation_input
from .storage import ReportingStore

TEMPLATE_DISTILLATION_TASK_ID = "template-skill-distillation"
TEMPLATE_DISTILLER_AGENT_ID = "template-distiller"
TEMPLATE_DISTILLATION_SESSION_KEY = "template-distillation"
TEMPLATE_DISTILLATION_INPUT_REF = (
    "Work/runs/{run_id}/context/template-distillation-input.json"
)
TEMPLATE_DISTILLATION_INSPECTION_REF = (
    "Work/runs/{run_id}/context/template-inspection.json"
)
TEMPLATE_DISTILLATION_SNAPSHOT_REF = (
    "Work/runs/{run_id}/templates/template-for-skill.docx"
)
TEMPLATE_SKILL_ROOT = "Work/report-template-role-skills"
TEMPLATE_DISTILLATION_ALLOWED_TOOLS = (
    "inspect_document",
    "write_result_part",
    "list_result_parts",
    "submit_result",
    "report_blocked",
)
TEMPLATE_DISTILLATION_CONSTRAINTS = (
    "Inspect the declared template exactly once before producing Skill parts.",
    "Persist complete Skill parts with write_result_part and submit one typed result.",
    "The required part ids and reusable-guidance boundary come from the input contract.",
)


@dataclass(frozen=True)
class TemplateInspectionPlan:
    """The single document inspection exposed to the future runtime binding."""

    path: str
    max_chars: int
    once: bool
    cache_ref: str


@dataclass(frozen=True)
class TemplateDistillationPlan:
    """Generic dispatch data for one same-run template distillation task."""

    input: TemplateDistillationInput
    task: TaskEnvelope
    session_key: str
    inspect_document: TemplateInspectionPlan
    required_part_ids: tuple[str, ...]
    allowed_tools: tuple[str, ...]

    def continuation_part_ids(
        self,
        completed_part_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Return the durable parts still needed after a Tool Slice boundary."""

        completed = set(completed_part_ids)
        return tuple(part_id for part_id in self.required_part_ids if part_id not in completed)


class TemplateSkillMaterialization(StrictModel):
    """Typed public output of the local Skill materialization Tool."""

    kind: Literal["template_skill_materialization"] = "template_skill_materialization"
    skill_refs: tuple[str, ...] = Field(min_length=len(TEMPLATE_ROLE_SKILL_IDS))
    boundary_ref: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


def prepare_template_distillation_input(
    value: TemplateDistillationInput,
    *,
    snapshots: RunInputSnapshotStore,
    store: ReportingStore,
    source_metadata: MutableMapping[str, Any],
) -> TemplateDistillationInput:
    """Freeze the declared template inside the file Workflow's Tool boundary.

    The input contract remains the only public value shape.  A project-relative
    source is frozen by the existing run snapshot service, then exposed at the
    isolated template path consumed by the current distiller runner.  No
    content-addressing or validation policy is implemented here; the snapshot
    service remains the owner of that behavior.
    """

    template_ref = Path(value.template_ref)
    expected_ref = Path(
        TEMPLATE_DISTILLATION_SNAPSHOT_REF.format(run_id=value.run_id)
    )
    if template_ref == expected_ref and (snapshots.workspace / template_ref).is_file():
        prepared_ref = template_ref
        source = str(source_metadata.get("source", "provided"))
    elif template_ref.as_posix().startswith(
        f"Work/runs/{value.run_id}/frozen-project/"
    ) and (snapshots.workspace / template_ref).is_file():
        prepared_ref = template_ref
        source = str(source_metadata.get("source", "frozen-project"))
    else:
        snapshot = snapshots.freeze(value.run_id, extra_refs=(template_ref,))
        frozen_ref = snapshot.resolve(template_ref)
        item = next(
            item for item in snapshot.files if item.logical_ref == template_ref
        )
        target = snapshots.workspace / expected_ref
        if not target.exists() and not target.is_symlink():
            trusted_handle = snapshots.content_store.load_trusted_handle(
                item.trusted_handle_ref
            )
            snapshots.content_store.link_trusted_view(trusted_handle, target)
        prepared_ref = expected_ref
        source = str(source_metadata.get("source", "project"))
        source_metadata["frozen_source_ref"] = frozen_ref.as_posix()

    prepared = value.model_copy(update={"template_ref": prepared_ref.as_posix()})
    input_ref = TEMPLATE_DISTILLATION_INPUT_REF.format(run_id=value.run_id)
    store.write_json(input_ref, prepared.model_dump(mode="json"))
    source_metadata.update(
        {
            "source": source,
            "template_ref": prepared.template_ref,
            "inspection_ref": TEMPLATE_DISTILLATION_INSPECTION_REF.format(
                run_id=value.run_id
            ),
        }
    )
    return prepared


def build_template_distillation_plan(
    run_id: str,
    template_ref: str,
    *,
    input_ref: str | None = None,
    inspect_max_chars: int = 100_000,
) -> TemplateDistillationPlan:
    """Build one generic task envelope without starting a Provider session."""

    resolved_input_ref = input_ref or TEMPLATE_DISTILLATION_INPUT_REF.format(run_id=run_id)
    distillation_input = TemplateDistillationInput(
        run_id=run_id,
        template_ref=template_ref,
        inspect_max_chars=inspect_max_chars,
        required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
    )
    task = TaskEnvelope(
        task_id=TEMPLATE_DISTILLATION_TASK_ID,
        run_id=run_id,
        agent_id=TEMPLATE_DISTILLER_AGENT_ID,
        objective=(
            "从唯一指定报告模板中蒸馏可复用的角色与模块 Skill；"
            "输出五个作者 Skill、五个 Auditor Skill、三个 Chief 章节 Skill 和一个 Final Auditor Skill。"
        ),
        input_refs=[resolved_input_ref, template_ref],
        constraints=list(TEMPLATE_DISTILLATION_CONSTRAINTS),
        allowed_outputs=["template_skill_submission"],
        allowed_tools=list(TEMPLATE_DISTILLATION_ALLOWED_TOOLS),
        input_contract_kind="template_distillation_input",
        input_contract_ref=resolved_input_ref,
    )
    return TemplateDistillationPlan(
        input=distillation_input,
        task=task,
        session_key=TEMPLATE_DISTILLATION_SESSION_KEY,
        inspect_document=TemplateInspectionPlan(
            path=template_ref,
            max_chars=inspect_max_chars,
            once=True,
            cache_ref=TEMPLATE_DISTILLATION_INSPECTION_REF.format(run_id=run_id),
        ),
        required_part_ids=TEMPLATE_ROLE_SKILL_IDS,
        allowed_tools=TEMPLATE_DISTILLATION_ALLOWED_TOOLS,
    )


def materialize_template_skill_submission(
    store: ReportingStore,
    submission: TemplateSkillSubmission,
    *,
    source_metadata: Mapping[str, Any],
) -> TemplateSkillMaterialization:
    """Write the fourteen Skill files plus their boundary and source manifests.

    ``source_metadata`` is supplied by the application binding.  Existing
    lineage fields are preserved as data, but this Capability deliberately
    does not calculate or verify hashes/CAS handles.
    """

    for skill_id in TEMPLATE_ROLE_SKILL_IDS:
        store.write_text(
            f"{TEMPLATE_SKILL_ROOT}/{skill_id}/SKILL.md",
            submission.skills[skill_id],
        )

    boundary_ref = f"{TEMPLATE_SKILL_ROOT}/boundary.json"
    source_ref = f"{TEMPLATE_SKILL_ROOT}/source.json"
    store.write_json(
        boundary_ref,
        submission.boundary_manifest.model_dump(mode="json"),
    )
    source = dict(source_metadata)
    source.update(
        {
            "producer": TEMPLATE_DISTILLER_AGENT_ID,
            "task_id": TEMPLATE_DISTILLATION_TASK_ID,
            "skill_root": TEMPLATE_SKILL_ROOT,
            "boundary_policy_version": submission.boundary_manifest.policy_version,
            "boundary_ref": boundary_ref,
        }
    )
    store.write_json(source_ref, source)
    return TemplateSkillMaterialization(
        skill_refs=tuple(
            f"{TEMPLATE_SKILL_ROOT}/{skill_id}/SKILL.md"
            for skill_id in TEMPLATE_ROLE_SKILL_IDS
        ),
        boundary_ref=boundary_ref,
        source_ref=source_ref,
    )


def build_template_distillation_tool_implementations(
    store: ReportingStore,
    *,
    source_metadata: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Bind the file Workflow's preparation and materialization Tools."""

    snapshots = RunInputSnapshotStore(store.workspace)

    def prepare(value: Any) -> dict[str, Any]:
        input_value = (
            value
            if isinstance(value, TemplateDistillationInput)
            else TemplateDistillationInput.model_validate(value)
        )
        return prepare_template_distillation_input(
            input_value,
            snapshots=snapshots,
            store=store,
            source_metadata=source_metadata,
        ).model_dump(mode="json")

    def materialize(value: Any) -> dict[str, Any]:
        submission = (
            value
            if isinstance(value, TemplateSkillSubmission)
            else TemplateSkillSubmission.model_validate(value)
        )
        return materialize_template_skill_submission(
            store,
            submission,
            source_metadata=source_metadata,
        ).model_dump(mode="json")

    return {
        "project-public-template-distillation-input": (
            project_public_template_distillation_input
        ),
        "prepare-template-distillation": prepare,
        "materialize-template-skill": materialize,
    }


class TemplateDistillationWorkflowRuntime:
    """Execute ``distill-template-skill`` through the Generic Host."""

    workflow_id = "distill-template-skill"

    def __init__(
        self,
        workspace: Path,
        *,
        agent_invoker: Any,
        state_store: FileWorkflowStateStore | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.agent_invoker = agent_invoker
        self._store = state_store or FileWorkflowStateStore(self.workspace)
        self._executors = build_builtin_executor_registry()
        self._source_metadata: dict[str, Any] = {}

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        if workflow_id != self.workflow_id:
            raise ValueError(f"workflow is not runnable: {workflow_id}")
        _capability, registry, contracts, plan = self._compiled()
        if plan.input_contract is None or plan.input_variable is None:
            raise TypeError("distill-template-skill has no declared input binding")
        request = contracts[plan.input_contract].validate(values)
        run_id = self.run_id_for(command_id, workflow_id)
        try:
            state = self._store.load(run_id)
        except FileNotFoundError:
            self._store.save_plan(run_id, plan)
            state = WorkflowState.for_plan(
                run_id,
                plan,
                initial_variables={
                    plan.input_variable: request,
                    "run-id": run_id,
                },
            )
        await self._execute(plan, state, registry, contracts)
        return {"run_id": run_id, "task_id": None}

    @property
    def state_store(self) -> FileWorkflowStateStore:
        return self._store

    @staticmethod
    def run_id_for(command_id: UUID, workflow_id: str) -> str:
        """Return the Run identity owned by distill-template-skill."""

        return f"{workflow_id}-{command_id.hex}"

    async def provide_input(
        self,
        command_id: UUID,
        run_id: str,
        *,
        input_id: str | None,
        values: Any,
    ) -> dict[str, Any]:
        del command_id, input_id, values
        self._load_state(run_id)
        raise CapabilityRunInputError("distill-template-skill has no waiting input")

    async def resume(
        self,
        command_id: UUID,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume the persisted incomplete template Plan + State."""

        del command_id
        state = self._load_state(run_id)
        if state.status is WorkflowStatus.COMPLETED:
            return {"run_id": run_id, "task_id": None}
        if state.status is WorkflowStatus.WAITING:
            raise CapabilityRunStateError(
                "run is waiting for input; resume it through the input endpoint"
            )
        try:
            plan = self._store.load_plan(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        registry = restore_plan_definition_registry(plan)
        contracts = self._contracts(registry)
        await self._execute(plan, state, registry, contracts)
        return {"run_id": run_id, "task_id": None}

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        waiting_input = [state.waiting_input] if state.waiting_input is not None else []
        return {
            "run": {
                "run_id": run_id,
                "capability_id": "distribution-reporting",
                "workflow_id": state.workflow_id,
                "status": state.status.value,
                "active": state.status in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING},
                "task_id": None,
            },
            "state": state.model_dump(mode="json"),
            "waiting_input": waiting_input,
        }

    def get_outputs(self, run_id: str) -> dict[str, Any]:
        state = self._load_state(run_id)
        outputs: list[dict[str, Any]] = []
        for output_id, value in state.outputs.items():
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            outputs.append({"id": output_id, "kind": "value", "value": value})
        return {"run_id": run_id, "outputs": outputs}

    def get_cost(self, run_id: str) -> dict[str, Any]:
        self._load_state(run_id)
        from manyselves.core.usage_ledger import UsageLedger

        return {
            "run_id": run_id,
            "usage": UsageLedger(self.workspace, run_id).summarize(group_by="stage"),
        }

    def _compiled(
        self,
    ) -> tuple[
        Any,
        DefinitionRegistry,
        dict[str, ContractAdapter],
        Any,
    ]:
        capability, registry = load_distribution_reporting_capability()
        workflow = registry.require(DefinitionKind.WORKFLOW, self.workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"definition is not a workflow: {self.workflow_id}")
        contracts = self._contracts(registry)
        plan = WorkflowCompiler(self._executors).compile(workflow, registry)
        return capability, registry, contracts, plan

    async def _execute(
        self,
        plan: Any,
        state: WorkflowState,
        registry: DefinitionRegistry,
        contracts: dict[str, ContractAdapter],
    ) -> WorkflowState:
        input_value = state.variables.get(plan.input_variable)
        if input_value is not None:
            restored_input = (
                input_value
                if isinstance(input_value, TemplateDistillationInput)
                else TemplateDistillationInput.model_validate(input_value)
            )
            self._source_metadata.setdefault("run_id", restored_input.run_id)
            self._source_metadata.setdefault("template_ref", restored_input.template_ref)
        tools = self._tools(registry, contracts, plan.tool_ids)
        return await WorkflowRuntimeHost(
            self._executors,
            self._store,
            FileWorkflowEventSink(self.workspace),
        ).execute(
            plan,
            state,
            RuntimeContext(
                tools=tools,
                contracts=contracts,
                agents={TEMPLATE_DISTILLER_AGENT_ID: self.agent_invoker},
                definitions=registry,
                conversations=ConversationRegistry(FileConversationStore(self.workspace)),
                plan_tool_factory=self._bind_plan_tool,
            ),
        )

    @staticmethod
    def _contracts(registry: DefinitionRegistry) -> dict[str, ContractAdapter]:
        return {
            definition.id: build_contract_adapter(definition)
            for definition in registry.all(DefinitionKind.CONTRACT)
            if isinstance(definition, ContractDefinition)
        }

    def _tools(
        self,
        registry: DefinitionRegistry,
        contracts: dict[str, ContractAdapter],
        tool_ids: list[str],
    ) -> dict[str, CapabilityToolAdapter]:
        factory = CapabilityToolAdapterFactory(
            "distribution-reporting",
            build_template_distillation_tool_implementations(
                ReportingStore(self.workspace),
                source_metadata=self._source_metadata,
            ),
            contracts,
        )
        return {
            tool_id: factory.build(registry.require(DefinitionKind.TOOL, tool_id))
            for tool_id in tool_ids
        }

    def _bind_plan_tool(
        self,
        definition: ToolDefinition,
        contracts: Mapping[str, ContractAdapter],
    ) -> CapabilityToolAdapter:
        return CapabilityToolAdapterFactory(
            "distribution-reporting",
            build_template_distillation_tool_implementations(
                ReportingStore(self.workspace),
                source_metadata=self._source_metadata,
            ),
            contracts,
        ).build(definition)

    def _load_state(self, run_id: str) -> WorkflowState:
        try:
            state = self._store.load(run_id)
        except FileNotFoundError as exc:
            raise CapabilityRunNotFoundError(run_id) from exc
        if state.workflow_id != self.workflow_id:
            raise CapabilityRunNotFoundError(run_id)
        return state


__all__ = [
    "TEMPLATE_DISTILLATION_ALLOWED_TOOLS",
    "TEMPLATE_DISTILLATION_CONSTRAINTS",
    "TEMPLATE_DISTILLATION_INPUT_REF",
    "TEMPLATE_DISTILLATION_INSPECTION_REF",
    "TEMPLATE_DISTILLATION_SNAPSHOT_REF",
    "TEMPLATE_DISTILLATION_SESSION_KEY",
    "TEMPLATE_DISTILLATION_TASK_ID",
    "TEMPLATE_DISTILLER_AGENT_ID",
    "TEMPLATE_SKILL_ROOT",
    "TemplateDistillationPlan",
    "TemplateInspectionPlan",
    "TemplateSkillMaterialization",
    "TemplateDistillationAgentBridge",
    "TemplateDistillationWorkflowRuntime",
    "build_template_distillation_plan",
    "build_template_distillation_tool_implementations",
    "prepare_template_distillation_input",
    "materialize_template_skill_submission",
]
