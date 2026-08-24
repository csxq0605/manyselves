"""File-defined, deterministic rendering of an existing Markdown artifact."""

from __future__ import annotations

import io
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from docx import Document
from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportingModel,
    ReportRequest,
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
from .delivery_tools import atomic_copy_file
from .input_snapshot import RunInputSnapshotStore
from .rendering.contracts import RenderRequest, RenderResult
from .rendering.handoff_docx import PackagedV2DocxCore
from .rendering.packaged_docx import verify_rendered_markdown
from .rendering.pds_docx_renderer import PdsDocxRenderer
from .storage import ReportingStore
from .template_resolver import resolve_report_template


class RenderExistingRequest(ReportRequest):
    """Public request narrowed to the existing-Markdown rendering operation."""

    operation: Literal["render_existing"] = "render_existing"


class RenderExistingPrepareInput(ReportingModel):
    """Internal tool input combining the request with the Generic Run identity."""

    request: RenderExistingRequest
    run_id: str = Field(min_length=1)


class RenderExistingContext(ReportingModel):
    """Serializable context passed from preparation to deterministic rendering."""

    run_id: str = Field(min_length=1)
    source_markdown_ref: Path
    source_snapshot_ref: Path
    template_ref: Path
    output_ref: Path
    template_source: str = Field(min_length=1)
    render_request_ref: Path
    template_provenance_ref: Path


def _relative_template_path(workspace: Path, selected_template: Path) -> str:
    if selected_template.is_relative_to(workspace):
        return selected_template.relative_to(workspace).as_posix()
    return "manyselves/templates/reporting/report_template.docx"


def prepare_render_existing(
    value: RenderExistingPrepareInput,
    *,
    workspace: Path,
    snapshots: RunInputSnapshotStore,
    store: ReportingStore,
) -> RenderExistingContext:
    """Freeze and materialize the source/template handoff for one Run."""

    request = value.request
    snapshot = snapshots.freeze(
        value.run_id,
        extra_refs=(request.source_markdown_ref,),
    )
    source_snapshot_ref = snapshot.resolve(request.source_markdown_ref)
    selected_template, template_source = resolve_report_template(
        workspace,
        value.run_id,
    )
    template_ref = Path(
        f"Work/runs/{value.run_id}/templates/report_template.docx"
    )
    atomic_copy_file(workspace, selected_template, workspace / template_ref)
    output_ref = Path("Outputs/Reports") / (
        request.output_filename or f"{request.source_markdown_ref.stem}.docx"
    )
    render_request_ref = Path(f"Work/runs/{value.run_id}/render-request.json")
    template_provenance_ref = Path(
        f"Work/runs/{value.run_id}/template-provenance.json"
    )
    render_request = RenderRequest(
        run_id=value.run_id,
        source_markdown_ref=request.source_markdown_ref,
        source_snapshot_ref=source_snapshot_ref,
        template_ref=template_ref,
        output_ref=output_ref,
    )
    store.write_json(
        render_request_ref.as_posix(),
        render_request.model_dump(mode="json"),
    )
    store.write_json(
        template_provenance_ref.as_posix(),
        {
            "source": template_source,
            "selected_path": _relative_template_path(workspace, selected_template),
            "snapshot_path": template_ref.as_posix(),
            "storage": "materialized",
        },
    )
    return RenderExistingContext(
        run_id=value.run_id,
        source_markdown_ref=request.source_markdown_ref,
        source_snapshot_ref=source_snapshot_ref,
        template_ref=template_ref,
        output_ref=output_ref,
        template_source=template_source,
        render_request_ref=render_request_ref,
        template_provenance_ref=template_provenance_ref,
    )


