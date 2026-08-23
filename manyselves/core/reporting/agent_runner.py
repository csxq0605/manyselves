"""Run one reporting identity in an isolated Manyselves AgentLoop."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import operator
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from ...config.schema import AgentDefaults
from ...interfaces.types import (
    AgentResponse,
    AgentResultMessage,
)
from ...kernel.contracts import (
    ContractAdapter,
    ContractValidationError,
    build_contract_catalog,
)
from ...kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    ToolDefinition,
)
from ...kernel.recovery import RecoveryActionKind, RecoveryEventKind
from ...kernel.workflow import ResolvedPlan, restore_plan_definition_registry
from ...runtime.agent_execution import (
    AgentExecutionService,
    AgentExecutionSession,
    AgentRecoveryCompleted,
    AgentRecoveryDirective,
    AgentRecoveryObservation,
    AgentRecoveryProgress,
    AgentRecoveryRequired,
    AgentRecoveryStopped,
    AgentSessionRestore,
    AgentTerminalSubscription,
    AgentTurnOutcome,
    AgentTurnRequest,
)
from ...runtime.agent_recovery import AgentRecoveryDriver
from ..artifacts import ArtifactGateway, ArtifactGrant, ToolContractError, parse_artifact
from ..artifacts.content_store import ContentAddressedStore
from ..loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AGENT_TURN_CONTINUATION_REQUIRED,
    AgentLoop,
)
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..providers.base import Message as LLMMessage
from ..tools.artifact_tools import OpenArtifactTool, OpenToolResultTool, SearchTextTool
from ..tools.document_tool import InspectDocumentTool
from ..tools.outcomes import ToolOutcome
from ..tools.registry import Tool, ToolRegistry
from ..tools.reporting_collaboration_tools import (
    ListResultPartsTool,
    QueryPeerTool,
    ReplyPeerTool,
    ReportBlockedTool,
    ReportGapTool,
    SubmitResultTool,
    WriteResultPartTool,
)
from ..tools.reporting_research_tools import (
    OpenProjectSourceTool,
    OpenReferenceTool,
    OpenWebSourceTool,
    PublishResearchNoteTool,
    SearchProjectEvidenceTool,
    SearchReferenceLibraryTool,
    WebSearchTool,
)
from ..tools.result_memory import RunToolResultIndex
from ..tools.skill_evolution_tools import ProductSkillEvolutionTool
from .agentic_models import (
    TEMPLATE_ROLE_SKILL_IDS,
    AgentResult,
    AgentRunStatus,
    TaskEnvelope,
)
from .capabilities import (
    Capability,
    collect_reference_refs,
    compile_agent_access,
    scoped_gateway,
)
from .config import AgentDefinition
from .context_manifest import (
    HashOccurrenceTracker,
    build_manifest_payload,
    sha256_value,
)
from .context_rebase import ReportingContextRebuilder
from .context_state import (
    ContextManifest,
    EvidenceSlice,
    KnowledgeSlice,
    TaskStateCapsule,
    TaskStateStore,
    ToolResultMemoStore,
)
from .execution_runtime import ProviderRouter, ResolvedTaskExecutionProfile
from .input_contracts import (
    INPUT_CONTRACT_TYPES,
    AggregateEditorInput,
    ChiefChapterLaneInput,
    ChiefEditorInput,
    ChiefRevisionInput,
    CrossOwnerInput,
    CrossReviewInput,
    FinalChapterLaneInput,
    FinalReviewInput,
    ModuleAuthoringInput,
    ModuleReviewInput,
    ModuleRevisionInput,
    TemplateDistillationInput,
)
from .input_snapshot import RunInputSnapshotStore
from .message_router import WorkflowMessageRouter, artifact_path_refs
from .models import CHIEF_RESULT_PART_IDS, CHIEF_SECTION_RESULT_PART_IDS
from .module_skills import ModuleSkillLibrary
from .parallel_runtime import (
    IdentityLease,
    IdentityLeaseManager,
    TaskAttemptStore,
    TaskCorrelation,
    exclusive_file_lock,
)
from .prompts import PromptAssembler
from .provider_admission import ProviderAdmissionController
from .research.evidence_memory import EvidenceResearchMemory
from .research.reference_library import ReferenceLibrary
from .research.web import BraveWebResearchBackend, DisabledWebResearchBackend
from .session_summary import SessionSummaryBuilder, SessionSummaryStore
from .skills.resolver import RuntimeSkillResolver
from .source_ledger import SourceLedger
from .store import ReportingStore
from .submission_contracts import submission_schema
from .taxonomy import REPORT_TAXONOMY
from .versions import SkillProvenance

REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS = 600.0
REPORTING_AUDIT_STREAM_IDLE_TIMEOUT_SECONDS = 600.0
CONTINUATION_HARNESS_STOPPED = "REPORTING_CONTINUATION_HARNESS_STOPPED:"
REPORTING_AUDIT_AGENT_IDS = frozenset(
    {
        "evidence-auditor",
        "cross-module-reviewer",
        "chief-editor-auditor",
    }
)


class _RunnerContextRebuilder(ReportingContextRebuilder):
    """Reporting rebaser with Provider-safe ordering for legacy one-call tails.

    The shared rebaser emits typed state, complete tool units, then a bounded
    tail.  A first one-call tool round can leave the immutable ``task_context``
    user message in that tail; move that *initial* task header before the
    assistant tool call so the Provider sees ``assistant -> tool`` as the last
    pair.  Semantic continuation/correction tails intentionally stay after the
    unit because they are the next user turn.
    """

    def rebuild(self, messages, tool_definitions=None, **kwargs):
        rebased = super().rebuild(messages, tool_definitions, **kwargs)
        items = list(rebased.messages)
        assistant_index = next(
            (
                index
                for index, item in enumerate(items)
                if getattr(item, "role", None) == "assistant"
                and getattr(item, "tool_calls", None)
            ),
            None,
        )
        if assistant_index is None:
            return rebased
        trailing = items[assistant_index + 1 :]
        task_headers = [
            item
            for item in trailing
            if getattr(item, "role", None) == "user"
            and str(getattr(item, "content", "") or "").lstrip().startswith(
                "<task_context>"
            )
        ]
        if not task_headers:
            return rebased
        remaining = [item for item in trailing if item not in task_headers]
        reordered = items[:assistant_index] + task_headers + items[assistant_index:assistant_index + 1]
        # Keep any tool results and semantic tail after the assistant unit.
        reordered.extend(remaining)
        return rebased.__class__(
            messages=reordered,
            tool_definitions=rebased.tool_definitions,
            manifest=rebased.manifest,
            dropped_message_count=rebased.dropped_message_count,
            forensic_only=rebased.forensic_only,
            reason=rebased.reason,
        )


def load_conversation_trace(
    workspace: Path,
    manifest_ref: str | Path,
) -> dict:
    """Read the current identity/reference state."""

    workspace = Path(workspace).resolve()
    manifest_path = (
        Path(manifest_ref)
        if Path(manifest_ref).is_absolute()
        else workspace / manifest_ref
    ).resolve()
    if (
        not manifest_path.is_relative_to(workspace)
        or not manifest_path.is_file()
    ):
        raise ValueError(
            f"conversation manifest is not a readable workspace file: {manifest_ref}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 4 or manifest.get("encoding") != "identity+refs":
        raise ValueError("unsupported conversation identity state")
    identity_state = manifest.get("identity_state")
    business_refs = manifest.get("business_refs")
    if not isinstance(identity_state, dict) or not isinstance(business_refs, dict):
        raise ValueError("conversation identity/reference state is invalid")
    return {**manifest, "messages": []}


class InspectImageTool(Tool):
    name = "inspect_image"
    description = "Inspect dimensions and format of one project-local image."

    side_effect = "pure_read"
    parallel_safe = True

    def __init__(
        self,
        workspace: Path,
        *,
        gateway: ArtifactGateway | None = None,
        capabilities: tuple[Capability, ...] | list[Capability] = (),
        allowed_refs: tuple[str, ...] | list[str] = (),
        photo_refs: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.gateway = gateway
        self.capabilities = {
            item.canonical_ref: item for item in capabilities
        }
        self.allowed_refs = frozenset(str(ref) for ref in allowed_refs)
        self.photo_refs = dict(photo_refs or ())

    def _resolve_ref(self, value: str) -> tuple[str, Path, Capability | None]:
        if not isinstance(value, str) or not value.strip():
            raise ToolContractError(
                "image reference is required",
                code="invalid_reference",
            )
        ref = value.strip()
        if ref.upper().startswith("P-") and "/" not in ref and "\\" not in ref:
            mapped = self.photo_refs.get(ref)
            if mapped is None:
                raise ToolContractError(
                    "image identifier is not in the current run PhotoAsset map",
                    code="image_scope_unresolved",
                    repair_code="resolve_image_scope",
                    details={"identifier": ref},
                )
            ref = mapped
        if ref not in self.allowed_refs:
            raise PermissionError(
                "image was not delivered by reference for this task"
            )
        capability = self.capabilities.get(ref)
        if capability is None and self.gateway is not None:
            descriptor = self.gateway.describe(ref)
            if descriptor.kind != "image" or "inspect_image" not in descriptor.allowed_operations:
                raise ToolContractError(
                    "artifact is not an inspectable image",
                    code="unsupported_operation",
                    repair_code="use_format_reader",
                    details={"ref": ref, "kind": descriptor.kind},
                )
        elif capability is not None:
            if capability.kind != "image" or not capability.allows_operation("inspect_image"):
                raise ToolContractError(
                    "artifact capability does not allow image inspection",
                    code="capability_denied",
                    details={"ref": ref},
                )
        if self.gateway is not None and ref.startswith("artifact:v1:"):
            target = self.gateway._resolve(ref)
        else:
            target = (self.workspace / ref).resolve()
        if not target.is_relative_to(self.workspace) or not target.is_file():
            raise PermissionError("image must resolve to a current workspace file")
        return ref, target, capability

    async def __call__(self, path: str | None = None, ref: str | None = None) -> dict:
        """Inspect an image.

        Args:
            path: Authorized project-relative image path or a current-run P-ID.
            ref: Alias for an authorized opaque/current image reference.
        """
        selected = ref if ref is not None else path
        canonical_ref, target, _capability = self._resolve_ref(selected or "")
        parsed = parse_artifact(target)
        return {
            "path": canonical_ref,
            "kind": parsed.kind,
            "metadata": parsed.blocks[0].text if parsed.blocks else "",
            "visual_verified": False,
            "error": parsed.error,
        }


class _IndexedOpenArtifactTool(OpenArtifactTool):
    """Run-scoped idempotency wrapper for the bounded artifact reader."""

    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(
        self, ref: str, offset: int = 0, limit: int | None = None
    ) -> dict:
        arguments = {"ref": ref, "offset": offset, "limit": limit}
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=ref,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(ref=ref, offset=offset, limit=limit)
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=ref,
            )
        return result


class _IndexedSearchTextTool(SearchTextTool):
    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(
        self,
        ref: str,
        query: str,
        max_matches: int = 20,
        context_lines: int = 2,
    ) -> dict:
        arguments = {
            "ref": ref,
            "query": query,
            "max_matches": max_matches,
            "context_lines": context_lines,
        }
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=ref,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(
            ref=ref,
            query=query,
            max_matches=max_matches,
            context_lines=context_lines,
        )
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=ref,
            )
        return result


class _IndexedOpenToolResultTool(OpenToolResultTool):
    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(self, ref: str, offset: int = 0, limit: int = 8000) -> dict:
        arguments = {"ref": ref, "offset": offset, "limit": limit}
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=ref,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(ref=ref, offset=offset, limit=limit)
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=ref,
            )
        return result


class _IndexedInspectImageTool(InspectImageTool):
    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(self, path: str | None = None, ref: str | None = None) -> dict:
        arguments = {"path": path, "ref": ref}
        selected = ref if ref is not None else path
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=selected,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(path=path, ref=ref)
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=selected,
            )
        return result


class CalculateTool(Tool):
    name = "calculate"
    description = "Evaluate a basic arithmetic expression with no names or code execution."
    side_effect = "pure_read"
    parallel_safe = True
    _ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    async def __call__(self, expression: str) -> dict:
        """Calculate a value.

        Args:
            expression: Arithmetic expression containing numbers and operators only.
        """

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in self._ops:
                return self._ops[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in self._ops:
                return self._ops[type(node.op)](evaluate(node.operand))
            raise ValueError("unsupported expression")

        return {"expression": expression, "result": evaluate(ast.parse(expression, mode="eval"))}


class _RecoveryAwareReportingTool:
    """Dispatch Tool contract failures without changing Provider-visible schemas."""

    def __init__(
        self,
        delegate: Tool,
        callback: Callable[[str, dict[str, Any]], Awaitable[Any]],
    ) -> None:
        self._delegate = delegate
        self._callback = callback

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def __call__(self, **kwargs: Any) -> Any:
        try:
            return await self._delegate(**kwargs)
        except ToolContractError as exc:
            decision = await self._callback(
                RecoveryEventKind.TOOL_CONTRACT_ERROR.value,
                {
                    "tool": self._delegate.name,
                    "error": str(exc),
                },
            )
            if (
                decision is not None
                and decision.action is RecoveryActionKind.STOP
            ):
                error = (
                    "recovery policy stopped after Tool contract failure: "
                    f"{self._delegate.name}"
                )
                return ToolOutcome(
                    status="blocked",
                    terminal=True,
                    result={
                        "status": "blocked",
                        "reason": "recovery_policy_stop",
                        "tool": self._delegate.name,
                    },
                    error=error,
                )
            raise


def _saved_reporting_definition_registry(
    workspace: Path,
    run_id: str,
    *,
    declarative: bool,
) -> DefinitionRegistry | None:
    """Restore Agent-visible Tool definitions captured by one declarative Run."""

    if not declarative:
        return None
    plan_path = workspace / "Work" / "runs" / run_id / "resolved-plan.json"
    if not plan_path.is_file():
        return None
    plan = ResolvedPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    if not plan.definition_snapshots:
        return None
    return restore_plan_definition_registry(plan)


def _reporting_tool_implementation_id(definition: ToolDefinition) -> str:
    prefix = "capability:distribution-reporting:"
    if not definition.implementation.startswith(prefix):
        raise ValueError(
            "unsupported declarative Reporting Tool implementation: "
            f"{definition.implementation}"
        )
    implementation_id = definition.implementation.removeprefix(prefix)
    if not implementation_id or ":" in implementation_id:
        raise ValueError(
            "unsupported declarative Reporting Tool implementation: "
            f"{definition.implementation}"
        )
    return implementation_id


def _apply_saved_reporting_tool_definition(
    tool: Tool,
    definition: ToolDefinition,
) -> Tool:
    """Apply one saved declarative projection to a fresh current Tool instance."""

    tool.name = definition.id
    tool.description = definition.instructions or definition.description
    tool.side_effect = definition.side_effect  # type: ignore[assignment]
    tool.parallel_safe = definition.parallel_safe
    tool.reuse_result = definition.reuse_result  # type: ignore[attr-defined]
    return tool


class _ContractBoundReportingTool:
    """Validate one Agent-visible Tool with its Run-frozen contracts."""

    def __init__(
        self,
        delegate: Tool,
        input_contract: ContractAdapter,
        output_contract: ContractAdapter,
    ) -> None:
        self._delegate = delegate
        self._input_contract = input_contract
        self._output_contract = output_contract

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    @property
    def result_index(self) -> Any:
        return getattr(self._delegate, "result_index")

    @result_index.setter
    def result_index(self, value: Any) -> None:
        self._delegate.result_index = value  # type: ignore[attr-defined]

    @property
    def task_id(self) -> str:
        return str(getattr(self._delegate, "task_id"))

    @task_id.setter
    def task_id(self, value: str) -> None:
        self._delegate.task_id = value  # type: ignore[attr-defined]

    async def __call__(self, **kwargs: Any) -> Any:
        try:
            validated = self._input_contract.validate(dict(kwargs))
        except ContractValidationError as exc:
            raise ToolContractError(
                f"{self.name} input contract: {exc}",
                code="declared_tool_input_contract",
            ) from exc
        if hasattr(validated, "model_dump"):
            validated = validated.model_dump(mode="python")
        if not isinstance(validated, Mapping):
            raise ToolContractError(
                f"{self.name} input contract must produce an object",
                code="declared_tool_input_contract",
            )
        result = await self._delegate(**dict(validated))
        try:
            self._output_contract.validate(result)
        except ContractValidationError as exc:
            raise ToolContractError(
                f"{self.name} output contract: {exc}",
                code="declared_tool_output_contract",
            ) from exc
        return result


class ReportingAgentRunner:
    """Retain one stable reporting identity for the lifetime of a workflow."""

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        llm_provider: LLMProvider,
        defaults: AgentDefaults,
        *,
        timeout: float | None = None,
        product_skill_root: Path | None = None,
        global_root: Path | None = None,
        provider_router: ProviderRouter | None = None,
        provider_admission: ProviderAdmissionController | None = None,
        provider_admission_controller: ProviderAdmissionController | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.llm_provider = llm_provider
        self.provider_router = provider_router or ProviderRouter(llm_provider)
        self.provider_admission = provider_admission or provider_admission_controller
        self.defaults = defaults
        self.global_root = (
            Path(global_root).resolve() if global_root is not None else None
        )
        # Reporting tasks are already bounded by provider idle timeouts, tool-turn
        # limits, and the run-level request/token budget.  A second wall-clock
        # deadline here used to cancel healthy long-form agents after 600 seconds,
        # even while they were actively persisting result parts.
        self.timeout = timeout
        self.store = ReportingStore(self.workspace)
        self.product_skill_root = (
            Path(product_skill_root).resolve()
            if product_skill_root is not None
            else Path(__file__).resolve().parents[3] / "ProductCapabilities/skills"
        )
        self.module_skills = RuntimeSkillResolver.resolve(
            ModuleSkillLibrary.packaged(),
            product_root=self.product_skill_root,
            project_root=self.workspace / "Capabilities/skills",
        )
        self._agent_execution = AgentExecutionService(bus, timeout=timeout)
        # Temporary inspection alias while Reporting-specific continuation and
        # result decoding are extracted in later FA-02 slices. The registry is
        # owned and mutated by AgentExecutionService.
        self._sessions = self._agent_execution.sessions
        self._session_route_bindings: dict[tuple[str, str], tuple[str, str]] = {}
        # Last typed boundary delivered to each durable identity.  The loop
        # keeps the lossless transcript; this small index only decides whether
        # the next task needs the full contract or an explicit delta.
        self._session_task_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._artifact_root = ArtifactGateway(
            self.workspace, ArtifactGrant("root", "root", "workflow", "root")
        )
        self._routers: dict[str, WorkflowMessageRouter] = {}
        self._provider_attempt_guard: Callable[[str, str], Awaitable[None]] | None = None
        # Typed context is scoped to the complete run/task/revision identity,
        # not to the durable professional Agent identity.  Keeping the map
        # separate from ``_sessions`` means a cached loop can safely receive a
        # fresh capsule when the workflow advances to another task.
        self._context_rebuilders: dict[
            tuple[str, str, str, int], ReportingContextRebuilder
        ] = {}

    def _loop_config(self, definition: AgentDefinition, envelope: TaskEnvelope) -> AgentDefaults:
        """Build task-sensitive limits without changing the durable Agent identity."""

        specialist = bool(re.fullmatch(r"module-2\.[1-5]-specialist", definition.id))
        long_reasoning_agent = specialist or definition.id in {
            "evidence-auditor",
            "cross-module-reviewer",
        }
        return self.defaults.model_copy(
            update={
                "max_tool_iterations": min(definition.max_turns, 40),
                "max_tokens": (
                    max(
                        definition.max_tokens or self.defaults.max_tokens,
                        32_768,
                    )
                    if long_reasoning_agent
                    else definition.max_tokens or self.defaults.max_tokens
                ),
                "max_tool_calls_per_round": (
                    max(self.defaults.max_tool_calls_per_round, len(TEMPLATE_ROLE_SKILL_IDS))
                    if envelope.task_id == "template-skill-distillation"
                    else max(self.defaults.max_tool_calls_per_round, 8)
                    if definition.id == "chief-editor"
                    else max(self.defaults.max_tool_calls_per_round, 8)
                    if specialist
                    else self.defaults.max_tool_calls_per_round
                ),
                "max_tool_result_chars": (
                    max(self.defaults.max_tool_result_chars, 160_000)
                    if envelope.task_id == "template-skill-distillation"
                    else (
                        max(self.defaults.max_tool_result_chars, 320_000)
                        if definition.id == "chief-editor-auditor"
                        else max(self.defaults.max_tool_result_chars, 180_000)
                        if definition.id
                        in {
                            "cross-module-reviewer",
                            "chief-editor",
                        }
                        else max(self.defaults.max_tool_result_chars, 32_000)
                        if specialist
                        else max(self.defaults.max_tool_result_chars, 10_000)
                        if definition.id == "evidence-auditor"
                        else self.defaults.max_tool_result_chars
                    )
                ),
                "working_memory_tokens": (
                    max(self.defaults.working_memory_tokens, 96_000)
                    if definition.id
                    in {
                        "cross-module-reviewer",
                        "chief-editor",
                        "chief-editor-auditor",
                    }
                    else max(self.defaults.working_memory_tokens, 80_000)
                    if envelope.task_id == "template-skill-distillation"
                    else max(self.defaults.working_memory_tokens, 64_000)
                    if specialist or definition.id == "evidence-auditor"
                    else self.defaults.working_memory_tokens
                ),
            }
        )

    def set_provider_attempt_guard(
        self,
        guard: Callable[[str, str], Awaitable[None]] | None,
    ) -> None:
        """Apply one run-scoped guard before every provider attempt."""

        self._provider_attempt_guard = guard

    @staticmethod
    def _usage_stage(definition: AgentDefinition, envelope: TaskEnvelope) -> str:
        outputs = set(envelope.allowed_outputs)
        if definition.id == "template-distiller":
            return "template_distillation"
        if definition.id == "evidence-auditor":
            return "module_review"
        if definition.id == "cross-module-reviewer":
            return "cross_review"
        if definition.id == "chief-editor-auditor":
            return "final_review"
        if definition.id == "chief-editor":
            return (
                "chief_revision"
                if "chief_revision_submission" in outputs
                else "chief_edit"
            )
        if definition.id == "main-agent":
            return "main_decision"
        if "module_revision_submission" in outputs:
            return "module_revision"
        if "module_submission" in outputs:
            return "module_authoring"
        return "reporting_other"

    @staticmethod
    def _provider_stream_idle_timeout(definition: AgentDefinition) -> float:
        """Return an explicit long idle window; provider defaults must not govern audits."""

        if definition.id in REPORTING_AUDIT_AGENT_IDS:
            return REPORTING_AUDIT_STREAM_IDLE_TIMEOUT_SECONDS
        return REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS

    def _record_identity(
        self,
        *,
        workflow_id: str,
        envelope: TaskEnvelope,
        identity_key: str,
        session_id: str,
        runtime_id: str,
        status: str,
    ) -> None:
        """Persist identities that Main has actually created, never a planned roster."""

        relative = f"Work/runs/{envelope.run_id}/agent-identities.json"
        path = self.workspace / relative
        with exclusive_file_lock(path.with_suffix(".lock")):
            registry = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.is_file()
                else {
                    "workflow_id": workflow_id,
                    "created_by": "main",
                    "identities": {},
                }
            )
            identities = registry.setdefault("identities", {})
            identity = identities.setdefault(
                identity_key,
                {
                    "agent_id": envelope.agent_id,
                    "identity_key": identity_key,
                    "session_id": session_id,
                    "runtime_id": runtime_id,
                    "created_on_first_dispatch": True,
                    "first_task_id": envelope.task_id,
                    "first_revision": envelope.revision,
                },
            )
            if identity["session_id"] != session_id or identity["runtime_id"] != runtime_id:
                raise RuntimeError(f"reporting identity changed inside workflow: {identity_key}")
            identity.update(
                {
                    "status": status,
                    "last_task_id": envelope.task_id,
                    "last_revision": envelope.revision,
                }
            )
            self.store.write_json(relative, registry)

    def _reporting_research_guard(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        workflow_id: str,
    ) -> Callable[[], None] | None:
        """Record research usage without stopping incomplete taxonomy coverage."""

        specialist = bool(re.fullmatch(r"module-2\.[1-5]-specialist", definition.id))
        if not specialist and definition.id != "evidence-auditor":
            return None
        key = f"{definition.id}:{envelope.task_id}:r{envelope.revision}"
        usage_path = self.workspace / f"Work/runs/{envelope.run_id}/research-tool-usage.json"

        def guard() -> None:
            with exclusive_file_lock(usage_path.with_suffix(".lock")):
                usage = (
                    json.loads(usage_path.read_text(encoding="utf-8"))
                    if usage_path.is_file()
                    else {}
                )
                used = int(usage.get(key, 0))
                usage[key] = used + 1
                self.store.write_json(
                    f"Work/runs/{envelope.run_id}/research-tool-usage.json",
                    usage,
                )

        return guard

    @staticmethod
    def _reporting_module_id(definition: AgentDefinition, envelope: TaskEnvelope) -> str | None:
        if not (
            re.fullmatch(r"module-2\.[1-5]-specialist", definition.id)
            or definition.id == "evidence-auditor"
        ):
            return None
        match = re.search(r"2\.[1-5]", envelope.task_id)
        return match.group(0) if match is not None else None

    def _selected_module_skills(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
    ):
        module_id: str | None = None
        if definition.id == "evidence-auditor":
            match = re.search(r"(2\.[1-5])", envelope.task_id)
            if match is None:
                raise ValueError(
                    f"{definition.id} task must identify one fixed owner module"
                )
            module_id = match.group(1)
        return self.module_skills.for_agent(
            definition.id,
            module_id=module_id,
            submodule_ids=(
                set(envelope.target_submodule_ids)
                if (
                    definition.id == "evidence-auditor"
                    or re.fullmatch(r"module-2\.[1-5]-specialist", definition.id)
                )
                else None
            ),
        )

    def _system_prompt(self, definition: AgentDefinition, envelope: TaskEnvelope) -> str:
        skills = self._selected_module_skills(definition, envelope)
        return PromptAssembler.system_prompt(
            definition,
            module_skills=skills,
            module_skill_index=None,
        )

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _provider_round_reason(phase: str, attempt: int = 1) -> str:
        """Return H1's explicit reason for one physical Provider attempt."""

        phase = str(phase or "initial")
        if int(attempt or 1) > 1:
            return "provider_retry"
        if phase == "tool_followup":
            return "evidence_lookup"
        if phase == "guard" or phase.startswith("guard"):
            return "tool_contract_error"
        if "continuation" in phase:
            return "long_output_continuation"
        if "correction" in phase or "revision" in phase:
            return "semantic_correction"
        return "direct_submit"

    @staticmethod
    def _continuation_limits(
        resolved_profile: ResolvedTaskExecutionProfile,
    ) -> dict[str, int]:
        """Derive finite continuation headroom without reducing task limits.

        Tool-boundary slices have no profile-count limit: productive tool work
        may continue until the typed result is submitted.  The independent
        no-progress guard below remains responsible for stopping stalled tool
        loops.  Output and correction continuations stay finite.
        """

        profile = resolved_profile.profile
        max_tokens_slices = max(
            1,
            min(
                3,
                (65_536 + profile.max_output_tokens - 1)
                // profile.max_output_tokens,
            ),
        )
        return {
            "max_tokens_continuation": max_tokens_slices,
            # A correction is already a dedicated extra model turn.  If that
            # turn reaches its tool boundary, allow exactly one lossless slice
            # to submit the typed result, but never a correction loop.
            "submission_correction": 1,
            "max_no_progress_observations": 1,
        }

    @staticmethod
    def _continuation_conversation_event_digests(loop: AgentLoop) -> set[str]:
        """Hash unique semantic events while ignoring harness-owned prompts.

        Sets intentionally ignore repeated copies of the same tool call/result,
        so changing call ids or appending an identical transcript is not
        mistaken for progress.
        """

        def normalized_content(value: Any) -> str:
            content = str(value or "")
            if (
                "<same_identity_continuation>" in content
                or "<submission_correction>" in content
                or "<progress_check>" in content
                or "<working_memory_checkpoint>" in content
            ):
                return ""
            marker = (
                "[Provider output reached the per-request max_tokens limit "
                "before a typed tool submission. The task is not complete.]"
            )
            content = content.replace(marker, "").strip()
            if not content:
                return ""
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                return content
            if isinstance(parsed, dict) and parsed.get("truncated") is True:
                parsed = dict(parsed)
                parsed.pop("full_result_ref", None)
                return json.dumps(
                    parsed,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            return content

        digests: set[str] = set()
        for message in loop._conversation_history:
            tool_calls = []
            for call in getattr(message, "tool_calls", None) or ():
                tool_calls.append(
                    {
                        "name": str(getattr(call, "name", "") or ""),
                        "arguments": dict(getattr(call, "arguments", {}) or {}),
                    }
                )
            event = {
                "role": str(getattr(message, "role", "") or ""),
                "content": normalized_content(getattr(message, "content", "")),
                "is_tool_result": bool(
                    getattr(message, "is_tool_result", False)
                ),
                "tool_calls": tool_calls,
            }
            if not event["content"] and not tool_calls:
                continue
            serialized = json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            digests.add(hashlib.sha256(serialized.encode("utf-8")).hexdigest())
        return digests

    async def _continuation_progress_snapshot(
        self,
        loop: AgentLoop,
        envelope: TaskEnvelope,
    ) -> dict[str, Any]:
        """Return content-free evidence of durable or conversational progress."""

        durable: dict[str, str] = {}
        run_root = self.workspace / "Work" / "runs" / envelope.run_id
        durable_roots = (
            run_root / "drafts" / envelope.task_id / f"r{envelope.revision}",
            run_root / "results" / "attempts" / envelope.task_id,
        )
        for root in durable_roots:
            if not root.is_dir():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = path.relative_to(self.workspace).as_posix()
                durable[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        canonical_result = run_root / "results" / f"{envelope.task_id}.json"
        if canonical_result.is_file():
            relative = canonical_result.relative_to(self.workspace).as_posix()
            durable[relative] = hashlib.sha256(
                canonical_result.read_bytes()
            ).hexdigest()

        result_parts: dict[str, Any] | None = None
        list_parts = loop.tools.get("list_result_parts")
        if list_parts is not None:
            try:
                listed = await list_parts()
            except Exception as exc:  # pragma: no cover - defensive telemetry
                result_parts = {"status": "unreadable", "error_type": type(exc).__name__}
            else:
                result_parts = {
                    "complete": bool(listed.get("complete", False)),
                    "ready_part_ids": sorted(listed.get("ready_part_ids", ())),
                    "missing_part_ids": sorted(listed.get("missing_part_ids", ())),
                    "rewrite_part_ids": sorted(listed.get("rewrite_part_ids", ())),
                    "parts": sorted(
                        (
                            str(item.get("part_id", "")),
                            int(item.get("characters", 0)),
                            bool(item.get("ready", False)),
                        )
                        for item in listed.get("parts", ())
                    ),
                }

        durable_payload = json.dumps(
            {"files": durable, "result_parts": result_parts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_digests = self._continuation_conversation_event_digests(loop)
        return {
            "durable_sha256": hashlib.sha256(
                durable_payload.encode("utf-8")
            ).hexdigest(),
            "durable_file_count": len(durable),
            "result_parts": result_parts,
            "conversation_event_sha256": sorted(event_digests),
        }

    @staticmethod
    def _claim_hash_occurrence(
        claim_root: Path,
        digest: str,
        occurrence: str,
    ) -> str:
        """Choose one first occurrence without a shared read/modify/write race."""

        claim_root.mkdir(parents=True, exist_ok=True)
        claim_path = claim_root / digest
        with exclusive_file_lock(claim_path.with_suffix(".lock")):
            if claim_path.is_file():
                return claim_path.read_text(encoding="utf-8")
            with claim_path.open("w", encoding="utf-8") as handle:
                handle.write(occurrence)
                handle.flush()
                os.fsync(handle.fileno())
            return occurrence

    def _merge_hash_index(self, index_path: Path, claims: dict[str, str]) -> None:
        """Maintain the legacy aggregate view through one process-safe reducer."""

        with exclusive_file_lock(index_path.with_suffix(".lock")):
            current = (
                json.loads(index_path.read_text(encoding="utf-8"))
                if index_path.is_file()
                else {}
            )
            for digest, occurrence in claims.items():
                current.setdefault(digest, occurrence)
            self.store.write_json(
                index_path.relative_to(self.workspace).as_posix(), current
            )

    def _write_context_manifest(
        self,
        *,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        identity_key: str,
        session_id: str,
        system_prompt: str,
        task_message: str,
        input_contract_payload: str | None,
        shared_artifacts: list[str],
        resolved_execution_profile: ResolvedTaskExecutionProfile | None = None,
    ) -> Path:
        """Persist content-free context provenance for one provider dispatch."""

        root = f"Work/runs/{envelope.run_id}/context-manifests"
        index_ref = f"{root}/hash-index.json"
        index_path = self.workspace / index_ref
        claims: dict[str, str] = {}
        claim_root = index_path.parent / "hash-index-claims"
        # v3 repeats are scoped to the complete logical identity.  Keep the
        # legacy aggregate claim index above for v1/v2 forensic readers; it is
        # intentionally not used for v3 character counters.
        v3_tracker = HashOccurrenceTracker(
            index_path.parent / "v3-hash-index",
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            identity_key=identity_key,
            revision=envelope.revision,
        )
        safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", envelope.task_id)
        task_sha256 = self._sha256_text(task_message)
        manifest_ref = (
            f"{root}/{safe_task_id}-r{envelope.revision}-{session_id}-"
            f"{task_sha256[:12]}.json"
        )

        def component(kind: str, value: str) -> dict:
            digest = self._sha256_text(value)
            occurrence = f"{manifest_ref}#{kind}"
            first = self._claim_hash_occurrence(claim_root, digest, occurrence)
            claims[digest] = first
            return {
                "kind": kind,
                "chars": len(value),
                "sha256": digest,
                "first_occurrence": first,
                "repeated_content": first != occurrence,
            }

        declared_modes = envelope.artifact_delivery_modes
        artifact_entries: list[dict] = []
        artifact_payloads: dict[str, bytes | str] = {}
        declared_refs = [
            *envelope.input_refs,
            *envelope.context_summary_refs,
            *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
            *shared_artifacts,
        ]
        for ref in dict.fromkeys(declared_refs):
            mode = declared_modes.get(ref, "reference")
            entry = {
                "ref": ref,
                "delivery_mode": mode,
                "bytes": None,
                "sha256": None,
                "first_occurrence": None,
                "repeated_content": False,
            }
            if not ref.startswith("artifact:v1:"):
                path = (self.workspace / ref).resolve()
                if path.is_relative_to(self.workspace) and path.is_file():
                    payload = path.read_bytes()
                    artifact_payloads[ref] = payload
                    digest = hashlib.sha256(payload).hexdigest()
                    occurrence = f"{manifest_ref}#artifact:{ref}"
                    first = self._claim_hash_occurrence(
                        claim_root, digest, occurrence
                    )
                    claims[digest] = first
                    entry.update(
                        {
                            "bytes": len(payload),
                            "sha256": digest,
                            "first_occurrence": first,
                            "repeated_content": first != occurrence,
                        }
                    )
            artifact_entries.append(entry)

        prompt_components = [
            component("system_prompt", system_prompt),
            component("task_message", task_message),
        ]
        if envelope.inline_context:
            prompt_components.append(
                component("inline_context", envelope.inline_context)
            )
        if input_contract_payload is not None:
            prompt_components.append(
                component("input_contract", input_contract_payload)
            )
        selected_skills = self._selected_module_skills(definition, envelope)
        v3_segments: list[dict[str, Any]] = []

        def v3_segment(
            kind: str,
            value: Any,
            *,
            label: str,
            stable: bool | None = None,
            duplicate: bool = False,
        ) -> None:
            v3_segments.append(
                v3_tracker.observe(
                    kind,
                    value,
                    ref=f"{manifest_ref}#segment:{label}",
                    occurrence=f"{manifest_ref}#segment:{label}",
                    stable=stable,
                    duplicate=duplicate,
                )
            )

        v3_segment("system_prompt", system_prompt, label="system_prompt", stable=True)
        v3_segment("task_contract", task_message, label="task_contract")
        v3_segment(
            "task_state_capsule",
            {
                "run_id": envelope.run_id,
                "task_id": envelope.task_id,
                "task_attempt_id": envelope.task_attempt_id,
                "revision": envelope.revision,
                "agent_id": definition.id,
                "objective": envelope.objective,
                "constraints": envelope.constraints,
                "allowed_outputs": envelope.allowed_outputs,
                "target_submodule_ids": envelope.target_submodule_ids,
            },
            label="task_state_capsule",
        )
        for index, ref in enumerate(envelope.context_summary_refs):
            v3_segment(
                "knowledge_slice",
                artifact_payloads.get(ref, ref),
                label=f"knowledge_slice-{index}",
            )
        for index, ref in enumerate(shared_artifacts):
            v3_segment(
                "evidence_slice",
                artifact_payloads.get(ref, ref),
                label=f"evidence_slice-{index}",
            )
        if input_contract_payload is not None:
            v3_segment("input_contract", input_contract_payload, label="input_contract")
        for index, skill in enumerate(selected_skills):
            v3_segment(
                "module_skill",
                skill.content,
                label=f"module_skill-{index}",
                stable=True,
            )
        for index, ref in enumerate(dict.fromkeys(declared_refs)):
            v3_segment(
                "artifact_ref",
                artifact_payloads.get(ref, ref),
                label=f"artifact_ref-{index}",
            )
        if envelope.prior_result_ref:
            v3_segment(
                "completed_result_ref",
                envelope.prior_result_ref,
                label="completed_result_ref",
            )
        manifest = {
            # v1 fields remain below (prompt_components/artifacts) so old
            # forensic readers keep working; v3 is the authoritative view.
            "context_manifest_version": 3,
            "legacy_context_manifest_version": 1,
            "schema_version": 3,
            "manifest_version": 3,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "task_attempt_id": envelope.task_attempt_id,
            "revision": envelope.revision,
            "agent_id": definition.id,
            "identity_key": identity_key,
            "session_id": session_id,
            "execution_profile": (
                resolved_execution_profile.model_dump(mode="json")
                if resolved_execution_profile is not None
                else None
            ),
            "input_contract_kind": envelope.input_contract_kind,
            "target_submodule_ids": envelope.target_submodule_ids,
            "prompt_components": prompt_components,
            "artifacts": artifact_entries,
            "module_skills": [
                {
                    "skill_id": skill.id,
                    "version": skill.version,
                    "scope": skill.scope,
                    "sha256": skill.sha256,
                    "submodules": list(skill.submodules),
                }
                for skill in selected_skills
            ],
            "delivery_mode_counts": {
                mode: sum(
                    entry["delivery_mode"] == mode
                    for entry in artifact_entries
                )
                for mode in ("inline", "reference", "hash_retained")
            },
        }
        manifest.update(
            build_manifest_payload(
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                identity_key=identity_key,
                revision=envelope.revision,
                task_attempt_id=envelope.task_attempt_id,
                session_id=session_id,
                segments=v3_segments,
                manifest_kind="context_manifest_v3",
                # Preserve the v1 fields already assembled above instead of
                # serializing any prompt/artifact body into v3.
            )
        )
        manifest.pop("manifest_sha256", None)
        manifest["manifest_sha256"] = self._sha256_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        self._merge_hash_index(index_path, claims)
        return self.store.write_json(manifest_ref, manifest)

    def _write_provider_call_manifest(
        self,
        *,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        identity_key: str,
        session_id: str,
        messages: list,
        tool_definitions: list[dict] | None,
        phase: str,
        attempt: int,
        call_index: int,
        resolved_execution_profile: ResolvedTaskExecutionProfile | None = None,
    ) -> Path:
        """Persist pre-adapter hashes, then backfill canonical provider payload metrics."""

        root = f"Work/runs/{envelope.run_id}/context-manifests"
        index_ref = f"{root}/provider-hash-index.json"
        index_path = self.workspace / index_ref
        claims: dict[str, str] = {}
        claim_root = index_path.parent / "provider-hash-index-claims"
        v3_tracker = HashOccurrenceTracker(
            index_path.parent / "v3-provider-hash-index",
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            identity_key=identity_key,
            revision=envelope.revision,
        )
        safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", envelope.task_id)
        safe_phase = re.sub(r"[^A-Za-z0-9_.-]", "_", phase)
        manifest_ref = (
            f"{root}/provider-calls/{safe_task_id}-r{envelope.revision}-"
            f"{session_id}-c{call_index:04d}-{safe_phase}-a{attempt}.json"
        )
        provider_call_id = Path(manifest_ref).stem

        def component(kind: str, value: str, *, index: int | None = None) -> dict:
            digest = self._sha256_text(value)
            occurrence = f"{manifest_ref}#{kind}"
            first = self._claim_hash_occurrence(claim_root, digest, occurrence)
            claims[digest] = first
            result = {
                "kind": kind,
                "chars": len(value),
                "sha256": digest,
                "first_occurrence": first,
                "repeated_content": first != occurrence,
            }
            if index is not None:
                result["index"] = index
            return result

        message_components: list[dict] = []
        v3_segments: list[dict[str, Any]] = []
        seen_v3_hashes: set[str] = set()

        def v3_segment(
            kind: str,
            value: Any,
            *,
            label: str,
            stable: bool | None = None,
        ) -> None:
            preview_digest, _ = sha256_value(value)
            item = v3_tracker.observe(
                kind,
                value,
                ref=f"{manifest_ref}#segment:{label}",
                occurrence=f"{manifest_ref}#segment:{label}",
                stable=stable,
                duplicate=preview_digest in seen_v3_hashes,
            )
            seen_v3_hashes.add(preview_digest)
            v3_segments.append(item)

        serialized_messages: list[dict] = []
        for index, message in enumerate(messages):
            payload = (
                message.model_dump(mode="json")
                if hasattr(message, "model_dump")
                else {
                    "role": getattr(message, "role", None),
                    "content": getattr(message, "content", None),
                    "tool_calls": getattr(message, "tool_calls", None),
                    "tool_call_id": getattr(message, "tool_call_id", None),
                    "is_tool_result": getattr(message, "is_tool_result", False),
                }
            )
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            item = component(
                f"message:{getattr(message, 'role', 'unknown')}",
                serialized,
                index=index,
            )
            item["role"] = getattr(message, "role", "unknown")
            item["is_tool_result"] = bool(
                getattr(message, "is_tool_result", False)
            )
            message_components.append(item)
            serialized_messages.append(payload)
            v3_segment(
                "tool_result"
                if bool(getattr(message, "is_tool_result", False))
                else ("system_prompt" if getattr(message, "role", "") == "system" else "message"),
                serialized,
                label=f"message-{index}",
                stable=(getattr(message, "role", "") == "system"),
            )

        serialized_tools = json.dumps(
            tool_definitions or [],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        serialized_request = json.dumps(
            {
                "messages": serialized_messages,
                "tools": tool_definitions or [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        v3_segment("tool_schema", serialized_tools, label="tool_schema")
        v3_segment(
            "task_contract",
            {
                "objective": envelope.objective,
                "allowed_outputs": envelope.allowed_outputs,
                "allowed_tools": envelope.allowed_tools,
                "input_contract_kind": envelope.input_contract_kind,
            },
            label="task_contract",
        )
        v3_segment(
            "task_state_capsule",
            {
                "run_id": envelope.run_id,
                "task_id": envelope.task_id,
                "task_attempt_id": envelope.task_attempt_id,
                "revision": envelope.revision,
            },
            label="task_state_capsule",
        )
        if envelope.input_contract_ref:
            v3_segment("input_contract", envelope.input_contract_ref, label="input_contract")
        for index, ref in enumerate(
            dict.fromkeys(
                [
                    *envelope.input_refs,
                    *envelope.context_summary_refs,
                    *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
                ]
            )
        ):
            v3_segment("artifact_ref", ref, label=f"artifact_ref-{index}")
        if envelope.prior_result_ref:
            v3_segment(
                "completed_result_ref",
                envelope.prior_result_ref,
                label="completed_result_ref",
            )
        request_sha256 = self._sha256_text(serialized_request)
        manifest = {
            "provider_context_manifest_version": 3,
            "schema_version": 3,
            "manifest_version": 3,
            "context_manifest_version": 3,
            "provider_call_id": provider_call_id,
            "provider_call_ref": manifest_ref,
            "provider_call_hash": request_sha256,
            "provider_call_sha256": request_sha256,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "task_attempt_id": envelope.task_attempt_id,
            "revision": envelope.revision,
            "agent_id": definition.id,
            "identity_key": identity_key,
            "session_id": session_id,
            "provider_call_index": call_index,
            "phase": phase,
            "attempt": attempt,
            "execution_profile": (
                resolved_execution_profile.model_dump(mode="json")
                if resolved_execution_profile is not None
                else None
            ),
            "message_count": len(messages),
            "provider_message_count": len(messages),
            "logical_task_message_count": sum(
                1
                for message in messages
                if not (
                    str(getattr(message, "content", "") or "").lstrip().startswith(
                        "<typed_task_state>"
                    )
                    or str(getattr(message, "content", "") or "").lstrip().startswith(
                        "<bounded_context>"
                    )
                )
            ),
            "messages": message_components,
            "tool_definitions": component("tool_definitions", serialized_tools),
            "request_sha256": request_sha256,
            "request_sha256_scope": "agent_pre_adapter",
            "pre_adapter_request": {
                "representation": "agent_llm_messages_and_tool_definitions_v1",
                "request_sha256": self._sha256_text(serialized_request),
                "message_chars": sum(
                    int(item["chars"]) for item in message_components
                ),
                "tool_schema_chars": len(serialized_tools),
            },
            "provider_payload_status": "pending",
            "provider_payload": None,
        }
        manifest.update(
            build_manifest_payload(
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                identity_key=identity_key,
                revision=envelope.revision,
                task_attempt_id=envelope.task_attempt_id,
                session_id=session_id,
                provider_call_id=provider_call_id,
                provider_call_ref=manifest_ref,
                provider_call_hash=request_sha256,
                manifest_kind="provider_context_observation",
                segments=v3_segments,
                # Keep the pre-adapter/provider payload fields above; the
                # v3 helper only adds hash-only segments and counters.
            )
        )
        manifest.pop("manifest_sha256", None)
        manifest["manifest_sha256"] = self._sha256_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        self._merge_hash_index(index_path, claims)
        return self.store.write_json(manifest_ref, manifest)

    def _finalize_provider_call_manifest(
        self,
        record: dict[str, Any],
    ) -> Path | None:
        """Attach hash-only metrics for the payload actually passed to the SDK."""

        relative = str(record.get("context_manifest_ref") or "")
        if not relative:
            return None
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            return None
        manifest = json.loads(path.read_text(encoding="utf-8"))
        provider_call_id = str(record.get("provider_call_id") or "")
        if provider_call_id != str(manifest.get("provider_call_id") or ""):
            raise ValueError(
                "usage provider_call_id does not match provider context manifest"
            )
        expected_ref = str(manifest.get("provider_call_ref") or relative)
        supplied_ref = record.get("provider_call_ref")
        if supplied_ref is not None and str(supplied_ref) != expected_ref:
            raise ValueError(
                "usage provider_call_ref does not match provider context manifest"
            )
        expected_hash = str(
            manifest.get("provider_call_hash")
            or manifest.get("request_sha256")
            or ""
        )
        supplied_hash = record.get("provider_call_hash")
        if supplied_hash is not None and str(supplied_hash) != expected_hash:
            raise ValueError(
                "usage provider_call_hash does not match provider context manifest"
            )
        observed = record.get("request_metric_source") == "provider_adapter_payload"
        manifest["provider_payload_status"] = (
            "observed" if observed else "unavailable"
        )
        manifest["provider_payload"] = (
            {
                "representation": record.get(
                    "provider_request_representation"
                ),
                "request_sha256": record.get("request_fingerprint"),
                "message_sha256": record.get("message_fingerprint"),
                "tool_schema_sha256": record.get(
                    "tool_schema_fingerprint"
                ),
                "request_chars": int(record.get("request_chars", 0) or 0),
                "message_chars": int(record.get("message_chars", 0) or 0),
                "tool_schema_chars": int(
                    record.get("tool_schema_chars", 0) or 0
                ),
            }
            if observed
            else None
        )
        manifest["usage_status"] = record.get("status")
        manifest["usage_source"] = record.get("usage_source")
        manifest["attempt_disposition"] = record.get("attempt_disposition")
        manifest["retry_decision"] = record.get("retry_decision")
        disposition = str(record.get("attempt_disposition") or "")
        provider_request_sent = record.get("provider_request_sent")
        if provider_request_sent is None:
            provider_request_sent = disposition not in {"not_sent", "pre_send"}
        provider_request_sent = bool(provider_request_sent)
        attempt = int(record.get("attempt", manifest.get("attempt", 1)) or 1)
        phase = str(record.get("phase") or manifest.get("phase") or "initial")
        if record.get("round_reason"):
            round_reason = str(record["round_reason"])
        elif attempt > 1:
            round_reason = "provider_retry"
        elif phase == "tool_followup":
            round_reason = "evidence_lookup"
        elif phase == "guard" or phase.startswith("guard"):
            round_reason = "tool_contract_error"
        elif "continuation" in phase:
            round_reason = "long_output_continuation"
        elif "correction" in phase or "revision" in phase:
            round_reason = "semantic_correction"
        else:
            round_reason = "direct_submit"
        manifest.update(
            {
                "provider_call_ref": expected_ref,
                "provider_call_hash": expected_hash or manifest.get("request_sha256"),
                "provider_request_sent": provider_request_sent,
                "attempt_kind": (
                    str(record.get("attempt_kind") or "provider_request")
                    if provider_request_sent
                    else str(record.get("attempt_kind") or "pre_send_block")
                ),
                "round_reason": round_reason,
                "pre_send_guard_status": record.get("pre_send_guard_status"),
                "rebuild_count": int(record.get("rebuild_count", 0) or 0),
                "context_manifest_version": 3,
            }
        )
        # Finalization changes usage fields, so refresh the manifest integrity
        # hash.  provider_call_hash remains the pre-adapter request hash and is
        # therefore stable across this update.
        manifest.pop("manifest_sha256", None)
        manifest["manifest_sha256"] = self._sha256_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        return self.store.write_json(relative, manifest)

    def _input_contract(self, envelope: TaskEnvelope):
        if not envelope.input_contract_kind or not envelope.input_contract_ref:
            return None
        model = INPUT_CONTRACT_TYPES.get(envelope.input_contract_kind)
        if model is None:
            return None
        path = (self.workspace / envelope.input_contract_ref).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            return None
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    def _reference_artifacts(self, envelope: TaskEnvelope) -> list[str]:
        """Return recursively declared refs which are reopenable in this task.

        The public TaskEnvelope fields remain the first compatibility source;
        typed input refs are added only after the gateway/compiler has checked
        them.  Inline/hash-retained values stay out of the tool allowlist.
        """

        try:
            typed_input = self._input_contract(envelope)
        except Exception:
            # ``_tools`` will report the typed contract error with its strict
            # compiler context.  Keep this helper side-effect free for legacy
            # callers that use it while constructing a prompt.
            typed_input = None
        declared = collect_reference_refs(envelope, typed_input=typed_input)
        return [
            ref
            for ref in declared
            if envelope.artifact_delivery_modes.get(ref) == "reference"
        ]

    @staticmethod
    def _task_submission_schema(
        kind: str,
        contract,
        *,
        module_id: str | None = None,
    ) -> dict:
        """Specialize provider-visible review schemas to the exact active task."""

        schema = deepcopy(submission_schema(kind))

        def remove_property(node: dict, name: str) -> None:
            node.get("properties", {}).pop(name, None)
            node["required"] = [field for field in node.get("required", []) if field != name]

        if isinstance(contract, ModuleAuthoringInput):
            schema.get("properties", {}).get("module_id", {}).update({"const": contract.module_id})
            schema.get("properties", {}).get("revision", {}).update({"const": contract.revision})
        elif isinstance(contract, ModuleRevisionInput):
            schema.get("properties", {}).get("module_id", {}).update({"const": contract.module_id})
            schema.get("properties", {}).get("base_revision", {}).update(
                {"const": contract.subject.revision}
            )
            schema.get("properties", {}).get("revision", {}).update(
                {"const": contract.subject.revision + 1}
            )
        elif isinstance(contract, ModuleReviewInput):
            remove_property(schema, "coverage")
            finding = schema.get("$defs", {}).get("ModuleReviewFinding", {})
            remove_property(finding, "id")
            finding.get("properties", {}).get("target_submodule_id", {}).update(
                {"enum": list(contract.required_submodule_ids)}
            )
            allowed_evidence = [
                contract.subject_ref,
                contract.validation_report_ref,
                *(item.evidence_id for item in contract.evidence),
            ]
            finding.get("properties", {}).get("evidence_refs", {}).setdefault("items", {}).update(
                {"enum": list(dict.fromkeys(allowed_evidence))}
            )
            if kind == "module_review_verdict_submission":
                remove_property(
                    schema.get("$defs", {}).get("ResolutionVerdict", {}),
                    "finding_id",
                )
                required_count = len(contract.required_findings)
                schema.get("properties", {}).get("verdicts", {}).update(
                    {"minItems": required_count, "maxItems": required_count}
                )
        elif isinstance(contract, CrossReviewInput):
            remove_property(schema, "coverage")
            cross_finding = schema.get("$defs", {}).get("CrossReviewFinding", {})
            remove_property(cross_finding, "id")
            cross_finding.get("properties", {}).get("owner_module_id", {}).update(
                {"const": contract.owner_module_id}
            )
            cross_finding.get("properties", {}).get(
                "target_submodule_ids", {}
            ).setdefault("items", {}).update(
                {
                    "enum": list(
                        REPORT_TAXONOMY[contract.owner_module_id].submodules
                    )
                }
            )
            if kind == "cross_review_verdict_submission":
                remove_property(
                    schema.get("$defs", {}).get("ResolutionVerdict", {}),
                    "finding_id",
                )
                required_count = len(contract.required_findings)
                schema.get("properties", {}).get("verdicts", {}).update(
                    {"minItems": required_count, "maxItems": required_count}
                )
        elif isinstance(contract, CrossOwnerInput):
            # A Cross-owner lane is bound to one durable owner identity.  The
            # runtime allocates newly-created finding ids, while rechecks must
            # copy one exact verdict id for each immutable prior finding.
            schema.get("properties", {}).get("owner_module_id", {}).update(
                {"const": contract.owner_module_id}
            )
            coverage = schema.get("$defs", {}).get("CrossReviewCoverageEntry", {})
            coverage.get("properties", {}).get("module_id", {}).update(
                {"const": contract.owner_module_id}
            )
            finding = schema.get("$defs", {}).get("CrossReviewFinding", {})
            remove_property(finding, "id")
            if kind == "cross_owner_verdict_submission":
                # Initial synthesis is immutable runtime-owned input. Recheck
                # may report regressions but cannot rewrite an accepted relation.
                remove_property(schema, "synthesis_inputs")
                required_ids = [finding.id for finding in contract.required_findings]
                verdicts = schema.get("properties", {}).get("verdicts", {})
                verdicts.update(
                    {"minItems": len(required_ids), "maxItems": len(required_ids)}
                )
                schema.get("$defs", {}).get("ResolutionVerdict", {}).get(
                    "properties", {}
                ).get("finding_id", {}).update({"enum": required_ids})
        elif isinstance(contract, FinalReviewInput):
            remove_property(schema, "checked_section_ids")
            remove_property(
                schema.get("$defs", {}).get("FinalReviewFinding", {}),
                "id",
            )
            if kind == "final_review_verdict_submission":
                remove_property(
                    schema.get("$defs", {}).get("ResolutionVerdict", {}),
                    "finding_id",
                )
                required_count = len(contract.required_findings)
                schema.get("properties", {}).get("verdicts", {}).update(
                    {"minItems": required_count, "maxItems": required_count}
                )
        elif isinstance(contract, ChiefChapterLaneInput):
            schema.get("properties", {}).get("run_id", {}).update(
                {"const": contract.run_id}
            )
            schema.get("properties", {}).get("chapter_id", {}).update(
                {"const": contract.chapter_id}
            )
            schema.get("properties", {}).get("revision", {}).update(
                {"const": contract.revision}
            )
            section_ids = schema.get("properties", {}).get("section_ids", {})
            if kind == "chief_chapter_lane_submission":
                section_ids.update({"const": list(contract.section_ids)})
            else:
                section_ids.get("items", {}).update({"enum": list(contract.section_ids)})
                section_ids.update({"minItems": 1})
                schema.get("properties", {}).get("base_subject_ref", {}).update(
                    {"const": contract.subject_ref}
                )
        elif isinstance(contract, FinalChapterLaneInput):
            schema.get("properties", {}).get("run_id", {}).update(
                {"const": contract.run_id}
            )
            schema.get("properties", {}).get("chapter_id", {}).update(
                {"const": contract.chapter_id}
            )
            schema.get("properties", {}).get("checked_section_ids", {}).update(
                {"const": list(contract.section_ids)}
            )
            finding = schema.get("$defs", {}).get("ChapterScopedFinalReviewFinding", {})
            remove_property(finding, "id")
            if kind == "final_chapter_lane_verdict_submission":
                verdict_schema = schema.get("$defs", {}).get("ResolutionVerdict", {})
                remove_property(verdict_schema, "finding_id")
                required_ids = [finding.id for finding in contract.required_findings]
                schema.get("properties", {}).get("verdicts", {}).update(
                    {"minItems": len(required_ids), "maxItems": len(required_ids)}
                )
                verdict_schema.get("properties", {}).get("finding_id", {}).update(
                    {"enum": required_ids}
                )
        examples = schema.get("examples", [])
        if examples and isinstance(examples[0], dict):
            example = examples[0]
            if isinstance(contract, ModuleAuthoringInput):
                example["module_id"] = contract.module_id
                example["revision"] = contract.revision
            elif isinstance(contract, ModuleRevisionInput):
                example["module_id"] = contract.module_id
                example["base_revision"] = contract.subject.revision
                example["revision"] = contract.subject.revision + 1
            elif isinstance(contract, CrossOwnerInput):
                example["owner_module_id"] = contract.owner_module_id
                if isinstance(example.get("coverage"), dict):
                    example["coverage"]["module_id"] = contract.owner_module_id
            elif isinstance(contract, ChiefChapterLaneInput):
                example["run_id"] = contract.run_id
                example["chapter_id"] = contract.chapter_id
                example["section_ids"] = list(contract.section_ids)
                example["revision"] = contract.revision
                if kind == "chief_chapter_lane_revision_submission":
                    example["base_subject_ref"] = contract.subject_ref
            elif isinstance(contract, FinalChapterLaneInput):
                example["run_id"] = contract.run_id
                example["chapter_id"] = contract.chapter_id
                example["checked_section_ids"] = list(contract.section_ids)
                target_section_id = contract.section_ids[0]
                for field in ("findings", "new_findings"):
                    for value in example.get(field, []):
                        if not isinstance(value, dict):
                            continue
                        value["target_section_ids"] = [target_section_id]
                        value["target_changes"] = [
                            {
                                "target_section_id": target_section_id,
                                "required_change": (
                                    "补充该结论对应的证据边界，"
                                    "并明确尚待确认的项目事实。"
                                ),
                                "reviewer_checks": [
                                    "修改后能够从结论直接追溯到"
                                    "当前运行的证据。"
                                ],
                            }
                        ]
                        value["evidence_refs"] = [contract.subject_ref]
            if not isinstance(contract, CrossOwnerInput):
                example.pop("coverage", None)
            example.pop("checked_section_ids", None)
            if isinstance(contract, FinalChapterLaneInput):
                example["checked_section_ids"] = list(contract.section_ids)
            for field in ("findings", "new_findings"):
                values = example.get(field, [])
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, dict):
                            value.pop("id", None)
            if isinstance(contract, ModuleReviewInput):
                for field in ("findings", "new_findings"):
                    for value in example.get(field, []):
                        value["target_submodule_id"] = contract.required_submodule_ids[0]
                        value["evidence_refs"] = [contract.subject_ref]
            if kind.endswith("_verdict_submission"):
                required_findings = list(getattr(contract, "required_findings", []))
                base_verdicts = example.get("verdicts", [])
                base = (
                    dict(base_verdicts[0])
                    if isinstance(base_verdicts, list) and base_verdicts
                    else {
                        "verdict": "resolved",
                        "reason": "当前修订满足原 finding 的 reviewer checks。",
                        "evidence_refs": [],
                    }
                )
                base.pop("finding_id", None)
                if isinstance(contract, ModuleReviewInput):
                    base["evidence_refs"] = [contract.subject_ref]
                elif isinstance(contract, CrossReviewInput):
                    base["evidence_refs"] = [next(iter(contract.module_refs.values()))]
                elif isinstance(contract, CrossOwnerInput):
                    base["evidence_refs"] = [contract.owner_subject_ref]
                elif isinstance(contract, FinalReviewInput):
                    base["evidence_refs"] = [contract.subject_ref]
                if isinstance(contract, CrossOwnerInput):
                    example["verdicts"] = [
                        {
                            **deepcopy(base),
                            "finding_id": finding.id,
                        }
                        for finding in required_findings
                    ]
                else:
                    example["verdicts"] = [deepcopy(base) for _ in required_findings]
        return schema

    @staticmethod
    def _result_part_item_schema(
        expected_part_ids: list[str],
        *,
        evidence_binding_required: bool,
        section_body_only: bool = False,
        chapter_four_planned_headings: bool = False,
        chapter_id: str | None = None,
    ) -> dict:
        """Return the exact provider-visible shape for one current-task prose part."""

        section_numbering_guidance = ""
        if section_body_only:
            section_by_part = {
                part_id: section_id
                for section_id, part_id in CHIEF_SECTION_RESULT_PART_IDS.items()
                if part_id in expected_part_ids
            }
            assignments = ", ".join(
                f"{part_id} -> **{section_id}.x ...**"
                for part_id, section_id in section_by_part.items()
            )
            section_numbering_guidance = (
                " This is a static Chief section body: do not include any numbered "
                "Markdown heading, including the section's own heading; the runtime adds "
                "report headings. Unnumbered internal labels are allowed. If you choose "
                "bold numbered internal labels, derive them below the exact assigned "
                f"section rather than the chapter: {assignments}. Never use a chapter-level "
                f"shortcut such as **{chapter_id}.x ...**, and never restart at **1.1 ...** "
                "outside section 1.1. This is a writing convention, not a rejection rule."
            )
        chapter_four_numbering_guidance = ""
        if chapter_four_planned_headings:
            chapter_four_numbering_guidance = (
                " This is the complete dynamic Chapter 4 part: preserve every planned "
                "### 4.n heading exactly once and in plan order. Numbered descendant "
                "headings such as #### 4.1.1 are allowed under their matching planned "
                "parent, with deeper structure written as ##### 4.1.1.1; do not add another "
                "top-level 4.n section. Chapter 4 numbered structure must use these explicit "
                "Markdown headings; never replace it with bold numbered labels such as "
                "**4.1.1 ...** or restart it at **1.1 ...**."
            )

        part_id_schema: dict = {
            "type": "string",
            "description": "One fixed part id assigned by the current task.",
        }
        if expected_part_ids:
            part_id_schema["enum"] = list(expected_part_ids)
        else:
            part_id_schema["pattern"] = r"^[A-Za-z0-9._-]+$"
        properties = {
            "part_id": part_id_schema,
            "content": {
                "type": "string",
                "minLength": 1,
                "maxLength": 48_000,
                "description": (
                    "Complete reader-visible prose for this one durable part. Always "
                    "supply the full intended prose. Every call must contain all required "
                    "arguments. After a successful write, use list_result_parts and keep "
                    "an already-ready part without rewriting it unless correction feedback "
                    "explicitly names that part."
                    + section_numbering_guidance
                    + chapter_four_numbering_guidance
                ),
            },
        }
        required = ["part_id", "content"]
        if evidence_binding_required:
            properties["evidence_ids"] = {
                "type": "array",
                "items": {"type": "string", "pattern": r"^E-"},
                "uniqueItems": True,
                "description": (
                    "Registered current-run E-* ids supporting this module part; "
                    "use an empty list for an explicit evidence gap."
                ),
            }
            required.append("evidence_ids")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @classmethod
    def _result_part_tool_schema(
        cls,
        expected_part_ids: list[str],
        *,
        evidence_binding_required: bool,
        section_body_only: bool = False,
        chapter_four_planned_headings: bool = False,
        chapter_id: str | None = None,
        batch_size: int | None = None,
    ) -> dict:
        """Specialize single and batch result-part tools to the active task."""

        item_schema = cls._result_part_item_schema(
            expected_part_ids,
            evidence_binding_required=evidence_binding_required,
            section_body_only=section_body_only,
            chapter_four_planned_headings=chapter_four_planned_headings,
            chapter_id=chapter_id,
        )
        if batch_size is None:
            return item_schema
        return {
            "type": "object",
            "properties": {
                "parts": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": batch_size,
                    "items": item_schema,
                    "description": (
                        "A validation-atomic batch; every item is checked before any "
                        "part is persisted."
                    ),
                }
            },
            "required": ["parts"],
            "additionalProperties": False,
        }

    def skill_provenance(self) -> list[SkillProvenance]:
        return [
            SkillProvenance(
                skill_id=skill.id,
                version=skill.version,
                sha256=skill.sha256,
                scope=skill.scope,
            )
            for skill in self.module_skills.skills
        ]

    def _task_correlation(
        self,
        envelope: TaskEnvelope,
        *,
        workflow_id: str,
        identity_key: str,
        session_id: str,
        identity_lease: IdentityLease,
        execution_profile_sha256: str = "0" * 64,
    ) -> TaskCorrelation:
        """Bind one dispatch to its immutable inputs and active identity lease."""

        input_contract_ref = envelope.input_contract_ref
        input_contract_sha256: str | None = None
        subject_ref: str | None = None
        subject_sha256: str | None = None
        if input_contract_ref:
            contract_path = (self.workspace / input_contract_ref).resolve()
            if (
                not contract_path.is_relative_to(self.workspace)
                or not contract_path.is_file()
            ):
                raise ValueError("task input contract is not a readable workspace artifact")
            contract_bytes = contract_path.read_bytes()
            input_contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
            try:
                contract_payload = json.loads(contract_bytes)
            except (TypeError, ValueError) as exc:
                raise ValueError("task input contract is not valid JSON") from exc
            candidate_ref = next(
                (
                    contract_payload.get(field)
                    for field in ("subject_ref", "base_subject_ref")
                    if isinstance(contract_payload.get(field), str)
                ),
                None,
            )
            if candidate_ref:
                candidate_path = (self.workspace / candidate_ref).resolve()
                if (
                    not candidate_path.is_relative_to(self.workspace)
                    or not candidate_path.is_file()
                ):
                    raise ValueError("task subject is not a readable workspace artifact")
                subject_ref = candidate_ref
                subject_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        task_envelope_sha256 = hashlib.sha256(
            json.dumps(
                envelope.model_dump(
                    mode="json",
                    exclude={"task_attempt_id"},
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return TaskCorrelation(
            workflow_id=workflow_id,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            task_attempt_id=envelope.task_attempt_id,
            agent_id=envelope.agent_id,
            identity_key=identity_key,
            session_id=session_id,
            task_envelope_sha256=task_envelope_sha256,
            execution_profile_sha256=execution_profile_sha256,
            input_contract_ref=input_contract_ref,
            input_contract_sha256=input_contract_sha256,
            subject_ref=subject_ref,
            subject_sha256=subject_sha256,
            lease_owner_id=identity_lease.owner_id,
            lease_epoch=identity_lease.lease_epoch,
        )

    @staticmethod
    def _same_recoverable_task(
        previous: TaskCorrelation,
        current: TaskCorrelation,
    ) -> bool:
        excluded = {"task_attempt_id", "lease_owner_id", "lease_epoch"}
        return previous.model_dump(exclude=excluded) == current.model_dump(
            exclude=excluded
        )

    def _tools(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        session_id: str,
        workflow_id: str,
        *,
        gateway: ArtifactGateway | None = None,
        shared_artifacts: list[str] | None = None,
        task_correlation: TaskCorrelation | None = None,
        recovery_event_callback: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        gateway = gateway or scoped_gateway(
            self._artifact_root,
            workflow_id=workflow_id,
            envelope=envelope,
            agent_id=definition.id,
            session_id=session_id,
        )
        declared_modes = envelope.artifact_delivery_modes
        reference_shared_artifacts = [
            ref
            for ref in (shared_artifacts or [])
            if ref not in declared_modes or declared_modes[ref] == "reference"
        ]
        access = compile_agent_access(
            definition,
            envelope,
            self._reference_artifacts(envelope),
            gateway=gateway,
            shared_refs=reference_shared_artifacts,
        )
        # One result index belongs to the run, not to a process-local Agent
        # session.  Read-only wrappers below consult it before executing a
        # duplicate local parse/search.
        result_index = RunToolResultIndex(self.workspace, envelope.run_id)
        result_index.capabilities = access.capabilities  # type: ignore[attr-defined]
        ledger = SourceLedger(self.workspace, envelope.run_id)
        module_id = self._reporting_module_id(definition, envelope)
        evidence_memory = (
            EvidenceResearchMemory(self.workspace, envelope.run_id, module_id)
            if module_id is not None
            else None
        )
        input_snapshot_path = (
            self.workspace
            / f"Work/runs/{envelope.run_id}/input-snapshot.json"
        )
        if input_snapshot_path.is_file():
            input_snapshot = RunInputSnapshotStore(self.workspace).load(
                envelope.run_id
            )
            library = ReferenceLibrary(
                self.workspace,
                knowledge_root=input_snapshot.scope_root(
                    self.workspace, "Knowledge"
                ),
                index_root=(
                    self.workspace
                    / f"Work/runs/{envelope.run_id}/indexes/knowledge"
                ),
                global_root=self.global_root,
            )
        else:
            library = ReferenceLibrary(
                self.workspace,
                global_root=self.global_root,
            )
        key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
        web = BraveWebResearchBackend(key) if key else DisabledWebResearchBackend()
        research_guard = self._reporting_research_guard(definition, envelope, workflow_id)
        template_inspection: TemplateDistillationInput | None = None
        if envelope.task_id == "template-skill-distillation":
            if (
                definition.id != "template-distiller"
                or envelope.input_contract_kind != "template_distillation_input"
                or not envelope.input_contract_ref
            ):
                raise ValueError("template distillation requires its exact typed input contract")
            contract_path = (self.workspace / envelope.input_contract_ref).resolve()
            if not contract_path.is_relative_to(self.workspace) or not contract_path.is_file():
                raise ValueError(
                    "template distillation input contract is not a readable workspace artifact"
                )
            template_inspection = TemplateDistillationInput.model_validate_json(
                contract_path.read_text(encoding="utf-8")
            )
            if template_inspection.run_id != envelope.run_id:
                raise ValueError("template distillation input contract belongs to another run")
            template_ref = Path(template_inspection.template_ref)
            if (
                template_ref.is_absolute()
                or ".." in template_ref.parts
                or template_ref.as_posix() != template_inspection.template_ref
            ):
                raise ValueError(
                    "template distillation template_ref is not one canonical workspace file"
                )
            logical_template_path = self.workspace / template_ref
            template_path = logical_template_path.resolve()
            if (
                not template_path.is_relative_to(self.workspace)
                or not template_path.is_file()
            ):
                raise ValueError(
                    "template distillation template_ref is not one canonical workspace file"
                )
            if logical_template_path.is_symlink():
                content_ref = template_path.relative_to(self.workspace)
                try:
                    ContentAddressedStore(self.workspace).resolve_blob(content_ref)
                except (FileNotFoundError, ValueError) as exc:
                    raise ValueError(
                        "template distillation template_ref symlink is not one verified CAS view"
                    ) from exc
            elif template_path != logical_template_path:
                raise ValueError(
                    "template distillation template_ref is not one canonical workspace file"
                )
        input_contract = self._input_contract(envelope)
        if len(envelope.allowed_outputs) > 1:
            raise ValueError(
                "one exact allowed_output is required per typed task; "
                "union submission schemas are not exposed to the Provider"
            )
        task_submission_schemas = {
            kind: self._task_submission_schema(
                kind,
                input_contract,
                module_id=module_id,
            )
            for kind in envelope.allowed_outputs
        }
        expected_result_part_ids = (
            list(template_inspection.required_part_ids)
            if template_inspection is not None
            else list(input_contract.required_submodule_ids)
            if isinstance(input_contract, ModuleAuthoringInput)
            else list(input_contract.target_submodule_ids)
            if isinstance(input_contract, ModuleRevisionInput)
            else [
                CHIEF_SECTION_RESULT_PART_IDS[section_id]
                for section_id in input_contract.target_section_ids
            ]
            if isinstance(input_contract, ChiefRevisionInput)
            else (
                ["special_topic_analysis"]
                if input_contract.chapter_id == "4"
                else [
                    CHIEF_SECTION_RESULT_PART_IDS[section_id]
                    for section_id in input_contract.section_ids
                ]
            )
            if isinstance(input_contract, ChiefChapterLaneInput)
            else [
                part_id
                for part_id in CHIEF_RESULT_PART_IDS
                if (
                    part_id != "special_topic_analysis"
                    or not isinstance(
                        input_contract, (ChiefEditorInput, AggregateEditorInput)
                    )
                    or input_contract.special_topic_plan is not None
                )
            ]
            if "edited_report_submission" in envelope.allowed_outputs
            else envelope.target_submodule_ids
        )
        evidence_binding_required = bool(
            {
                "module_submission",
                "module_revision_submission",
            }
            & set(envelope.allowed_outputs)
        )
        section_body_only = bool(
            isinstance(input_contract, ChiefChapterLaneInput)
            and input_contract.chapter_id != "4"
        )
        special_topic_plan = (
            input_contract.special_topic_plan
            if isinstance(input_contract, ChiefChapterLaneInput)
            and input_contract.chapter_id == "4"
            else None
        )
        required_synthesis_input_ids: list[str] = []
        available: dict[str, Tool] = {
            "search_project_evidence": SearchProjectEvidenceTool(
                self.workspace,
                ledger,
                research_guard,
                evidence_memory,
                run_id=envelope.run_id,
            ),
            "open_project_source": OpenProjectSourceTool(
                self.workspace,
                ledger,
                research_guard,
                evidence_memory,
                run_id=envelope.run_id,
            ),
            "search_reference_library": SearchReferenceLibraryTool(library, ledger),
            "open_reference": OpenReferenceTool(library, ledger),
            "web_search": WebSearchTool(web),
            "open_web_source": OpenWebSourceTool(web, ledger),
            "open_source": OpenWebSourceTool(web, ledger),
            "inspect_document": InspectDocumentTool(
                self.workspace,
                one_shot=template_inspection is not None,
                allow_template_distiller_source=template_inspection is not None,
                required_path=(
                    template_inspection.template_ref if template_inspection is not None else None
                ),
                required_max_chars=(
                    template_inspection.inspect_max_chars
                    if template_inspection is not None
                    else None
                ),
                cache_ref=(
                    f"Work/runs/{envelope.run_id}/context/template-inspection.json"
                    if template_inspection is not None
                    else None
                ),
            ),
            "inspect_image": _IndexedInspectImageTool(
                self.workspace,
                gateway=gateway,
                capabilities=access.capabilities,
                allowed_refs=access.readable_refs,
                photo_refs=access.photo_map(),
                result_index=result_index,
                task_id=envelope.task_id,
            ),
            "open_artifact": _IndexedOpenArtifactTool(
                gateway,
                default_limit=(
                    160_000
                    if definition.id
                    in {
                        "cross-module-reviewer",
                        "chief-editor",
                        "chief-editor-auditor",
                    }
                    else 8000
                    if definition.id == "evidence-auditor"
                    else 4000
                ),
                minimum_limit=(
                    160_000
                    if definition.id
                    in {
                        "cross-module-reviewer",
                        "chief-editor",
                    }
                    else 1
                ),
                maximum_limit=(
                    160_000
                    if definition.id
                    in {
                        "cross-module-reviewer",
                        "chief-editor",
                        "chief-editor-auditor",
                    }
                    else 8000
                ),
                allowed_refs=access.readable_refs,
                result_index=result_index,
                task_id=envelope.task_id,
            ),
            "open_tool_result": _IndexedOpenToolResultTool(
                gateway,
                result_index=result_index,
                task_id=envelope.task_id,
            ),
            "search_text": _IndexedSearchTextTool(
                gateway,
                research_guard,
                allowed_refs=access.readable_refs,
                result_index=result_index,
                task_id=envelope.task_id,
            ),
            "calculate": CalculateTool(),
            "publish_research_note": PublishResearchNoteTool(
                self.workspace,
                self.bus,
                workflow_id,
                envelope.run_id,
                envelope.task_id,
                definition.id,
            ),
            "query_peer": QueryPeerTool(
                self.bus, envelope.task_id, definition.id, session_id, workflow_id
            ),
            "reply_peer": ReplyPeerTool(self.bus, definition.id, workflow_id),
            "report_gap": ReportGapTool(
                definition.id,
                envelope.run_id,
                envelope.task_id,
                self.store,
                self.bus,
                workflow_id,
            ),
            "submit_result": SubmitResultTool(
                definition.id,
                session_id,
                envelope.run_id,
                envelope.task_id,
                self.store,
                self.bus,
                workflow_id,
                allowed_outputs=envelope.allowed_outputs,
                revision=envelope.revision,
                input_contract_kind=envelope.input_contract_kind,
                input_contract_ref=envelope.input_contract_ref,
                submission_schemas=task_submission_schemas,
                task_correlation=task_correlation,
                recovery_event_callback=recovery_event_callback,
            ),
            "write_result_part": WriteResultPartTool(
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
                self.store,
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
                section_body_only=section_body_only,
                special_topic_plan=special_topic_plan,
                required_synthesis_input_ids=required_synthesis_input_ids,
            ),
            "list_result_parts": ListResultPartsTool(
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
                self.store,
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
                section_body_only=section_body_only,
                special_topic_plan=special_topic_plan,
                required_synthesis_input_ids=required_synthesis_input_ids,
            ),
            "report_blocked": ReportBlockedTool(
                definition.id,
                session_id,
                envelope.run_id,
                envelope.task_id,
                self.store,
                self.bus,
                workflow_id,
                task_correlation=task_correlation,
            ),
        }
        if definition.id == "product-skill-maintainer":
            available["product_skill_evolution"] = ProductSkillEvolutionTool(
                self.product_skill_root.parents[1]
            )
        saved_definition_registry = _saved_reporting_definition_registry(
            self.workspace,
            envelope.run_id,
            declarative=recovery_policy is not None,
        )
        saved_tool_definitions = (
            {
                item.id: item
                for item in saved_definition_registry.all(DefinitionKind.TOOL)
                if isinstance(item, ToolDefinition)
            }
            if saved_definition_registry is not None
            else {}
        )
        saved_contracts = (
            build_contract_catalog(saved_definition_registry)
            if saved_definition_registry is not None
            else {}
        )
        for name in access.tool_names:
            saved_definition = saved_tool_definitions.get(name)
            if saved_definition is not None and not saved_definition.model_visible:
                continue
            implementation_id = (
                _reporting_tool_implementation_id(saved_definition)
                if saved_definition is not None
                else name
            )
            if implementation_id not in available:
                raise ValueError(f"unsupported tool in {definition.id}: {name}")
            tool = available[implementation_id]
            if saved_definition is not None:
                tool = _apply_saved_reporting_tool_definition(tool, saved_definition)
                input_contract = saved_contracts.get(saved_definition.input_contract)
                output_contract = saved_contracts.get(saved_definition.output_contract)
                if (
                    name not in {"submit_result", "write_result_part"}
                    and input_contract is not None
                    and output_contract is not None
                    and input_contract.json_schema()
                ):
                    tool = _ContractBoundReportingTool(
                        tool,
                        input_contract,
                        output_contract,
                    )
            registry.register(tool)
        for name in (
            "open_artifact",
            "open_tool_result",
            "search_text",
            "inspect_image",
        ):
            tool = registry.get(name)
            if tool is not None:
                # The index is an injected run-level dependency.  The concrete
                # bounded readers retain their legacy call/schema behaviour;
                # H2 callers may opt into lookup/record without broadening the
                # provider-visible contract.
                tool.result_index = result_index  # type: ignore[attr-defined]
                tool.task_id = envelope.task_id  # type: ignore[attr-defined]
        # Keep the compiler object attached to the registry for local callers
        # and tests; it is not serialized into provider-visible schemas.
        registry.capabilities = access.capabilities  # type: ignore[attr-defined]
        registry.capability_access = access  # type: ignore[attr-defined]
        registry.result_index = result_index  # type: ignore[attr-defined]
        if registry.get("inspect_image") is not None:
            registry._schema_cache["inspect_image"] = {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Authorized image path or current-run P-ID.",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Authorized current-run image reference.",
                    },
                },
                "anyOf": [{"required": ["path"]}, {"required": ["ref"]}],
                "additionalProperties": False,
            }
        if registry.get("write_result_part") is not None:
            # Expose one exact write shape to providers. The former automatic batch
            # companion encouraged providers to stringify ``parts`` or omit fields,
            # then retry already-persisted prose. Internal callers may still use the
            # batch implementation, but model-facing reporting identities always use
            # the single-part protocol plus list_result_parts for durable state.
            registry._schema_cache["write_result_part"] = self._result_part_tool_schema(
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
                section_body_only=section_body_only,
                chapter_four_planned_headings=special_topic_plan is not None,
                chapter_id=(
                    input_contract.chapter_id
                    if isinstance(input_contract, ChiefChapterLaneInput)
                    else None
                ),
            )
        if registry.get("submit_result") is not None:
            output_schemas = list(task_submission_schemas.values())
            if not output_schemas:
                raise ValueError(
                    f"{definition.id} has submit_result but no known allowed output contract"
                )
            submission_tool_schema = (
                {
                    **output_schemas[0],
                    "description": (
                        f"{output_schemas[0].get('description', '').strip()} "
                        "The submit_result tool arguments are this submission object "
                        "itself: put kind and every declared field at the top level. "
                        "Never add a payload wrapper or JSON-stringify the object."
                    ).strip(),
                }
                if len(output_schemas) == 1
                else {
                    "type": "object",
                    "oneOf": output_schemas,
                    "description": (
                        "Submit exactly one of the output contracts explicitly allowed "
                        "by this task. The tool arguments are the selected submission "
                        "object itself; put kind and every declared field at the top "
                        "level. Never add a payload wrapper or JSON-stringify the object."
                    ),
                }
            )
            registry._schema_cache["submit_result"] = submission_tool_schema
        for name, saved_definition in saved_tool_definitions.items():
            if name in {"submit_result", "write_result_part"}:
                continue
            input_contract = saved_contracts.get(saved_definition.input_contract)
            if registry.get(name) is not None and input_contract is not None:
                schema = input_contract.json_schema()
                if schema:
                    registry._schema_cache[name] = schema
        if recovery_event_callback is not None:
            # Registration above already captured each concrete Tool schema.
            # Replace only the private executable map so declarative Reporting
            # can dispatch ToolContractError without changing AgentLoop, Kernel,
            # or the Provider-visible Tool contract.
            registry._tools = {
                name: _RecoveryAwareReportingTool(tool, recovery_event_callback)
                for name, tool in registry._tools.items()
            }
        return registry

    @staticmethod
    def _identity_key(
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        session_key: str | None,
    ) -> str:
        """Scope concurrent module tasks without weakening durable role ownership."""

        if definition.id == "evidence-auditor" and session_key:
            return session_key
        if (
            definition.id == "cross-module-reviewer"
            and session_key
            and session_key.startswith("cross-owner-")
        ):
            # Cross-owner lanes share one packaged reviewer definition but must
            # keep one durable Provider identity per owner module.  The same
            # key is intentionally reused for that owner's recheck.
            return session_key
        if definition.id in {"chief-editor", "chief-editor-auditor"} and session_key:
            # Chapter lanes are independent durable identities.  Revisions and
            # rechecks keep the same chapter prefix so one chapter's lease or
            # cached context can never block another chapter lane.
            for prefix in ("chief-chapter-", "final-chapter-"):
                if session_key.startswith(prefix):
                    return session_key.split("-r", 1)[0]
        return definition.id

    @staticmethod
    def _runtime_id(
        definition: AgentDefinition,
        identity_key: str,
        session_id: str,
    ) -> str:
        """Expose the durable reporting identity to UI/history routing.

        Shared packaged roles (Auditor, Cross, Chief and Final) use one
        definition but several independent business identities.  Prefixing the
        runtime id with the identity key keeps the left rail from collapsing
        those lanes under one generic role label.
        """

        runtime_role = (
            identity_key
            if identity_key != definition.id
            else definition.id
        )
        return f"{runtime_role}--{session_id}"

    # ------------------------------------------------------------------
    # Typed Provider-context lifecycle (H3)
    # ------------------------------------------------------------------
    @staticmethod
    def _context_store_identity(
        envelope: TaskEnvelope,
    ) -> tuple[str, str, str, int]:
        """Return the complete key used for a typed context capsule."""

        return (
            str(envelope.run_id),
            str(envelope.task_id),
            str(envelope.agent_id),
            int(envelope.revision),
        )

    def _context_stores(
        self, envelope: TaskEnvelope
    ) -> tuple[TaskStateStore, ToolResultMemoStore]:
        """Create run-scoped stores with task/revision identity checks.

        ``TaskStateStore`` intentionally keeps one atomic sequence per run.  The
        runner selects entries by the full task/revision key below, so advancing
        one durable Agent to another task cannot make the later task read the
        previous task's current pointer.
        """

        return (
            TaskStateStore(
                self.workspace,
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
            ),
            ToolResultMemoStore(
                self.workspace,
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
            ),
        )

    @staticmethod
    def _manifest_matches_envelope(
        manifest: ContextManifest, envelope: TaskEnvelope
    ) -> bool:
        return (
            manifest.run_id == envelope.run_id
            and manifest.task_id == envelope.task_id
            and manifest.revision == envelope.revision
        )

    def _load_verified_context_manifest(
        self,
        store: TaskStateStore,
        envelope: TaskEnvelope,
    ) -> ContextManifest | None:
        """Load only a hash- and identity-verified capsule.

        A missing state sequence is a normal first run.  A present matching
        entry whose bytes or canonical hash are invalid is a hard failure: a
        crash/resume path must never fall back to a forensic conversation.
        Entries for another task in the same run are ignored and do not bleed
        into this task's Provider context.
        """

        pointer = store.current_pointer()
        if pointer is not None:
            pointer_identity = (
                pointer.get("run_id"),
                pointer.get("task_id"),
                pointer.get("revision"),
            )
            expected_identity = (
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
            )
            if pointer_identity == expected_identity:
                try:
                    return store.load(
                        expected_sha256=str(pointer.get("manifest_sha256") or "")
                        or None,
                        run_id=envelope.run_id,
                        task_id=envelope.task_id,
                        revision=envelope.revision,
                    )
                except FileNotFoundError:
                    # The pointer may have been written just before a process
                    # crash.  The append-only sequence remains the only safe
                    # migration source; continue with the identity-filtered
                    # lookup below.
                    pass

        # Do not use a different task's current pointer.  Search the immutable
        # sequence for the newest exact identity and verify its recorded hash.
        for entry in reversed(store._read_sequence()):  # type: ignore[attr-defined]
            if (
                entry.get("run_id") != envelope.run_id
                or entry.get("task_id") != envelope.task_id
                or int(entry.get("revision", -1)) != envelope.revision
            ):
                continue
            reference = str(entry.get("ref") or "")
            if not reference:
                raise ValueError("typed context sequence entry lacks a manifest ref")
            return store.load(
                reference,
                expected_sha256=str(entry.get("manifest_sha256") or "") or None,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                revision=envelope.revision,
            )
        return None

    def _context_slice(
        self,
        ref: str,
        *,
        kind: str,
        gateway: ArtifactGateway | None,
        include_content: bool = True,
        max_chars: int = 4096,
    ) -> EvidenceSlice | KnowledgeSlice:
        """Bind one envelope ref to a hash-addressed, bounded context slice."""

        digest: str | None = None
        content: str | None = None
        canonical_ref = str(ref)
        target: Path | None = None
        if gateway is not None:
            try:
                descriptor = gateway.describe(canonical_ref)
                canonical_ref = descriptor.canonical_ref
                digest = descriptor.sha256
                target = gateway._resolve(canonical_ref)
            except Exception:
                # A typed contract can carry provenance refs that are not
                # reopenable in the current task.  Keep their identity as a
                # hash-only hint instead of inventing a path or reading a
                # legacy transcript.
                target = None
        if target is None:
            candidate = (self.workspace / canonical_ref).resolve()
            if candidate.is_relative_to(self.workspace) and candidate.is_file():
                target = candidate
                try:
                    payload = candidate.read_bytes()
                    digest = hashlib.sha256(payload).hexdigest()
                    if include_content:
                        try:
                            content = payload.decode("utf-8")[:max_chars]
                        except UnicodeDecodeError:
                            content = None
                except OSError:
                    target = None
        if digest is None:
            digest = hashlib.sha256(canonical_ref.encode("utf-8")).hexdigest()
        common = {
            "ref": canonical_ref,
            "sha256": digest,
            "content": content,
            "summary": (
                re.sub(r"\s+", " ", content).strip()[:768]
                if content
                else None
            ),
            "chars": len(content or ""),
        }
        if kind == "knowledge":
            return KnowledgeSlice(kind="knowledge", **common)
        return EvidenceSlice(kind="evidence", **common)

    def _context_rebuilder_for_task(
        self,
        *,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        identity_key: str,
        session_id: str,
        system_prompt: str,
        gateway: ArtifactGateway | None,
        shared_artifacts: list[str],
    ) -> ReportingContextRebuilder:
        """Create/load one typed capsule and return its Provider rebaser."""

        cache_key = self._context_store_identity(envelope)
        state_store, memo_store = self._context_stores(envelope)
        loaded = self._load_verified_context_manifest(state_store, envelope)
        evidence_refs = list(
            dict.fromkeys(
                [
                    *envelope.input_refs,
                    *shared_artifacts,
                ]
            )
        )
        knowledge_refs = list(
            dict.fromkeys(
                [
                    *envelope.context_summary_refs,
                    *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
                ]
            )
        )
        evidence = [
            self._context_slice(
                ref,
                kind="evidence",
                gateway=gateway,
                include_content=True,
            )
            for ref in evidence_refs
            if ref
        ]
        knowledge = [
            self._context_slice(
                ref,
                kind="knowledge",
                gateway=gateway,
                include_content=True,
            )
            for ref in knowledge_refs
            if ref
        ]

        rebuilder = _RunnerContextRebuilder(
            self.workspace,
            store=state_store,
            memo_store=memo_store,
            persist=True,
        )
        required_tool_names = list(
            dict.fromkeys([*envelope.allowed_tools, *definition.tools])
        )
        if loaded is not None and loaded.task_state is not None:
            capsule = loaded.task_state
            # The durable task state is tied to run/task/revision.  A fresh
            # attempt id may change after a crash, but never changes that
            # semantic identity; update the attempt marker and re-hash it.
            if capsule.input_refs and list(capsule.input_refs) != list(envelope.input_refs):
                raise ValueError("typed context capsule input refs mismatch")
            capsule_updates: dict[str, Any] = {}
            if capsule.task_attempt_id != envelope.task_attempt_id:
                capsule_updates["task_attempt_id"] = envelope.task_attempt_id
            if required_tool_names and any(
                item not in capsule.required_tool_names for item in required_tool_names
            ):
                capsule_updates["required_tool_names"] = list(
                    dict.fromkeys([*capsule.required_tool_names, *required_tool_names])
                )
                capsule_updates["allowed_tool_names"] = list(
                    dict.fromkeys([*capsule.allowed_tool_names, *required_tool_names])
                )
            if capsule_updates:
                capsule = capsule.model_copy(
                    update=capsule_updates
                ).with_hash()
                loaded = loaded.model_copy(
                    update={
                        "task_state_capsule": capsule,
                        "manifest_sha256": None,
                    }
                ).with_hash()
                state_store.save(loaded)
            rebuilder.load_verify(
                loaded,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                revision=envelope.revision,
                expected_sha256=loaded.manifest_sha256,
            )
        else:
            capsule = TaskStateCapsule(
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                revision=envelope.revision,
                task_attempt_id=envelope.task_attempt_id,
                objective=envelope.objective,
                input_refs=list(dict.fromkeys(envelope.input_refs)),
                evidence_refs=list(dict.fromkeys(item.ref for item in evidence)),
                knowledge_refs=list(dict.fromkeys(item.ref for item in knowledge)),
                required_tool_names=required_tool_names,
                allowed_tool_names=required_tool_names,
                state={
                    "identity_key": identity_key,
                    "agent_id": definition.id,
                    "session_id": session_id,
                    "artifact_delivery_modes": dict(envelope.artifact_delivery_modes),
                },
            ).with_hash()
            rebuilder.begin_task(
                envelope=envelope,
                stable_prefix=[LLMMessage(role="system", content=system_prompt)],
                evidence=evidence,
                knowledge=knowledge,
                capsule=capsule,
                persist=True,
            )
        self._context_rebuilders[cache_key] = rebuilder
        return rebuilder

    @staticmethod
    def _sync_context_messages(
        rebuilder: ReportingContextRebuilder,
        messages: list[Any],
    ) -> None:
        """Record complete tool pairs without ever reading forensic traces."""

        known_calls = getattr(rebuilder, "_calls", {})
        known_results = getattr(rebuilder, "_results", {})
        consumed = getattr(rebuilder, "_consumed_calls", set())
        for message in messages:
            calls = list(getattr(message, "tool_calls", None) or ())
            if getattr(message, "role", None) == "assistant" and calls:
                if any(str(getattr(call, "id", "") or "") not in known_calls for call in calls):
                    rebuilder.record_tool_call(message)
                known_calls = getattr(rebuilder, "_calls", known_calls)
            call_id = str(getattr(message, "tool_call_id", "") or "")
            if not call_id or not bool(getattr(message, "is_tool_result", False)):
                continue
            if call_id in known_results or call_id in consumed:
                continue
            try:
                rebuilder.record_tool_result(message, consumed=False)
            except (TypeError, ValueError, RuntimeError):
                # A malformed forensic tail is never promoted into the typed
                # Provider context; the atomic parser will ignore it as well.
                continue
            known_results = getattr(rebuilder, "_results", known_results)

    @staticmethod
    def _context_reason_turn(
        rebuilder: ReportingContextRebuilder | None,
        *,
        content: str,
        turn_kind: str,
        prior_output: str | None = None,
    ) -> None:
        if rebuilder is None:
            return
        try:
            partial_tail = []
            if prior_output:
                # Provider calls are stateless.  Once the full evidence slices
                # become ref/summary-only, retain the immediately preceding
                # model conclusion or partial output as the semantic bridge to
                # a correction/continuation round.  The full forensic trace is
                # still never used as prompt input.
                partial_tail.append(
                    LLMMessage(role="assistant", content=prior_output[-2048:])
                )
            partial_tail.append(LLMMessage(role="user", content=content))
            rebuilder.record_terminal(
                "in_progress",
                next_action=turn_kind,
                continuation_required=True,
                partial_tail=partial_tail,
            )
        except (RuntimeError, ValueError):
            # A legacy loop may expose the hook but not a writable capsule;
            # preserving its legacy behavior is safer than reconstructing from
            # a forensic transcript.
            return

    @staticmethod
    def _context_terminal(
        rebuilder: ReportingContextRebuilder | None,
        *,
        status: str,
        result: AgentResult | None = None,
    ) -> None:
        if rebuilder is None:
            return
        try:
            rebuilder.record_terminal(
                status,
                next_action=None,
                continuation_required=False,
                partial_tail=[],
            )
        except (RuntimeError, ValueError):
            return

    @staticmethod
    def _typed_context_pre_send_guard(**kwargs: Any) -> dict[str, Any] | None:
        """Treat the typed rebuild as the context gate's successful decision.

        ``AgentLoop`` still performs its normal duplicate check on subsequent
        attempts.  Returning an explicit allow for the first rebuilt payload
        avoids emitting a second zero-cost ``pre_send_rebuild`` ledger row for
        the same physical Provider request.
        """

        if kwargs.get("rebuilt"):
            return {
                "status": "allow",
                "reason": "typed_context_rebased",
                "rebuild_count": max(1, int(kwargs.get("rebuild_count", 1) or 1)),
            }
        return None

    @staticmethod
    def _task_context_state(
        envelope: TaskEnvelope,
        *,
        shared_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the compact identity state used for full-vs-delta prompts."""

        return {
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "revision": envelope.revision,
            "agent_id": envelope.agent_id,
            "objective": envelope.objective,
            "input_refs": list(envelope.input_refs),
            "constraints": list(envelope.constraints),
            "allowed_outputs": list(envelope.allowed_outputs),
            "allowed_tools": list(envelope.allowed_tools),
            "input_contract_kind": envelope.input_contract_kind,
            "input_contract_ref": envelope.input_contract_ref,
            "prior_result_ref": envelope.prior_result_ref,
            "context_summary_refs": list(envelope.context_summary_refs),
            "inline_context": envelope.inline_context,
            "target_submodule_ids": list(envelope.target_submodule_ids),
            "artifact_delivery_modes": dict(envelope.artifact_delivery_modes),
            "shared_artifacts": list(shared_artifacts or []),
        }

    def _restore_persisted_session(
        self,
        loop: AgentLoop,
        *,
        envelope: TaskEnvelope,
        runtime_id: str,
        recovery_driver: AgentRecoveryDriver | None = None,
    ) -> bool:
        """Restore one identity from bounded business restart state.

        The new turn receives current typed input plus canonical references.
        Malformed or unrelated state starts the identity with empty history.
        """

        restore = self._session_restore_state(
            envelope=envelope,
            runtime_id=runtime_id,
            recovery_driver=recovery_driver,
        )
        if restore is None:
            return False
        loop.restore_conversation(
            restore.messages,
            task_boundaries=restore.task_boundaries,
            handoff_summary=restore.handoff_summary,
        )
        return True

    def _session_restore_state(
        self,
        *,
        envelope: TaskEnvelope,
        runtime_id: str,
        recovery_driver: AgentRecoveryDriver | None = None,
    ) -> AgentSessionRestore | None:
        """Load Capability persistence into the generic session restore shape."""

        payload = self._load_persisted_identity_state(
            envelope=envelope,
            runtime_id=runtime_id,
        )
        if payload is None:
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        if recovery_driver is not None and not recovery_driver.restore_attempts(
            payload.get("recovery_attempts", {})
        ):
            return None
        return AgentSessionRestore(
            messages=messages,
            task_boundaries=[
                {"current": payload["identity_state"], "sequence": 1}
            ],
        )

    def _load_persisted_identity_state(
        self,
        *,
        envelope: TaskEnvelope,
        runtime_id: str,
    ) -> dict[str, Any] | None:
        safe_runtime_id = re.sub(r"[^A-Za-z0-9_.-]", "_", runtime_id)
        manifest_path = (
            self.workspace
            / f"Work/runs/{envelope.run_id}/agent-conversations/{safe_runtime_id}.json"
        )
        if not manifest_path.is_file():
            return None
        try:
            payload = load_conversation_trace(self.workspace, manifest_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if payload.get("run_id") not in {None, envelope.run_id}:
            return None
        if payload.get("agent_id") not in {None, envelope.agent_id}:
            return None
        return payload

    async def run(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        workflow_id: str,
        session_key: str | None = None,
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ) -> AgentResult:
        """Run one typed task while holding a cross-process identity fence."""

        identity_key = self._identity_key(definition, envelope, session_key)
        # One public run() call is one dispatch/requeue.  model_copy() is used
        # heavily to build follow-up envelopes, so a copied construction-time
        # value must never make two dispatches share an immutable result path.
        envelope.task_attempt_id = f"attempt-{uuid4().hex}"
        manager = IdentityLeaseManager(self.workspace, envelope.run_id)
        lease_handle = manager.acquire(
            workflow_id,
            identity_key,
            owner_id=(
                f"{IdentityLeaseManager.default_owner_id()}:"
                f"{envelope.task_attempt_id}"
            ),
        )
        task_lease = None
        try:
            if self.provider_admission is not None:
                # The durable identity lease remains the cross-process fence;
                # this in-process task lease prevents two typed dispatches for
                # the same (workflow, identity) from entering cached-session
                # setup concurrently.
                task_lease = await self.provider_admission.task_acquire(
                    f"{workflow_id}:{identity_key}", envelope.task_id
                )
            return await self._run_with_identity_lease(
                definition,
                envelope,
                shared_artifacts,
                workflow_id=workflow_id,
                session_key=session_key,
                identity_lease=lease_handle.lease,
                recovery_policy=recovery_policy,
            )
        finally:
            if task_lease is not None:
                await task_lease.release()
            lease_handle.release()

    async def _run_with_identity_lease(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        workflow_id: str,
        session_key: str | None = None,
        identity_lease: IdentityLease,
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ) -> AgentResult:
        recovery_driver = AgentRecoveryDriver(recovery_policy)

        def apply_recovery_policy(
            event_kind: RecoveryEventKind,
            expected_action: RecoveryActionKind,
            detail: dict[str, Any] | None = None,
        ):
            decision = recovery_driver.decide(event_kind, detail)
            if decision is None:
                return None
            if decision.action is RecoveryActionKind.FAIL:
                raise RuntimeError(
                    f"recovery policy {recovery_policy.id} failed "
                    f"{event_kind.value}"
                )
            if decision.action not in {
                expected_action,
                RecoveryActionKind.STOP,
            }:
                raise RuntimeError(
                    f"recovery policy action {decision.action.value} does not match "
                    f"the reporting path requiring {expected_action.value} for "
                    f"{event_kind.value}"
                )
            return decision

        async def recovery_event_callback(
            event_kind: str,
            detail: dict[str, Any],
        ) -> Any:
            return apply_recovery_policy(
                RecoveryEventKind(event_kind),
                RecoveryActionKind.CORRECT,
                detail,
            )

        async def provider_recovery_decider(
            detail: dict[str, Any],
        ) -> str | None:
            action = await recovery_driver.provider_decision(detail)
            if action is None:
                return None
            if RecoveryActionKind(action) not in {
                RecoveryActionKind.CONTINUE,
                RecoveryActionKind.STOP,
                RecoveryActionKind.FAIL,
            }:
                raise RuntimeError(
                    f"recovery policy action {action} cannot "
                    "handle provider_error"
                )
            return action

        router = self._routers.get(workflow_id)
        if router is None:
            router = WorkflowMessageRouter(self.bus, workflow_id)
            self._routers[workflow_id] = router
        inherited_refs = artifact_path_refs(
            [
                *router.research_notes,
                *router.gaps,
                *router.blocked_notices,
            ]
        )
        module_id = self._reporting_module_id(definition, envelope)
        if module_id is not None and definition.id != "evidence-auditor":
            memory_ref = (
                EvidenceResearchMemory(self.workspace, envelope.run_id, module_id)
                .ensure()
                .as_posix()
            )
            inherited_refs.insert(0, memory_ref)
        shared_artifacts = list(dict.fromkeys([*shared_artifacts, *inherited_refs]))
        # Specialists already have one definition per module.  Module auditors
        # share one professional definition but require one durable identity per
        # module so their evidence scope and review history cannot bleed across
        # modules.  Revisions keep the same session_key and therefore the same
        # auditor identity.
        identity_key = self._identity_key(definition, envelope, session_key)
        if (
            identity_lease.workflow_id != workflow_id
            or identity_lease.identity_key != identity_key
        ):
            raise RuntimeError("identity lease does not match the typed task")
        cache_key = (workflow_id, identity_key)
        cached = self._sessions.get(cache_key)
        task_kind = self._usage_stage(definition, envelope)
        base_config = self._loop_config(definition, envelope)
        resolved_profile, routed_provider, resolved_config = (
            self.provider_router.resolve(
                definition,
                envelope,
                task_kind=task_kind,
                base_config=base_config,
            )
        )
        route_binding = (
            resolved_profile.resolved_provider_route,
            resolved_profile.resolved_model,
        )
        previous_binding = self._session_route_bindings.get(cache_key)
        if previous_binding is not None and previous_binding != route_binding:
            raise RuntimeError(
                "reporting identity provider route/model changed inside one lifecycle"
            )
        gateway = scoped_gateway(
            self._artifact_root,
            workflow_id=workflow_id,
            envelope=envelope,
            agent_id=definition.id,
            session_id=(cached[1] if cached is not None else "pending"),
        )
        prompt_definition = definition.model_copy(
            update={
                "effort": resolved_profile.profile.effort,
                "model": resolved_profile.resolved_model,
            }
        )
        system_prompt = self._system_prompt(prompt_definition, envelope)
        recovery_session_id = (
            cached[1]
            if cached is not None
            else "session-"
            + hashlib.sha256(
                f"{workflow_id}:{identity_key}".encode("utf-8")
            ).hexdigest()[:12]
        )
        recovery_correlation = self._task_correlation(
            envelope,
            workflow_id=workflow_id,
            identity_key=identity_key,
            session_id=recovery_session_id,
            identity_lease=identity_lease,
            execution_profile_sha256=resolved_profile.profile_sha256,
        )
        attempt_store = TaskAttemptStore(self.workspace, envelope.run_id)
        previous_correlation = attempt_store.current(envelope.task_id)
        if (
            previous_correlation is not None
            and self._same_recoverable_task(
                previous_correlation,
                recovery_correlation,
            )
        ):
            recovered = attempt_store.load_verified_result(previous_correlation)
            if recovered is not None:
                terminal, payload = recovered
                recovered_result = AgentResult.model_validate(payload)
                if terminal.status != recovered_result.status.value:
                    raise RuntimeError(
                        "persisted task result status does not match its terminal"
                    )
                if (
                    recovered_result.task_id != envelope.task_id
                    or recovered_result.run_id != envelope.run_id
                    or recovered_result.agent_id != definition.id
                    or recovered_result.session_id != recovery_session_id
                ):
                    raise RuntimeError(
                        "persisted task result does not match its Agent identity"
                    )
                # Only a completed typed result closes this semantic task.  An
                # incomplete/failed/blocked attempt remains forensic evidence,
                # but an explicit later dispatch must be allowed to create a
                # fresh attempt rather than returning that non-success forever.
                if terminal.status == AgentRunStatus.COMPLETED.value:
                    runtime_id = self._runtime_id(
                        definition,
                        identity_key,
                        recovery_session_id,
                    )

                    async def reuse_completed_result(
                        _directive: AgentRecoveryDirective,
                    ) -> AgentResult:
                        self._record_identity(
                            workflow_id=workflow_id,
                            envelope=envelope,
                            identity_key=identity_key,
                            session_id=recovery_session_id,
                            runtime_id=runtime_id,
                            status="completed",
                        )
                        return recovered_result

                    async def stop_completed_result(
                        directive: AgentRecoveryDirective,
                    ) -> AgentResult:
                        return recovered_result.model_copy(
                            update={
                                "status": AgentRunStatus.INCOMPLETE,
                                "reason": (
                                    directive.reason
                                    or "completed result recovery stopped"
                                ),
                            }
                        )

                    return await self._agent_execution.recover_completed_result(
                        recovery=recovery_driver,
                        detail={
                            "task_id": envelope.task_id,
                            "source": "persisted_result",
                        },
                        reuse_result=reuse_completed_result,
                        stop=stop_completed_result,
                    )
        # Provider call manifests are immutable audit evidence, not execution
        # locks. A failed request did not create a validated local result, so
        # an explicit same-run resume may create a fresh physical attempt.
        # Reporting uses the AgentLoop's lossless conversation history.  The
        # experimental typed context rebaser was removed from the runtime path
        # after a real run showed that consumed tool results were reduced to
        # unusable memo refs, causing agents to reopen the same artifacts.
        context_rebuilder: ReportingContextRebuilder | None = None
        if cached is None:
            identity_digest = hashlib.sha256(
                f"{workflow_id}:{identity_key}".encode("utf-8")
            ).hexdigest()[:12]
            session_id = f"session-{identity_digest}"
            task_correlation = self._task_correlation(
                envelope,
                workflow_id=workflow_id,
                identity_key=identity_key,
                session_id=session_id,
                identity_lease=identity_lease,
                execution_profile_sha256=resolved_profile.profile_sha256,
            )
            TaskAttemptStore(self.workspace, envelope.run_id).activate(task_correlation)
            gateway = scoped_gateway(
                self._artifact_root,
                workflow_id=workflow_id,
                envelope=envelope,
                agent_id=definition.id,
                session_id=session_id,
            )
            runtime_id = self._runtime_id(definition, identity_key, session_id)
            config = resolved_config
            loop_kwargs = {
                "agent_type": runtime_id,
                "workspace": self.workspace,
                "tools": self._tools(
                    definition,
                    envelope,
                    session_id,
                    workflow_id,
                    gateway=gateway,
                    shared_artifacts=shared_artifacts,
                    task_correlation=task_correlation,
                    recovery_event_callback=(
                        recovery_event_callback
                        if recovery_policy is not None
                        else None
                    ),
                    recovery_policy=recovery_policy,
                ),
                "bus": self.bus,
                "config": config,
                "llm_provider": routed_provider,
                "system_prompt": system_prompt,
                "usage_run_id": envelope.run_id,
                "usage_task_id": envelope.task_id,
                "artifact_gateway": gateway,
                "before_provider_attempt": (
                    None
                    if self._provider_attempt_guard is None
                    else lambda: self._provider_attempt_guard(definition.id, envelope.task_id)
                ),
                "provider_recovery_decider": (
                    provider_recovery_decider
                    if recovery_policy is not None
                    else None
                ),
            }
            if self.provider_admission is not None:
                loop_kwargs.update(
                    {
                        "provider_admission": self.provider_admission,
                        "provider_admission_provider": (
                            resolved_profile.resolved_provider_route
                        ),
                        "provider_admission_model": resolved_profile.resolved_model,
                        "provider_admission_identity_key": (
                            f"{workflow_id}:{identity_key}"
                        ),
                    }
                )
            def create_session_loop() -> AgentLoop:
                loop = AgentLoop(**loop_kwargs)
                # Reporting persists identity and canonical refs in its v4
                # state. In-memory compaction remains available, but its
                # process summary is not another durable artifact.
                loop.persist_handoff_summary = False
                return loop

            execution_session = await self._agent_execution.start_or_restore(
                workflow_id=workflow_id,
                conversation_key=identity_key,
                runtime_id=runtime_id,
                session_id=session_id,
                session_factory=create_session_loop,
                restore=self._session_restore_state(
                    envelope=envelope,
                    runtime_id=runtime_id,
                    recovery_driver=recovery_driver,
                ),
            )
            loop = execution_session.loop
            session_id = execution_session.session_id
            runtime_id = execution_session.runtime_id
            self._session_route_bindings[cache_key] = route_binding
            loop.usage_stage = task_kind
            router.register_session(definition.id, session_id, runtime_id)
        else:
            loop, session_id, runtime_id = cached
            execution_session = cast(
                AgentExecutionSession,
                self._agent_execution.session(workflow_id, identity_key),
            )
            persisted_identity = self._load_persisted_identity_state(
                envelope=envelope,
                runtime_id=runtime_id,
            )
            if persisted_identity is not None:
                recovery_driver.restore_attempts(
                    persisted_identity.get("recovery_attempts", {})
                )
            task_correlation = self._task_correlation(
                envelope,
                workflow_id=workflow_id,
                identity_key=identity_key,
                session_id=session_id,
                identity_lease=identity_lease,
                execution_profile_sha256=resolved_profile.profile_sha256,
            )
            TaskAttemptStore(self.workspace, envelope.run_id).activate(task_correlation)
            gateway = scoped_gateway(
                self._artifact_root,
                workflow_id=workflow_id,
                envelope=envelope,
                agent_id=definition.id,
                session_id=session_id,
            )
            loop.config = resolved_config
            loop.llm_provider = routed_provider
            loop.artifact_gateway = gateway
            loop._system_prompt_override = system_prompt
            loop.tools = self._tools(
                definition,
                envelope,
                session_id,
                workflow_id,
                gateway=gateway,
                shared_artifacts=shared_artifacts,
                task_correlation=task_correlation,
                recovery_event_callback=(
                    recovery_event_callback
                    if recovery_policy is not None
                    else None
                ),
                recovery_policy=recovery_policy,
            )
            loop.usage_run_id = envelope.run_id
            loop.usage_task_id = envelope.task_id
            loop.usage_stage = task_kind
            loop.before_provider_attempt = (
                None
                if self._provider_attempt_guard is None
                else lambda: self._provider_attempt_guard(definition.id, envelope.task_id)
            )
            loop.provider_recovery_decider = (
                provider_recovery_decider
                if recovery_policy is not None
                else None
            )
            if self.provider_admission is not None:
                loop.provider_admission = self.provider_admission
                loop.provider_admission_provider = resolved_profile.resolved_provider_route
                loop.provider_admission_model = resolved_profile.resolved_model
                loop.provider_admission_identity_key = f"{workflow_id}:{identity_key}"
        loop.usage_execution_profile_id = resolved_profile.profile.profile_id
        loop.usage_execution_profile_version = resolved_profile.profile.version
        loop.usage_execution_profile_sha256 = resolved_profile.profile_sha256
        loop.usage_resolved_provider_route = (
            resolved_profile.resolved_provider_route
        )
        loop.usage_resolved_model = resolved_profile.resolved_model
        loop.usage_task_priority = resolved_profile.profile.priority
        loop.usage_expected_duration_ms = (
            resolved_profile.profile.expected_duration_ms
        )
        safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", envelope.task_id)
        provider_call_root = (
            self.workspace
            / f"Work/runs/{envelope.run_id}/context-manifests/provider-calls"
        )
        provider_call_index = len(
            list(
                provider_call_root.glob(
                    f"{safe_task_id}-r{envelope.revision}-{session_id}-c*.json"
                )
            )
        )

        async def record_provider_context(
            messages: list,
            tool_definitions: list[dict] | None,
            phase: str,
            attempt: int,
        ) -> None:
            nonlocal provider_call_index
            provider_call_index += 1
            manifest_path = self._write_provider_call_manifest(
                definition=definition,
                envelope=envelope,
                identity_key=identity_key,
                session_id=session_id,
                messages=messages,
                tool_definitions=tool_definitions,
                phase=phase,
                attempt=attempt,
                call_index=provider_call_index,
                resolved_execution_profile=resolved_profile,
            )
            loop.usage_context_manifest_ref = manifest_path.relative_to(
                self.workspace
            ).as_posix()
            loop.usage_provider_call_id = manifest_path.stem
            # AgentLoop versions carrying the H1 ledger fields read these
            # attributes when they append the physical-attempt row.  Setting
            # them here keeps this Runner compatible with older loops (which
            # simply ignore unknown attributes) and gives every actual call an
            # explicit, non-inferred reason/send classification.
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            loop.usage_provider_call_ref = loop.usage_context_manifest_ref
            loop.usage_provider_call_hash = manifest_payload.get("provider_call_hash")
            loop.usage_context_manifest_version = 3
            loop.usage_provider_request_sent = True
            loop.usage_attempt_kind = "provider_request"
            loop.usage_round_reason = self._provider_round_reason(
                phase,
                attempt,
            )
            metrics = manifest_payload.get("metrics")
            if not isinstance(metrics, dict):
                metrics = {}
            for metric_name in (
                "new_chars",
                "repeated_chars",
                "stable_chars",
                "dynamic_chars",
                "duplicate_chars",
                "repeated_stable_chars",
                "repeated_dynamic_chars",
                "duplicate_tool_result_chars",
                "duplicate_completed_result_chars",
                "duplicate_evidence_chars",
            ):
                setattr(loop, f"usage_{metric_name}", int(metrics.get(metric_name, 0) or 0))
            loop.usage_parent_provider_call_id = None
            loop.usage_logical_round_id = (
                f"{envelope.run_id}:{envelope.task_id}:{envelope.revision}:{phase}"
            )
            # ``_chat_with_retries`` marks the exact first Provider payload as
            # a typed rebase (count=1).  The observer runs immediately before
            # that payload is sent, so do not clear the marker here or the H3
            # ledger row would claim that no context rebuild occurred.
            loop.usage_rebuild_count = 0
            loop.usage_pre_send_guard_status = None

        async def finalize_provider_context(record: dict[str, Any]) -> None:
            self._finalize_provider_call_manifest(record)

        loop.provider_attempt_observer = record_provider_context
        loop.provider_attempt_record_observer = finalize_provider_context
        self._record_identity(
            workflow_id=workflow_id,
            envelope=envelope,
            identity_key=identity_key,
            session_id=session_id,
            runtime_id=runtime_id,
            status="running",
        )
        input_contract_payload = None
        if envelope.input_contract_ref:
            contract_path = (self.workspace / envelope.input_contract_ref).resolve()
            if contract_path.is_relative_to(self.workspace) and contract_path.is_file():
                input_contract_payload = json.dumps(
                    json.loads(contract_path.read_text(encoding="utf-8")),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
        task_message = PromptAssembler.task_message(
            envelope,
            shared_artifacts,
            input_contract_payload=input_contract_payload,
        )
        current_task_state = self._task_context_state(
            envelope,
            shared_artifacts=shared_artifacts,
        )
        previous_task_state = loop.active_task_identity or self._session_task_state.get(
            cache_key
        )
        if previous_task_state is not None:
            task_message = PromptAssembler.task_delta_message(
                envelope,
                shared_artifacts,
                previous=previous_task_state,
                input_contract_payload=input_contract_payload,
            )
        loop.begin_typed_task(current_task_state)
        self._session_task_state[cache_key] = current_task_state
        self._write_context_manifest(
            definition=definition,
            envelope=envelope,
            identity_key=identity_key,
            session_id=session_id,
            system_prompt=system_prompt,
            task_message=task_message,
            input_contract_payload=input_contract_payload,
            shared_artifacts=shared_artifacts,
            resolved_execution_profile=resolved_profile,
        )

        def load_typed_result(message: AgentResultMessage) -> AgentResult:
            expected_ref = (
                f"Work/runs/{envelope.run_id}/results/attempts/"
                f"{envelope.task_id}/{envelope.task_attempt_id}.json"
            )
            if message.result_path != expected_ref:
                raise RuntimeError("typed result path does not match the active task attempt")
            result_path = (self.workspace / message.result_path).resolve()
            if (
                not result_path.is_relative_to(self.workspace)
                or not result_path.is_file()
            ):
                raise RuntimeError("typed result artifact is missing")
            result_bytes = result_path.read_bytes()
            if hashlib.sha256(result_bytes).hexdigest() != message.result_sha256:
                raise RuntimeError("typed result hash does not match its terminal envelope")
            raw = json.loads(result_bytes)
            return AgentResult.model_validate(raw)

        async def persist_untyped_completion(response: AgentResponse) -> AgentResult:
            """Return a natural Agent completion to the workflow without guessing its type."""

            continuation_stopped = response.content.startswith(
                CONTINUATION_HARNESS_STOPPED
            )
            incomplete = AgentResult(
                task_id=envelope.task_id,
                run_id=envelope.run_id,
                agent_id=definition.id,
                session_id=session_id,
                status=AgentRunStatus.INCOMPLETE,
                raw_output=response.content,
                reason=(
                    "continuation harness stopped after bounded no-progress/profile headroom"
                    if continuation_stopped
                    else "agent ended without a typed submission"
                ),
            )
            terminal = TaskAttemptStore(
                self.workspace, envelope.run_id
            ).persist_result(
                task_correlation,
                incomplete.model_dump(mode="json"),
                status=incomplete.status.value,
            )
            relative = terminal.result_ref
            await self.bus.publish(
                AgentResultMessage(
                    workflow_id=workflow_id,
                    task_id=envelope.task_id,
                    run_id=envelope.run_id,
                    sender=definition.id,
                    recipient="workflow",
                    artifact_refs=[relative],
                    result_path=relative,
                    status=incomplete.status.value,
                    content=response.content,
                    task_attempt_id=envelope.task_attempt_id,
                    session_id=session_id,
                    identity_key=identity_key,
                    input_contract_ref=task_correlation.input_contract_ref,
                    input_contract_sha256=(
                        task_correlation.input_contract_sha256
                    ),
                    result_sha256=terminal.result_sha256,
                    lease_owner_id=identity_lease.owner_id,
                    lease_epoch=identity_lease.lease_epoch,
                )
            )
            return incomplete

        async def one_turn(
            content: str,
            *,
            internal: bool = False,
            provider_stream_idle_timeout_seconds: float | None = None,
            turn_kind: str = "task_initial",
        ) -> AgentResult | AgentResponse:
            outcome = await self._agent_execution.dispatch_turn(
                execution_session,
                AgentTurnRequest(
                    content=content,
                    message_id=envelope.task_id,
                    workflow_id=workflow_id,
                    run_id=envelope.run_id,
                    task_id=envelope.task_id,
                    task_attempt_id=envelope.task_attempt_id,
                    internal=internal,
                    provider_stream_idle_timeout_seconds=(
                        provider_stream_idle_timeout_seconds
                    ),
                    turn_kind=turn_kind,
                ),
                terminals=(
                    AgentTerminalSubscription(
                        kind="typed_result",
                        message_type=AgentResultMessage,
                        predicate=lambda item: (
                            item.workflow_id == workflow_id
                            and item.run_id == envelope.run_id
                            and item.task_id == envelope.task_id
                            and item.task_attempt_id == envelope.task_attempt_id
                            and item.sender == definition.id
                            and item.session_id == session_id
                            and item.identity_key == identity_key
                            and item.lease_owner_id == identity_lease.owner_id
                            and item.lease_epoch == identity_lease.lease_epoch
                        ),
                    ),
                ),
            )
            if outcome.kind == "typed_result":
                return load_typed_result(cast(AgentResultMessage, outcome.message))
            if outcome.kind == "error":
                error = cast(Any, outcome.message)
                marker = "REPORTING_RUN_BUDGET_EXHAUSTED:"
                if marker in error.message:
                    from .workflow import ReportingNeedsDecisionError

                    raise ReportingNeedsDecisionError(
                        error.message.split(marker, 1)[1].strip()
                    )
                raise RuntimeError(error.message)
            return cast(AgentResponse, outcome.message)

        try:

            async def finish_tool_slices(
                turn: AgentResult | AgentResponse,
                *,
                origin_turn_kind: Literal[
                    "task_initial",
                    "submission_correction",
                ] = "task_initial",
            ) -> AgentResult | AgentResponse:
                """Continue only while durable/conversational state is advancing."""

                safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", envelope.task_id)
                safe_attempt_id = re.sub(
                    r"[^A-Za-z0-9_.-]", "_", envelope.task_attempt_id
                )
                continuation_ref = (
                    f"Work/runs/{envelope.run_id}/continuations/{safe_task_id}/"
                    f"{safe_attempt_id}.json"
                )
                continuation_path = self.workspace / continuation_ref
                limits = self._continuation_limits(resolved_profile)

                def load_state() -> dict[str, Any]:
                    if continuation_path.is_file():
                        state = json.loads(
                            continuation_path.read_text(encoding="utf-8")
                        )
                        expected = {
                            "run_id": envelope.run_id,
                            "task_id": envelope.task_id,
                            "task_attempt_id": envelope.task_attempt_id,
                            "revision": envelope.revision,
                            "execution_profile_sha256": (
                                resolved_profile.profile_sha256
                            ),
                        }
                        if any(state.get(key) != value for key, value in expected.items()):
                            raise RuntimeError(
                                "continuation harness state does not match the active typed task"
                            )
                        if state.get("limits") != limits:
                            raise RuntimeError(
                                "continuation harness limits changed inside one task attempt"
                            )
                        return state
                    return {
                        "kind": "reporting_continuation_harness_state",
                        "version": 1,
                        "run_id": envelope.run_id,
                        "task_id": envelope.task_id,
                        "task_attempt_id": envelope.task_attempt_id,
                        "revision": envelope.revision,
                        "execution_profile_id": resolved_profile.profile.profile_id,
                        "execution_profile_sha256": resolved_profile.profile_sha256,
                        "limits": limits,
                        "continuation_counts": {
                            "max_tokens_continuation": 0,
                            "tool_slice_continuation": 0,
                            "submission_correction": 0,
                        },
                        "no_progress_observations": 0,
                        "observed_conversation_event_sha256": [],
                        "last_durable_sha256": None,
                        "status": "active",
                        "events": [],
                    }

                def save_state(state: dict[str, Any]) -> None:
                    self.store.write_json(continuation_ref, state)

                while isinstance(turn, AgentResponse) and turn.content in {
                    AGENT_TURN_CONTINUATION_REQUIRED,
                    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
                }:
                    max_tokens_continuation = turn.content == AGENT_MAX_TOKENS_CONTINUATION_REQUIRED
                    recovery_event_kind = (
                        RecoveryEventKind.MAX_TOKENS
                        if max_tokens_continuation
                        else RecoveryEventKind.TOOL_SLICE_BOUNDARY
                    )
                    continuation_kind = (
                        "max_tokens_continuation"
                        if max_tokens_continuation
                        else "submission_correction"
                        if origin_turn_kind == "submission_correction"
                        else "tool_slice_continuation"
                    )
                    state = load_state()
                    snapshot = await self._continuation_progress_snapshot(
                        loop,
                        envelope,
                    )
                    observed = set(
                        state["observed_conversation_event_sha256"]
                    )
                    current_events = set(snapshot["conversation_event_sha256"])
                    first_observation = state["last_durable_sha256"] is None
                    durable_changed = (
                        not first_observation
                        and state["last_durable_sha256"]
                        != snapshot["durable_sha256"]
                    )
                    new_events = current_events - observed
                    progressed = first_observation or durable_changed or bool(new_events)
                    if progressed:
                        state["no_progress_observations"] = 0
                    else:
                        state["no_progress_observations"] += 1
                    state["last_durable_sha256"] = snapshot["durable_sha256"]
                    state["observed_conversation_event_sha256"] = sorted(
                        observed | current_events
                    )
                    count = int(state["continuation_counts"][continuation_kind])
                    profile_limit_reached = (
                        continuation_kind in limits
                        and count >= limits[continuation_kind]
                    )
                    stalled = (
                        not first_observation
                        and state["no_progress_observations"]
                        >= limits["max_no_progress_observations"]
                    )
                    event = {
                        "sequence": len(state["events"]) + 1,
                        "origin_turn_kind": origin_turn_kind,
                        "turn_kind": continuation_kind,
                        "progressed": progressed,
                        "new_conversation_event_count": len(new_events),
                        "durable_changed": durable_changed,
                        "durable_file_count": snapshot["durable_file_count"],
                        "result_parts": snapshot["result_parts"],
                        "continuations_already_issued": count,
                    }
                    if profile_limit_reached:
                        stop_reason = "execution_profile_continuation_limit"
                        event.update(
                            {"decision": "stop", "stop_reason": stop_reason}
                        )
                        state["events"].append(event)
                        state["status"] = "stopped"
                        state["stop_reason"] = stop_reason
                        state["stop_turn_kind"] = continuation_kind
                        save_state(state)
                        return turn.model_copy(
                            update={
                                "content": (
                                    f"{CONTINUATION_HARNESS_STOPPED}{stop_reason};"
                                    f"turn_kind={continuation_kind};state_ref={continuation_ref}"
                                ),
                                "internal": True,
                            }
                        )

                    observed_boundary = True

                    async def interpret_continuation_terminal(
                        outcome: AgentTurnOutcome,
                        _request: AgentTurnRequest,
                    ) -> AgentRecoveryObservation:
                        nonlocal observed_boundary
                        if observed_boundary:
                            observed_boundary = False
                            return AgentRecoveryProgress(
                                kind=(
                                    "no_progress"
                                    if stalled
                                    else "progressed"
                                ),
                                continuation=AgentRecoveryRequired(
                                    event_kind=recovery_event_kind,
                                    fallback_action=RecoveryActionKind.CONTINUE,
                                    detail={"task_id": envelope.task_id},
                                ),
                                detail={
                                    "task_id": envelope.task_id,
                                    "turn_kind": continuation_kind,
                                },
                            )
                        if outcome.kind == "typed_result":
                            return AgentRecoveryCompleted(
                                result=load_typed_result(
                                    cast(AgentResultMessage, outcome.message)
                                )
                            )
                        if outcome.kind == "error":
                            error = cast(Any, outcome.message)
                            marker = "REPORTING_RUN_BUDGET_EXHAUSTED:"
                            if marker in error.message:
                                from .workflow import ReportingNeedsDecisionError

                                raise ReportingNeedsDecisionError(
                                    error.message.split(marker, 1)[1].strip()
                                )
                            raise RuntimeError(error.message)
                        return AgentRecoveryCompleted(
                            result=cast(AgentResponse, outcome.message)
                        )

                    async def build_continuation_turn(
                        directive: AgentRecoveryDirective,
                        _observation: AgentRecoveryRequired,
                    ) -> AgentTurnRequest:
                        state["continuation_counts"][continuation_kind] = count + 1
                        event["decision"] = "continue"
                        state["events"].append(event)
                        state["status"] = "continuing"
                        save_state(state)
                        if directive.prompt:
                            continuation_instruction = directive.prompt
                        elif max_tokens_continuation:
                            continuation_instruction = (
                                "上一模型轮次达到单次 max_tokens 上限，未产生完整提交；"
                                "这是未完成续写，不是提交格式纠正，也不表示分析已经完成。"
                                "使用原任务输入和已获得的上下文继续，优先完成必要判断并调用 "
                                "submit_result；不要从头重复检索。"
                            )
                        else:
                            continuation_instruction = (
                                "先调用 list_result_parts 查看已保存分段，只继续未完成部分；不要重新检索或重写"
                                "已保存内容。"
                                if loop.tools.get("list_result_parts") is not None
                                else "继续使用当前输入引用和已经获得的上下文；不要重新检索或重读已有内容。"
                            )
                        boundary_explanation = (
                            ""
                            if directive.prompt
                            else (
                                "上一模型输出达到单次生成上限，但任务没有失败。你仍是原 Agent。"
                                if max_tokens_continuation
                                else "上一工具执行片段已达到单次轮次边界，但任务没有失败。你仍是原 Agent。"
                            )
                        )
                        semantic_reason = PromptAssembler.semantic_turn(
                            (
                                f"{boundary_explanation}{continuation_instruction}"
                                "完成后必须调用 submit_result 提交本任务规定的结构化结果。"
                            ),
                            turn_kind=continuation_kind,
                            next_action="submit_result",
                        )
                        continuation_message = (
                            "<same_identity_continuation>\n"
                            f"{semantic_reason}\n"
                            "</same_identity_continuation>"
                        )
                        self._context_reason_turn(
                            context_rebuilder,
                            content=continuation_message,
                            turn_kind=continuation_kind,
                            prior_output=next(
                                (
                                    str(getattr(item, "content", "") or "")
                                    for item in reversed(loop._conversation_history)
                                    if getattr(item, "role", None) == "assistant"
                                    and str(getattr(item, "content", "") or "")
                                ),
                                None,
                            ),
                        )
                        return AgentTurnRequest(
                            content=continuation_message,
                            message_id=envelope.task_id,
                            workflow_id=workflow_id,
                            run_id=envelope.run_id,
                            task_id=envelope.task_id,
                            task_attempt_id=envelope.task_attempt_id,
                            internal=True,
                            turn_kind=continuation_kind,
                            provider_stream_idle_timeout_seconds=(
                                REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS
                            ),
                        )

                    async def stop_continuation_recovery(
                        outcome: AgentTurnOutcome,
                        directive: AgentRecoveryDirective,
                    ) -> AgentResponse:
                        response = cast(AgentResponse, outcome.message)
                        stop_reason = (
                            "repeated_no_progress"
                            if directive.event_kind
                            is RecoveryEventKind.NO_PROGRESS
                            else "recovery_policy_stop"
                        )
                        if directive.event_kind is RecoveryEventKind.NO_PROGRESS:
                            event.update(
                                {
                                    "decision": "stop",
                                    "stop_reason": stop_reason,
                                }
                            )
                            state["events"].append(event)
                            state["status"] = "stopped"
                            state["stop_reason"] = stop_reason
                            state["stop_turn_kind"] = continuation_kind
                            save_state(state)
                        return response.model_copy(
                            update={
                                "content": (
                                    f"{CONTINUATION_HARNESS_STOPPED}"
                                    f"{stop_reason};turn_kind={continuation_kind}"
                                    + (
                                        f";state_ref={continuation_ref}"
                                        if directive.event_kind
                                        is RecoveryEventKind.NO_PROGRESS
                                        else ""
                                    )
                                ),
                                "internal": True,
                            }
                        )

                    async def reuse_continuation_result(
                        outcome: AgentTurnOutcome,
                        _directive: AgentRecoveryDirective,
                    ) -> AgentResult:
                        return load_typed_result(
                            cast(AgentResultMessage, outcome.message)
                        )

                    turn = await self._agent_execution.execute_with_recovery(
                        execution_session,
                        AgentTurnRequest(
                            content=turn.content,
                            message_id=envelope.task_id,
                            workflow_id=workflow_id,
                            run_id=envelope.run_id,
                            task_id=envelope.task_id,
                            task_attempt_id=envelope.task_attempt_id,
                            internal=True,
                            turn_kind=origin_turn_kind,
                        ),
                        recovery=recovery_driver,
                        interpret=interpret_continuation_terminal,
                        build_turn=build_continuation_turn,
                        stop=stop_continuation_recovery,
                        reuse_result=reuse_continuation_result,
                        terminals=(
                            AgentTerminalSubscription(
                                kind="typed_result",
                                message_type=AgentResultMessage,
                                predicate=lambda item: (
                                    item.workflow_id == workflow_id
                                    and item.run_id == envelope.run_id
                                    and item.task_id == envelope.task_id
                                    and item.task_attempt_id
                                    == envelope.task_attempt_id
                                    and item.sender == definition.id
                                    and item.session_id == session_id
                                    and item.identity_key == identity_key
                                    and item.lease_owner_id
                                    == identity_lease.owner_id
                                    and item.lease_epoch
                                    == identity_lease.lease_epoch
                                ),
                            ),
                        ),
                        observed_outcome=AgentTurnOutcome(
                            kind="response",
                            message=turn,
                            session_id=session_id,
                            runtime_id=runtime_id,
                        ),
                    )
                    if (
                        isinstance(turn, AgentResponse)
                        and turn.content.startswith(CONTINUATION_HARNESS_STOPPED)
                    ):
                        return turn
                if continuation_path.is_file():
                    state = load_state()
                    state["status"] = (
                        "typed_result"
                        if isinstance(turn, AgentResult)
                        else "ended_without_continuation_sentinel"
                    )
                    save_state(state)
                return turn

            # Reporting turns can spend several minutes assembling a typed tool
            # payload after their last visible text chunk.  Apply the long idle
            # window to the initial task as well as correction/continuation
            # turns; otherwise a chief-editor aggregation is still killed by
            # the provider default before it can submit the completed report.
            stream_idle_timeout = self._provider_stream_idle_timeout(definition)
            turn = await finish_tool_slices(
                await one_turn(
                    task_message,
                    provider_stream_idle_timeout_seconds=(stream_idle_timeout),
                )
            )
            if (
                isinstance(turn, AgentResponse)
                and turn.content.startswith(CONTINUATION_HARNESS_STOPPED)
            ):
                result = await persist_untyped_completion(turn)
            elif isinstance(turn, AgentResponse) and envelope.allowed_outputs:
                expected = ", ".join(envelope.allowed_outputs)
                if envelope.task_id == "template-skill-distillation":
                    correction = (
                        "<submission_correction>\n"
                        "你刚才错误地用普通文字结束。现在不得解释或重读模板。立即调用 "
                        "write_result_part，逐项写入 input contract 的十四个 required_part_ids；"
                        "然后调用 submit_result 提交 template_skill_submission，十四份 Skill"
                        "只使用 write_result_part 返回的 artifact_ref；边界 manifest 由"
                        "运行时生成，不得提交。只有 submit_result 工具成功才可结束。\n"
                        "</submission_correction>"
                    )
                elif definition.id == "chief-editor":
                    part_instruction = (
                        "只补齐 list_result_parts 列出的目标修订章节"
                        if envelope.input_contract_kind == "chief_revision_input"
                        else "只补齐 list_result_parts 列出的本章固定小节"
                        if envelope.input_contract_kind == "chief_chapter_input"
                        else "补齐 list_result_parts 列出的十二个固定章节"
                    )
                    submission_instruction = (
                        "提交小型 chief_revision_submission；不得重复父版本全文、"
                        "Cross dispositions、表格、图片或未决问题"
                        if envelope.input_contract_kind == "chief_revision_input"
                        else (
                            "提交小型 chief_chapter_submission；只声明 chapter_id、"
                            "Chapter 1 的 title、Chapter 3 的 tables 和未决问题"
                        )
                        if envelope.input_contract_kind == "chief_chapter_input"
                        else (
                            "提交 edited_report_submission；只提交当前合同声明的实际章节字段，"
                            "不要恢复已删除的跨领域风险模块或其旧版综合元数据"
                        )
                    )
                    module_instruction = (
                        "不得提交 module_narratives 或任何第二章内容。"
                        if envelope.input_contract_kind
                        in {"chief_revision_input", "chief_chapter_input"}
                        else (
                            "module_narratives 必须只提交五个精确标记 "
                            "[[APPROVED_MODULE:2.1]] 至 [[APPROVED_MODULE:2.5]]。"
                        )
                    )
                    correction = (
                        "<submission_correction>\n"
                        "你刚才未完成总编提交。批准的五模块正文绝对不得压缩、"
                        f"摘要、改写或重新输出。先调用 list_result_parts；{part_instruction}，"
                        "分别用同名 part_id 调用 write_result_part。"
                        f"{module_instruction}"
                        f"随后立即调用 submit_result {submission_instruction}。"
                        "不得重新读取或搜索输入。\n"
                        "</submission_correction>"
                    )
                elif definition.id.startswith("module-") and definition.id.endswith("-specialist"):
                    correction = (
                        "<submission_correction>\n"
                        "你刚才尚未提交模块 commit。先调用 list_result_parts；每个固定 part "
                        "必须 ready=true，未绑定部分用 write_result_part 重新提交完整正文和"
                        "当前 run 的 evidence_ids。正文只写读者可见内容，"
                        "submit_result 只发送 schema 声明的字段。随后按唯一"
                        f"合同调用 submit_result 提交小型 {expected} commit。\n"
                        "</submission_correction>"
                    )
                elif definition.id == "cross-module-reviewer":
                    correction = (
                        "<submission_correction>\n"
                        "你已完成审查分析但尚未提交类型化结果。立即按 task 中展示的"
                        f"唯一合同调用 submit_result 提交 {expected}。initial 只创建 findings "
                        "与 synthesis_inputs；recheck 只对 required_findings 返回 verdicts。"
                        "不得重新读取、改写旧 finding 或猜测其他结构。\n"
                        "</submission_correction>"
                    )
                elif definition.id == "chief-editor-auditor":
                    correction = (
                        "<submission_correction>\n"
                        "你已完成成稿审计但尚未提交类型化结果。立即使用现有结论调用 "
                        f"submit_result 提交 {expected}。initial 提交 findings；recheck 逐项"
                        "提交 required_findings 的 verdicts。不得重新读取、改写旧 finding "
                        "或猜测其他结构。\n"
                        "</submission_correction>"
                    )
                else:
                    correction = (
                        "<submission_correction>\n"
                        "你刚才结束了文字回答，但本任务只有调用 submit_result 才算完成。\n"
                        f"使用已经获得的上下文提交 {expected}。若长正文已经由 write_result_part "
                        "保存，先调用 list_result_parts 并按当前合同引用已保存内容；"
                        "不得压缩或省略已完成内容，不得重新读取文件或重新检索。\n"
                        "</submission_correction>"
                    )
                initial_recovery_request = AgentTurnRequest(
                    content=task_message,
                    message_id=envelope.task_id,
                    workflow_id=workflow_id,
                    run_id=envelope.run_id,
                    task_id=envelope.task_id,
                    task_attempt_id=envelope.task_attempt_id,
                    provider_stream_idle_timeout_seconds=stream_idle_timeout,
                )

                async def interpret_submission_terminal(
                    outcome: AgentTurnOutcome,
                    request: AgentTurnRequest,
                ) -> AgentRecoveryObservation:
                    if outcome.kind == "typed_result":
                        return AgentRecoveryCompleted(
                            result=load_typed_result(
                                cast(AgentResultMessage, outcome.message)
                            ),
                        )
                    if outcome.kind == "error":
                        error = cast(Any, outcome.message)
                        marker = "REPORTING_RUN_BUDGET_EXHAUSTED:"
                        if marker in error.message:
                            from .workflow import ReportingNeedsDecisionError

                            raise ReportingNeedsDecisionError(
                                error.message.split(marker, 1)[1].strip()
                            )
                        raise RuntimeError(error.message)
                    response = cast(AgentResponse, outcome.message)
                    if response.content in {
                        AGENT_TURN_CONTINUATION_REQUIRED,
                        AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
                    }:
                        return AgentRecoveryCompleted(result=response)
                    if request.turn_kind == "submission_correction":
                        return AgentRecoveryStopped()
                    return AgentRecoveryRequired(
                        event_kind=(
                            RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION
                        ),
                        fallback_action=RecoveryActionKind.CORRECT,
                        detail={"task_id": envelope.task_id},
                    )

                async def build_submission_turn(
                    directive: AgentRecoveryDirective,
                    _observation: AgentRecoveryRequired,
                ) -> AgentTurnRequest:
                    recovery_message = (
                        (
                            "<submission_correction>\n"
                            f"{directive.prompt}\n"
                            "</submission_correction>"
                        )
                        if directive.prompt
                        else correction
                    )
                    self._context_reason_turn(
                        context_rebuilder,
                        content=recovery_message,
                        turn_kind="submission_correction",
                        prior_output=turn.content,
                    )
                    return AgentTurnRequest(
                        content=recovery_message,
                        message_id=envelope.task_id,
                        workflow_id=workflow_id,
                        run_id=envelope.run_id,
                        task_id=envelope.task_id,
                        task_attempt_id=envelope.task_attempt_id,
                        internal=True,
                        turn_kind="submission_correction",
                        provider_stream_idle_timeout_seconds=(
                            REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS
                        ),
                    )

                async def stop_submission_recovery(
                    outcome: AgentTurnOutcome,
                    _directive: AgentRecoveryDirective,
                ) -> AgentResult:
                    response = cast(AgentResponse, outcome.message)
                    return await persist_untyped_completion(response)

                async def reuse_submission_result(
                    outcome: AgentTurnOutcome,
                    _directive: AgentRecoveryDirective,
                ) -> AgentResult:
                    return load_typed_result(
                        cast(AgentResultMessage, outcome.message)
                    )

                corrected = await self._agent_execution.execute_with_recovery(
                    execution_session,
                    initial_recovery_request,
                    recovery=recovery_driver,
                    interpret=interpret_submission_terminal,
                    build_turn=build_submission_turn,
                    stop=stop_submission_recovery,
                    reuse_result=reuse_submission_result,
                    terminals=(
                        AgentTerminalSubscription(
                            kind="typed_result",
                            message_type=AgentResultMessage,
                            predicate=lambda item: (
                                item.workflow_id == workflow_id
                                and item.run_id == envelope.run_id
                                and item.task_id == envelope.task_id
                                and item.task_attempt_id
                                == envelope.task_attempt_id
                                and item.sender == definition.id
                                and item.session_id == session_id
                                and item.identity_key == identity_key
                                and item.lease_owner_id
                                == identity_lease.owner_id
                                and item.lease_epoch
                                == identity_lease.lease_epoch
                            ),
                        ),
                    ),
                    observed_outcome=AgentTurnOutcome(
                        kind="response",
                        message=turn,
                        session_id=session_id,
                        runtime_id=runtime_id,
                    ),
                )
                if isinstance(corrected, AgentResponse):
                    corrected = await finish_tool_slices(
                        corrected,
                        origin_turn_kind="submission_correction",
                    )
                result = (
                    corrected
                    if isinstance(corrected, AgentResult)
                    else await persist_untyped_completion(corrected)
                )
            elif isinstance(turn, AgentResponse):
                result = await persist_untyped_completion(turn)
            else:
                result = turn
        except asyncio.CancelledError:
            self._context_terminal(
                context_rebuilder,
                status="cancelled",
            )
            self._save_conversation_trace(
                loop, envelope, runtime_id, session_id,
                shared_artifacts=shared_artifacts,
                status="cancelled",
                recovery_driver=recovery_driver,
            )
            raise
        except Exception:
            self._context_terminal(
                context_rebuilder,
                status="failed",
            )
            self._save_conversation_trace(
                loop,
                envelope,
                runtime_id,
                session_id,
                shared_artifacts=shared_artifacts,
                status="failed",
                recovery_driver=recovery_driver,
            )
            raise
        await self._agent_execution.wait_until_turn_complete(execution_session)
        self._context_terminal(
            context_rebuilder,
            status=result.status.value,
            result=result,
        )
        self._save_conversation_trace(
            loop, envelope, runtime_id, session_id,
            shared_artifacts=shared_artifacts,
            status=result.status.value,
            recovery_driver=recovery_driver,
        )
        self._save_session_summary(
            loop,
            envelope,
            shared_artifacts,
            result,
            context_rebuilder,
        )
        self._record_identity(
            workflow_id=workflow_id,
            envelope=envelope,
            identity_key=identity_key,
            session_id=session_id,
            runtime_id=runtime_id,
            status="waiting",
        )
        return result

    def _save_conversation_trace(
        self,
        loop: AgentLoop,
        envelope: TaskEnvelope,
        runtime_id: str,
        session_id: str,
        *,
        shared_artifacts: list[str] | None = None,
        status: str,
        recovery_driver: AgentRecoveryDriver | None = None,
    ) -> Path:
        """Persist only durable identity and canonical business references."""

        def json_value(value):
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            if is_dataclass(value):
                return json_value(asdict(value))
            if isinstance(value, dict):
                return {str(key): json_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_value(item) for item in value]
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            return str(value)

        safe_runtime_id = re.sub(r"[^A-Za-z0-9_.-]", "_", runtime_id)
        raw_identity = getattr(loop, "active_task_identity", None) or self._task_context_state(
            envelope,
            shared_artifacts=shared_artifacts,
        )
        identity_fields = (
            "run_id", "task_id", "revision", "agent_id", "objective",
            "input_refs", "constraints", "allowed_outputs", "allowed_tools",
            "input_contract_kind", "input_contract_ref", "prior_result_ref",
            "context_summary_refs", "artifact_delivery_modes", "shared_artifacts",
        )
        identity_state = {
            field: json_value(raw_identity.get(field))
            for field in identity_fields
            if raw_identity.get(field) not in (None, "", [], {})
        }
        result_ref = f"Work/runs/{envelope.run_id}/results/{envelope.task_id}.json"
        completed_result_ref = (
            result_ref if status == AgentRunStatus.COMPLETED.value
            and (self.workspace / result_ref).is_file() else None
        )
        knowledge_refs = list(dict.fromkeys([
            *envelope.input_refs,
            *envelope.context_summary_refs,
            *(shared_artifacts or []),
        ]))
        attention_scope_ref = (
            envelope.input_contract_ref
            if envelope.input_contract_ref
            and ("auditor" in envelope.agent_id or "cross" in envelope.agent_id)
            else None
        )
        if attention_scope_ref:
            identity_state["attention_scope_ref"] = attention_scope_ref
        return self.store.write_json(
            f"Work/runs/{envelope.run_id}/agent-conversations/{safe_runtime_id}.json",
            {
                "manifest_version": 4,
                "run_id": envelope.run_id,
                "task_id": envelope.task_id,
                "agent_id": envelope.agent_id,
                "runtime_id": runtime_id,
                "session_id": session_id,
                "status": status,
                "encoding": "identity+refs",
                "transcript_semantics": "durable_identity_reference_state_v1",
                "recovery_attempts": (
                    recovery_driver.snapshot_attempts()
                    if recovery_driver is not None
                    else {}
                ),
                "identity_state": identity_state,
                "business_refs": {
                    "input_contract_ref": envelope.input_contract_ref,
                    "input_knowledge_refs": knowledge_refs,
                    "prior_completed_result_ref": envelope.prior_result_ref,
                    "completed_result_ref": completed_result_ref,
                    "attention_scope_ref": attention_scope_ref,
                },
            },
        )

    def _save_session_summary(
        self,
        loop: AgentLoop,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        result: AgentResult,
        context_rebuilder: ReportingContextRebuilder | None = None,
    ) -> Path:
        builder = SessionSummaryBuilder(self.workspace)
        context_manifest_ref: str | None = None
        context_manifest_sha256: str | None = None
        capsule_sha256: str | None = None
        remaining_work: list[str] = []
        manifest = getattr(context_rebuilder, "manifest", None)
        if manifest is not None:
            context_manifest_sha256 = manifest.manifest_sha256
            capsule = manifest.task_state
            capsule_sha256 = capsule.capsule_sha256 if capsule is not None else None
            remaining_work = list(capsule.remaining_work if capsule is not None else ())
            store = getattr(context_rebuilder, "store", None)
            if store is not None:
                for entry in reversed(store._read_sequence()):  # type: ignore[attr-defined]
                    if entry.get("manifest_sha256") != manifest.manifest_sha256:
                        continue
                    candidate = store.root / str(entry.get("ref") or "")
                    if candidate.is_file():
                        context_manifest_ref = candidate.relative_to(self.workspace).as_posix()
                        break
        skeleton = builder.build_skeleton(
            envelope,
            shared_artifacts,
            session_id=result.session_id,
            message_log=list(loop._conversation_history),
            context_manifest_ref=context_manifest_ref,
            context_manifest_sha256=context_manifest_sha256,
            capsule_sha256=capsule_sha256,
            remaining_work=remaining_work,
        )
        payload = result.payload
        rationale = str(getattr(payload, "rationale", "") or result.reason or "")
        if not rationale and payload is not None:
            unresolved = getattr(payload, "unresolved_questions", [])
            rationale = "；".join(str(item) for item in unresolved)
        result_ref = f"Work/runs/{envelope.run_id}/results/{envelope.task_id}.json"
        summary = builder.finalize(
            skeleton,
            result,
            output_refs=[result_ref],
            agent_rationale=rationale,
        )
        return SessionSummaryStore(self.workspace).save(summary)

    async def close_workflow(self, workflow_id: str) -> None:
        """Stop all isolated sessions retained for peer questions and local revision."""
        keys = [key for key in self._sessions if key[0] == workflow_id]
        for key in keys:
            self._session_route_bindings.pop(key, None)
            self._session_task_state.pop(key, None)
        await self._agent_execution.close_workflow(workflow_id)
        router = self._routers.pop(workflow_id, None)
        if router is not None:
            router.close()
