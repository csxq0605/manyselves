"""Provider composition for the file-defined module Author and Reviewer tasks.

The module bridges own prompts and typed result decoding.  This module owns the
remaining Capability composition boundary: it resolves the account-scoped
Provider resources, builds the declared Capability tools, and gives the
generic Agent execution service one stable session per Conversation.

Artifact, task-correlation, and recovery objects are injected ports.  They are
deliberately not constructed or reimplemented here; the existing owner of
those concerns may pass its concrete objects through the
``ModuleProviderDependencies`` value.  The existing ``RunToolResultIndex`` is
passed through the same value and is used by the moved artifact readers for
completed-result reuse.  Provider-attempt recovery beyond the current bridge
remains outside this initial-turn slice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    ListResultPartsTool,
    QueryPeerTool,
    ReplyPeerTool,
    ReportBlockedTool,
    ReportGapTool,
    SubmitResultTool,
    WriteResultPartTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleAuthoringInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.module_agent_bridge import (
    ModuleAuthoringAgentBridge,
)
from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import (
    ModuleReviewerAgentBridge,
)
from manyselves.capabilities.distribution_reporting.runtime.research.evidence_memory import (
    EvidenceResearchMemory,
)
from manyselves.capabilities.distribution_reporting.runtime.research.reference_library import (
    ReferenceLibrary,
)
from manyselves.capabilities.distribution_reporting.runtime.research.web import (
    DisabledWebResearchBackend,
    WebResearchBackend,
)
from manyselves.capabilities.distribution_reporting.runtime.research_tools import (
    OpenProjectSourceTool,
    OpenReferenceTool,
    OpenWebSourceTool,
    SearchProjectEvidenceTool,
    SearchReferenceLibraryTool,
    WebSearchTool,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import (
    SourceLedger,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.loops.agent_loop import AgentLoop
from manyselves.core.loops.bus import MessageBus
from manyselves.core.tools.document_tool import InspectDocumentTool
from manyselves.core.tools.registry import Tool, ToolRegistry
from manyselves.core.tools.result_memory import RunToolResultIndex
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentSessionLoop,
)
from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory

from .contracts.submissions import submission_schema
from .module_provider_tools import (
    CalculateTool,
    _IndexedInspectImageTool,
    _IndexedOpenArtifactTool,
    _IndexedOpenToolResultTool,
    _IndexedSearchTextTool,
)
from .module_runtime import CapabilityModuleRuntime, SessionFactory

LoopBuilder = Callable[..., AgentSessionLoop]
RecoveryCallback = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ModuleProviderDependencies:
    """Existing runtime-owned objects passed into module Tool composition.

    The values intentionally remain opaque to this Capability boundary.  In
    particular, this type does not import or recreate the legacy
    ``ArtifactGateway``, ``TaskCorrelation`` or any recovery persistence
    implementation.
    """

    artifact_gateway: Any | None = None
    artifact_access: Any | None = None
    result_index: RunToolResultIndex | None = None
    task_correlation: Any | None = None
    recovery_event_callback: RecoveryCallback | None = None
    tool_implementations: Mapping[str, Tool] = field(default_factory=dict)
    web_backend: WebResearchBackend | None = None
    research_guard: Callable[[], None] | None = None


class ModuleProviderToolBuilder(Protocol):
    def __call__(
        self,
        workspace: Path,
        *,
        envelope: TaskEnvelope,
        module_id: str,
        session_id: str,
        workflow_id: str,
        bus: MessageBus,
        store: ReportingStore,
        global_knowledge_root: Path | None,
        tool_names: Sequence[str],
        expected_part_ids: Sequence[str],
        dependencies: ModuleProviderDependencies,
    ) -> ToolRegistry: ...


def _module_result_part_ids(
    workspace: Path,
    envelope: TaskEnvelope,
) -> list[str]:
    """Read the already-written typed Author input when it is available."""

    if envelope.input_contract_ref:
        path = workspace / envelope.input_contract_ref
        if path.is_file():
            try:
                value = ModuleAuthoringInput.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                pass
            else:
                return list(value.required_submodule_ids)
    return list(envelope.target_submodule_ids)


def build_module_provider_tools(
    workspace: Path,
    *,
    envelope: TaskEnvelope,
    module_id: str,
    session_id: str,
    workflow_id: str,
    bus: MessageBus,
    store: ReportingStore,
    global_knowledge_root: Path | None,
    tool_names: Sequence[str],
    expected_part_ids: Sequence[str],
    dependencies: ModuleProviderDependencies,
) -> ToolRegistry:
    """Build the declared Module tool set from Capability-owned implementations.

    The artifact gateway/access and existing result index are injected through
    ``dependencies``; when present, this function assembles the moved
    ``inspect_image``, ``open_artifact``, ``open_tool_result`` and ``search_text``
    readers with completed-result reuse.  ``calculate`` is always assembled
    locally.  A missing declared dependency or tool implementation is reported
    rather than silently omitted.
    """

    workspace = Path(workspace).resolve()
    ledger = SourceLedger(workspace, envelope.run_id)
    evidence_memory = EvidenceResearchMemory(workspace, envelope.run_id, module_id)
    library = ReferenceLibrary(workspace, global_root=global_knowledge_root)
    web_backend = dependencies.web_backend or DisabledWebResearchBackend()

    allowed_outputs = list(envelope.allowed_outputs)
    evidence_binding_required = "module_submission" in allowed_outputs
    available: dict[str, Tool] = {
        "search_project_evidence": SearchProjectEvidenceTool(
            workspace,
            ledger,
            dependencies.research_guard,
            evidence_memory,
            run_id=envelope.run_id,
        ),
        "open_project_source": OpenProjectSourceTool(
            workspace,
            ledger,
            dependencies.research_guard,
            evidence_memory,
            run_id=envelope.run_id,
        ),
        "search_reference_library": SearchReferenceLibraryTool(library, ledger),
        "open_reference": OpenReferenceTool(library, ledger),
        "web_search": WebSearchTool(web_backend),
        "open_web_source": OpenWebSourceTool(web_backend, ledger),
        "inspect_document": InspectDocumentTool(workspace),
        "calculate": CalculateTool(),
        "query_peer": QueryPeerTool(
            bus,
            envelope.task_id,
            envelope.agent_id,
            session_id,
            workflow_id,
        ),
        "reply_peer": ReplyPeerTool(bus, envelope.agent_id, workflow_id),
        "write_result_part": WriteResultPartTool(
            envelope.run_id,
            envelope.task_id,
            envelope.revision,
            store,
            list(expected_part_ids),
            evidence_binding_required=evidence_binding_required,
        ),
        "list_result_parts": ListResultPartsTool(
            envelope.run_id,
            envelope.task_id,
            envelope.revision,
            store,
            list(expected_part_ids),
            evidence_binding_required=evidence_binding_required,
        ),
        "submit_result": SubmitResultTool(
            envelope.agent_id,
            session_id,
            envelope.run_id,
            envelope.task_id,
            store,
            bus,
            workflow_id,
            allowed_outputs=allowed_outputs,
            revision=envelope.revision,
            input_contract_kind=envelope.input_contract_kind,
            input_contract_ref=envelope.input_contract_ref,
            submission_schemas={
                kind: submission_schema(kind) for kind in allowed_outputs
            },
            task_correlation=dependencies.task_correlation,
            recovery_event_callback=dependencies.recovery_event_callback,
        ),
        "report_blocked": ReportBlockedTool(
            envelope.agent_id,
            session_id,
            envelope.run_id,
            envelope.task_id,
            store,
            bus,
            workflow_id,
            task_correlation=dependencies.task_correlation,
        ),
        "report_gap": ReportGapTool(
            envelope.agent_id,
            envelope.run_id,
            envelope.task_id,
            store,
            bus,
            workflow_id,
        ),
    }
    if dependencies.artifact_gateway is not None and dependencies.result_index is not None:
        artifact_access = dependencies.artifact_access
        capabilities = tuple(getattr(artifact_access, "capabilities", ()) or ())
        allowed_refs = tuple(getattr(artifact_access, "readable_refs", ()) or ())
        photo_map = getattr(artifact_access, "photo_map", None)
        photo_refs = photo_map() if callable(photo_map) else {}
        large_artifact_reader = envelope.agent_id in {
            "cross-module-reviewer",
            "chief-editor",
            "chief-editor-auditor",
        }
        audit_artifact_reader = envelope.agent_id == "evidence-auditor"
        default_limit = 160_000 if large_artifact_reader else 8_000 if audit_artifact_reader else 4_000
        minimum_limit = 160_000 if envelope.agent_id in {
            "cross-module-reviewer",
            "chief-editor",
        } else 1
        maximum_limit = 160_000 if large_artifact_reader else 8_000
        available.update(
            {
                "inspect_image": _IndexedInspectImageTool(
                    workspace,
                    gateway=dependencies.artifact_gateway,
                    capabilities=capabilities,
                    allowed_refs=allowed_refs,
                    photo_refs=photo_refs,
                    result_index=dependencies.result_index,
                    task_id=envelope.task_id,
                ),
                "open_artifact": _IndexedOpenArtifactTool(
                    dependencies.artifact_gateway,
                    default_limit=default_limit,
                    minimum_limit=minimum_limit,
                    maximum_limit=maximum_limit,
                    allowed_refs=allowed_refs,
                    result_index=dependencies.result_index,
                    task_id=envelope.task_id,
                ),
                "open_tool_result": _IndexedOpenToolResultTool(
                    dependencies.artifact_gateway,
                    result_index=dependencies.result_index,
                    task_id=envelope.task_id,
                ),
                "search_text": _IndexedSearchTextTool(
                    dependencies.artifact_gateway,
                    dependencies.research_guard,
                    allowed_refs=allowed_refs,
                    result_index=dependencies.result_index,
                    task_id=envelope.task_id,
                ),
            }
        )
    available.update(dependencies.tool_implementations)

    missing = [name for name in tool_names if name not in available]
    if missing:
        raise ValueError(
            "module Provider composition requires explicit implementations for: "
            + ", ".join(missing)
        )

    registry = ToolRegistry()
    for name in tool_names:
        registry.register(available[name])
    if "submit_result" in tool_names and allowed_outputs:
        registry._schema_cache["submit_result"] = submission_schema(allowed_outputs[0])
    return registry


class ModuleProviderRuntime:
    """Invoke module Author and Reviewer turns through one generic service."""

    workflow_id = "public-reporting"
    agent_ids = (
        "module-2.1-specialist",
        "module-2.2-specialist",
        "module-2.3-specialist",
        "module-2.4-specialist",
        "module-2.5-specialist",
        "evidence-auditor",
    )

    def __init__(
        self,
        services: RuntimeServicesView,
        *,
        store: ReportingStore | None = None,
        execution: AgentExecutionService | None = None,
        loop_builder: LoopBuilder = AgentLoop,
        dependencies: ModuleProviderDependencies | None = None,
        tool_builder: ModuleProviderToolBuilder = build_module_provider_tools,
    ) -> None:
        if services.workspace is None:
            raise ValueError("module Provider composition requires a workspace")
        self.services = services
        self.workspace = Path(services.workspace).resolve()
        self.store = store or ReportingStore(self.workspace)
        self.execution = execution or AgentExecutionService(services.bus)
        self.loop_builder = loop_builder
        self.dependencies = dependencies or ModuleProviderDependencies()
        self.tool_builder = tool_builder
        self.agent_invokers: dict[str, AgentInvoker] = {
            agent_id: self for agent_id in self.agent_ids
        }

    async def close(self) -> None:
        await self.execution.close_workflow(self.workflow_id)

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        bridge = self._bridge(agent, task, value, conversation, task_id=task_id)
        return await bridge.invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        bridge = self._bridge(agent, task, value, conversation, task_id=task_id)
        if self._is_reviewer(agent, task):
            return await bridge.invoke_with_recovery(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
                recovery_policy=recovery_policy,
            )
        return await bridge.invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    def _bridge(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
    ) -> ModuleAuthoringAgentBridge | ModuleReviewerAgentBridge:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        envelope = self._envelope(context)
        if envelope is None:
            raise ValueError("module Provider turn requires a prepared TaskEnvelope")
        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
        runtime_id = f"{self.workflow_id}:{agent.id}:{conversation.key.value}"
        dependencies = self.dependencies
        tools = self.tool_builder(
            self.workspace,
            envelope=envelope,
            module_id=context.module_id,
            session_id=session_id,
            workflow_id=self.workflow_id,
            bus=self.services.bus,
            store=self.store,
            global_knowledge_root=self.services.global_knowledge_root,
            tool_names=list(task.tools),
            expected_part_ids=_module_result_part_ids(self.workspace, envelope),
            dependencies=dependencies,
        )
        loop_kwargs = {
            "agent_type": runtime_id,
            "workspace": self.workspace,
            "tools": tools,
            "bus": self.services.bus,
            "config": self.services.agent_defaults,
            "llm_provider": self.services.active_provider,
            "system_prompt": agent.instructions,
            "usage_run_id": str(context.reporting_state["run_id"]),
            "usage_task_id": task_id,
        }
        if dependencies.artifact_gateway is not None:
            loop_kwargs["artifact_gateway"] = dependencies.artifact_gateway
        session_factory = ProviderAgentSessionFactory(
            loop_builder=self.loop_builder,
            loop_kwargs=loop_kwargs,
            persist_handoff_summary=False,
        )
        if self._is_reviewer(agent, task):
            return ModuleReviewerAgentBridge(
                self.workspace,
                execution=self.execution,
                session_factory=lambda _runtime_id: session_factory(),
                workflow_id=self.workflow_id,
            )
        return ModuleAuthoringAgentBridge(
            self.workspace,
            execution=self.execution,
            session_factory=lambda _runtime_id: session_factory(),
            workflow_id=self.workflow_id,
        )

    @staticmethod
    def _envelope(context: DeclarativeModuleRuntimeLaneContext) -> TaskEnvelope | None:
        if context.authoring is not None:
            return context.authoring.envelope
        if context.review is not None:
            return context.review.envelope
        if context.revision is not None:
            return getattr(context.revision.prepared, "envelope", None)
        if context.recheck is not None:
            return getattr(context.recheck.prepared, "envelope", None)
        return None

    @staticmethod
    def _is_reviewer(agent: AgentDefinition, task: TaskDefinition) -> bool:
        return agent.id == "evidence-auditor" or task.output_contract in {
            "declarative_module_review_agent_result",
            "declarative_module_recheck_agent_result",
            "module_review_finding_submission",
        }


@dataclass(frozen=True, slots=True)
class ModuleProviderComposition:
    """The provider composition and its Capability public-runtime binding."""

    provider: ModuleProviderRuntime
    module_runtime: CapabilityModuleRuntime
    agent_invokers: Mapping[str, AgentInvoker]
    dependencies: ModuleProviderDependencies


def build_module_provider_composition(
    services: RuntimeServicesView,
    *,
    agent_session_factory: SessionFactory,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
    store: ReportingStore | None = None,
    dependencies: ModuleProviderDependencies | None = None,
    tool_builder: ModuleProviderToolBuilder = build_module_provider_tools,
) -> ModuleProviderComposition:
    """Compose a CapabilityModuleRuntime with Provider-backed Agent invokers."""

    provider = ModuleProviderRuntime(
        services,
        store=store,
        execution=execution,
        loop_builder=loop_builder,
        dependencies=dependencies,
        tool_builder=tool_builder,
    )
    resolved_store = provider.store
    resolved_dependencies = provider.dependencies
    module_runtime = CapabilityModuleRuntime(
        provider.workspace,
        store=resolved_store,
        agent_execution=provider.execution,
        agent_session_factory=agent_session_factory,
        agent_invokers=provider.agent_invokers,
        global_root=services.global_knowledge_root,
    )
    return ModuleProviderComposition(
        provider=provider,
        module_runtime=module_runtime,
        agent_invokers=provider.agent_invokers,
        dependencies=resolved_dependencies,
    )


__all__ = [
    "ModuleProviderComposition",
    "ModuleProviderDependencies",
    "ModuleProviderRuntime",
    "build_module_provider_composition",
    "build_module_provider_tools",
]