def _render_document(
    value: RenderExistingContext,
    *,
    workspace: Path,
    store: ReportingStore,
) -> RenderResult:
    source_path = workspace / value.source_snapshot_ref
    markdown = source_path.read_text(encoding="utf-8")
    if not markdown.strip():
        raise ValueError("render source Markdown is empty")

    output = workspace / value.output_ref
    template = workspace / value.template_ref
    _rendered_name, raw_docx = PackagedV2DocxCore(template).render_approved_prose(
        markdown,
        filename=output.name,
        report_model=None,
    )
    title = next(
        (
            line.removeprefix("#").strip()
            for line in markdown.splitlines()
            if line.startswith("# ")
        ),
        "配电安全专家咨询报告",
    )
    rendered_document = Document(io.BytesIO(raw_docx))
    PdsDocxRenderer._ensure_title(rendered_document, title)
    rendered_buffer = io.BytesIO()
    rendered_document.save(rendered_buffer)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.stem}-",
            suffix=".docx",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary.write(rendered_buffer.getvalue())
            temporary_path = Path(temporary.name)
        verify_rendered_markdown(temporary_path, markdown)
        temporary_path.replace(output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    render_log_ref = Path(f"Work/runs/{value.run_id}/render-log.json")
    result = RenderResult(
        status="completed",
        run_id=value.run_id,
        source_markdown_ref=value.source_markdown_ref,
        source_snapshot_ref=value.source_snapshot_ref,
        output_ref=value.output_ref,
        render_log_ref=render_log_ref,
        protected_prose_verified=True,
    )
    store.write_json(render_log_ref.as_posix(), result.model_dump(mode="json"))
    store.write_json(
        f"Work/runs/{value.run_id}/render-result.json",
        result.model_dump(mode="json"),
    )
    return result


def build_render_existing_tool_implementations(
    workspace: Path,
) -> dict[str, Any]:
    """Bind file-defined Tool IDs to this Capability's deterministic functions."""

    workspace = Path(workspace)
    snapshots = RunInputSnapshotStore(workspace)
    store = ReportingStore(workspace)

    def prepare(value: Any) -> RenderExistingContext:
        return prepare_render_existing(
            RenderExistingPrepareInput.model_validate(value),
            workspace=workspace,
            snapshots=snapshots,
            store=store,
        )

    def render(value: Any) -> RenderResult:
        return _render_document(
            RenderExistingContext.model_validate(value),
            workspace=workspace,
            store=store,
        )

    return {
        "prepare-render-existing": prepare,
        "render-existing": render,
    }


class RenderExistingWorkflowRuntime:
    """Run the render-existing file workflow through the Generic Host."""

    workflow_id = "render-existing"

    def __init__(
        self,
        workspace: Path,
        *,
        state_store: FileWorkflowStateStore | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self._store = state_store or FileWorkflowStateStore(self.workspace)
        self._executors = build_builtin_executor_registry()

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]:
        if workflow_id != self.workflow_id:
            raise ValueError(f"workflow is not runnable: {workflow_id}")
        _capability, registry, contracts, _tools, plan = self._compiled()
        if plan.input_contract is None or plan.input_variable is None:
            raise TypeError("render-existing workflow has no declared input binding")
        public_request = contracts[plan.input_contract].validate(values)
        request = RenderExistingRequest.model_validate(
            public_request.model_dump(mode="python")
        )
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
        """Return the Run identity owned by render-existing."""

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
        raise CapabilityRunInputError("render-existing has no waiting input")

    async def resume(
        self,
        command_id: UUID,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume the persisted incomplete render Plan + State."""

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
                "active": state.status
                in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING},
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
            if isinstance(value, Mapping) and value.get("output_ref"):
                path = str(value["output_ref"])
                target = Path(path)
                target = target if target.is_absolute() else self.workspace / target
                exists = target.is_file()
                outputs.append(
                    {
                        "id": path,
                        "kind": "artifact",
                        "path": path,
                        "exists": exists,
                        "size": target.stat().st_size if exists else 0,
                    }
                )
        return {"run_id": run_id, "outputs": outputs}

    def get_cost(self, run_id: str) -> dict[str, Any]:
        self._load_state(run_id)
        from manyselves.runtime.usage_ledger import UsageLedger

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
        dict[str, CapabilityToolAdapter],
        Any,
    ]:
        capability, registry = load_distribution_reporting_capability()
        workflow = registry.require(DefinitionKind.WORKFLOW, self.workflow_id)
        if not isinstance(workflow, WorkflowDefinition):
            raise TypeError(f"definition is not a workflow: {self.workflow_id}")
        contracts = self._contracts(registry)
        plan = WorkflowCompiler(self._executors).compile(workflow, registry)
        tools = self._tools(registry, contracts, plan.tool_ids)
        return capability, registry, contracts, tools, plan

    async def _execute(
        self,
        plan: Any,
        state: WorkflowState,
        registry: DefinitionRegistry,
        contracts: dict[str, ContractAdapter],
    ) -> WorkflowState:
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
                definitions=registry,
                conversations=ConversationRegistry(
                    FileConversationStore(self.workspace)
                ),
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
            build_render_existing_tool_implementations(self.workspace),
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
            build_render_existing_tool_implementations(self.workspace),
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
    "RenderExistingContext",
    "RenderExistingPrepareInput",
    "RenderExistingRequest",
    "RenderExistingWorkflowRuntime",
    "build_render_existing_tool_implementations",
    "prepare_render_existing",
]
