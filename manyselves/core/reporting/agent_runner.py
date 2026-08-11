"""Run one reporting identity in an isolated Manyselves AgentLoop."""

from __future__ import annotations

import ast
import asyncio
import gzip
import hashlib
import json
import operator
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...config.schema import AgentDefaults
from ...interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    Error,
    UserMessage,
)
from ..artifacts import ArtifactGateway, ArtifactGrant, parse_artifact
from ..artifacts.content_store import ContentAddressedStore
from ..loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AGENT_TURN_CONTINUATION_REQUIRED,
    AgentLoop,
)
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..tools.artifact_tools import OpenArtifactTool, OpenToolResultTool, SearchTextTool
from ..tools.document_tool import InspectDocumentTool
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
from ..tools.skill_evolution_tools import ProductSkillEvolutionTool
from .agentic_models import AgentResult, AgentRunStatus, TaskEnvelope
from .capabilities import compile_agent_access, scoped_gateway
from .config import AgentDefinition
from .execution_runtime import ProviderRouter, ResolvedTaskExecutionProfile
from .input_snapshot import RunInputSnapshotStore
from .input_contracts import (
    INPUT_CONTRACT_TYPES,
    AggregateEditorInput,
    ChiefEditorInput,
    ChiefRevisionInput,
    CrossReviewInput,
    FinalReviewInput,
    ModuleAuthoringInput,
    ModuleReviewInput,
    ModuleRevisionInput,
    SubmoduleAuthoringInput,
    TemplateDistillationInput,
)
from .message_router import WorkflowMessageRouter, artifact_path_refs
from .models import CHIEF_RESULT_PART_IDS, CHIEF_SECTION_RESULT_PART_IDS
from .module_collaboration import InterfaceRequest
from .module_skills import ModuleSkillLibrary
from .parallel_runtime import (
    IdentityLease,
    IdentityLeaseManager,
    TaskAttemptStore,
    TaskCorrelation,
    exclusive_file_lock,
)
from .prompts import PromptAssembler
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


class ProviderAttemptRecoveryRequired(RuntimeError):
    """A same-task Provider request may have been accepted without a result."""

    partial_output = True
    ambiguous = True

    def __init__(self, manifest_refs: list[str]) -> None:
        self.manifest_refs = tuple(manifest_refs)
        super().__init__(
            "Provider attempt status is ambiguous; reconcile the existing same-task "
            "journal before dispatching another physical request: "
            + ", ".join(manifest_refs)
        )


MODULE_COLLABORATION_SUBMISSION_KINDS = frozenset(
    {
        "module_discovery_submission",
        "module_interface_response_submission",
        "submodule_discovery_batch_submission",
        "submodule_discovery_submission",
        "submodule_interface_response_submission",
    }
)
SUBMODULE_SCOPED_SUBMISSION_KINDS = frozenset(
    {
        "submodule_discovery_submission",
        "submodule_interface_response_submission",
        "submodule_draft_submission",
    }
)


def load_conversation_trace(
    workspace: Path,
    manifest_ref: str | Path,
) -> dict:
    """Read legacy inline or v2 compressed conversation traces uniformly."""

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
    if isinstance(manifest.get("messages"), list):
        # v1 compatibility: the transcript was the manifest itself.
        return manifest
    if (
        manifest.get("manifest_version") != 2
        or manifest.get("encoding") != "gzip+json"
    ):
        raise ValueError("unsupported conversation trace manifest")
    transcript_ref = str(manifest.get("transcript_ref") or "")
    transcript_path = (workspace / transcript_ref).resolve()
    content_root = (workspace / "Work/content/sha256").resolve()
    if (
        not transcript_path.is_relative_to(content_root)
        or not transcript_path.is_file()
    ):
        raise ValueError("conversation transcript CAS blob is unreadable")
    compressed = transcript_path.read_bytes()
    compressed_sha256 = hashlib.sha256(compressed).hexdigest()
    if compressed_sha256 != manifest.get("compressed_sha256"):
        raise ValueError("conversation transcript compressed hash mismatch")
    try:
        serialized = gzip.decompress(compressed)
    except OSError as exc:
        raise ValueError("conversation transcript gzip payload is invalid") from exc
    if hashlib.sha256(serialized).hexdigest() != manifest.get(
        "transcript_sha256"
    ):
        raise ValueError("conversation transcript content hash mismatch")
    payload = json.loads(serialized)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("messages"), list
    ):
        raise ValueError("conversation transcript payload is invalid")
    return payload


class InspectImageTool(Tool):
    name = "inspect_image"
    description = "Inspect dimensions and format of one project-local image."

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    async def __call__(self, path: str) -> dict:
        """Inspect an image.

        Args:
            path: Project-relative image path.
        """
        target = (self.workspace / path).resolve()
        if not target.is_relative_to(self.workspace) or not target.is_file():
            raise ValueError("image must be a file inside the project")
        parsed = parse_artifact(target)
        return {
            "path": path,
            "kind": parsed.kind,
            "metadata": parsed.blocks[0].text if parsed.blocks else "",
            "visual_verified": False,
            "error": parsed.error,
        }


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
        provider_router: ProviderRouter | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.llm_provider = llm_provider
        self.provider_router = provider_router or ProviderRouter(llm_provider)
        self.defaults = defaults
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
        self._sessions: dict[tuple[str, str], tuple[AgentLoop, str, str]] = {}
        self._session_route_bindings: dict[tuple[str, str], tuple[str, str]] = {}
        self._artifact_root = ArtifactGateway(
            self.workspace, ArtifactGrant("root", "root", "workflow", "root")
        )
        self._routers: dict[str, WorkflowMessageRouter] = {}
        self._provider_attempt_guard: Callable[[str, str], Awaitable[None]] | None = None

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
                    max(self.defaults.max_tool_calls_per_round, 5)
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
        if "submodule_draft_submission" in outputs:
            return "submodule_authoring"
        if (
            "submodule_discovery_submission" in outputs
            or "submodule_discovery_batch_submission" in outputs
        ):
            return "submodule_collaboration_discovery"
        if "submodule_interface_response_submission" in outputs:
            return "submodule_collaboration_response"
        if "module_discovery_submission" in outputs:
            return "module_collaboration_discovery"
        if "module_interface_response_submission" in outputs:
            return "module_collaboration_response"
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
            match = re.search(r"2\.[1-5]", envelope.task_id)
            if match is None:
                raise ValueError("evidence-auditor task must identify one fixed module")
            module_id = match.group(0)
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
    def _continuation_limits(
        resolved_profile: ResolvedTaskExecutionProfile,
    ) -> dict[str, int]:
        """Derive finite extra-slice headroom without reducing task limits.

        One continuation reuses the exact execution profile.  A profile which
        already grants a large output or tool window therefore needs fewer
        complete extra windows than a deliberately small test/deployment
        profile.  These limits never mutate ``max_tokens`` or omit task input.
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
        tool_slices = max(
            1,
            min(
                3,
                (24 + profile.max_tool_rounds - 1) // profile.max_tool_rounds,
            ),
        )
        return {
            "max_tokens_continuation": max_tokens_slices,
            "tool_slice_continuation": tool_slices,
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
        manifest = {
            "context_manifest_version": 1,
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
        manifest = {
            "provider_context_manifest_version": 3,
            "provider_call_id": provider_call_id,
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
            "messages": message_components,
            "tool_definitions": component("tool_definitions", serialized_tools),
            "request_sha256": self._sha256_text(serialized_request),
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
        return self.store.write_json(relative, manifest)

    def _ambiguous_provider_call_refs(
        self,
        envelope: TaskEnvelope,
        *,
        session_id: str,
        task_attempt_id: str | None = None,
    ) -> list[str]:
        """Find same-task requests that cannot safely be replayed."""

        root = (
            self.workspace
            / f"Work/runs/{envelope.run_id}/context-manifests/provider-calls"
        )
        if not root.is_dir():
            return []
        ambiguous: list[str] = []
        for path in sorted(root.glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            expected = {
                "run_id": envelope.run_id,
                "task_id": envelope.task_id,
                "revision": envelope.revision,
                "session_id": session_id,
            }
            if task_attempt_id is not None:
                expected["task_attempt_id"] = task_attempt_id
            if any(
                manifest.get(key) != value
                for key, value in expected.items()
            ):
                continue
            pending = manifest.get("provider_payload_status") == "pending"
            accepted_unknown = (
                manifest.get("usage_status") == "error"
                and manifest.get("attempt_disposition")
                == "accepted_or_unknown"
            )
            if pending or accepted_unknown:
                ambiguous.append(path.relative_to(self.workspace).as_posix())
        return ambiguous

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

    @staticmethod
    def _reference_artifacts(envelope: TaskEnvelope) -> list[str]:
        """Expose only artifacts explicitly delivered by reference to model tools."""

        declared = [
            *envelope.input_refs,
            *envelope.context_summary_refs,
            *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
        ]
        return [
            ref
            for ref in dict.fromkeys(declared)
            if envelope.artifact_delivery_modes.get(ref) == "reference"
        ]

    @staticmethod
    def _task_submission_schema(
        kind: str,
        contract,
        *,
        module_id: str | None = None,
        submodule_id: str | None = None,
        collaboration_request_ids: list[str] | None = None,
    ) -> dict:
        """Specialize provider-visible review schemas to the exact active task."""

        schema = deepcopy(submission_schema(kind))

        def remove_property(node: dict, name: str) -> None:
            node.get("properties", {}).pop(name, None)
            node["required"] = [field for field in node.get("required", []) if field != name]

        if kind in MODULE_COLLABORATION_SUBMISSION_KINDS:
            if module_id is None:
                raise ValueError(
                    f"{kind} requires one fixed module specialist identity"
                )
            schema.get("properties", {}).get("module_id", {}).update(
                {"const": module_id}
            )
            if kind in {
                "submodule_discovery_submission",
                "submodule_interface_response_submission",
            }:
                if submodule_id is None:
                    raise ValueError(
                        f"{kind} requires one fixed leaf-submodule identity"
                    )
                schema.get("properties", {}).get("submodule_id", {}).update(
                    {"const": submodule_id}
                )
            if (
                kind in {
                    "module_interface_response_submission",
                    "submodule_interface_response_submission",
                }
                and collaboration_request_ids is not None
            ):
                dispositions = schema.get("properties", {}).get(
                    "dispositions", {}
                )
                dispositions.update(
                    {
                        "minItems": len(collaboration_request_ids),
                        "maxItems": len(collaboration_request_ids),
                    }
                )
                (
                    schema.get("$defs", {})
                    .get("InterfaceDisposition", {})
                    .get("properties", {})
                    .get("request_id", {})
                    .update({"enum": list(collaboration_request_ids)})
                )
            examples = schema.get("examples", [])
            if examples and isinstance(examples[0], dict):
                example = examples[0]
                example["module_id"] = module_id
                if kind == "submodule_discovery_submission":
                    example.update(
                        {
                            "submodule_id": submodule_id,
                            "discovery_summary": (
                                f"已完成固定叶子 {submodule_id} 的独立证据发现。"
                            ),
                            "evidence_ids": [],
                            "evidence_gaps": [],
                            "provisional_findings": [],
                            "interface_signals": [],
                        }
                    )
                elif kind == "submodule_discovery_batch_submission":
                    first_leaf = next(iter(REPORT_TAXONOMY[module_id].submodules))
                    example["discoveries"] = [
                        {
                            "kind": "submodule_discovery_submission",
                            "module_id": module_id,
                            "submodule_id": first_leaf,
                            "discovery_summary": (
                                f"已完成固定叶子 {first_leaf} 的独立证据发现。"
                            ),
                            "evidence_ids": [],
                            "evidence_gaps": [],
                            "provisional_findings": [],
                            "interface_signals": [],
                        }
                    ]
                elif kind == "module_discovery_submission":
                    example.update(
                        {
                            "discovery_summary": (
                                f"已完成模块 {module_id} 的证据与接口覆盖发现。"
                            ),
                            "evidence_ids": [],
                            "interface_coverage": [
                                {
                                    "target_module_id": peer_id,
                                    "status": "not_applicable",
                                    "rationale": "当前发现没有形成该模块接口依赖。",
                                }
                                for peer_id in REPORT_TAXONOMY
                                if peer_id != module_id
                            ],
                            "requests": [],
                        }
                    )
                elif kind == "submodule_interface_response_submission":
                    example["submodule_id"] = submodule_id
                    example["dispositions"] = [
                        {
                            "request_id": request_id,
                            "status": "unresolved",
                            "evidence_ids": [],
                            "conditions": [],
                            "unresolved_reason": "当前证据不足以形成可靠回答。",
                            "boundary": "正文保留该接口为未决边界，不推断目标模块事实。",
                        }
                        for request_id in (collaboration_request_ids or [])
                    ]
                elif kind == "module_interface_response_submission" and (
                    collaboration_request_ids is not None
                ):
                    example["dispositions"] = [
                        {
                            "request_id": request_id,
                            "status": "unresolved",
                            "evidence_ids": [],
                            "conditions": [],
                            "unresolved_reason": "当前证据不足以形成可靠回答。",
                            "boundary": "保留该接口为未决边界，不推断请求方事实。",
                        }
                        for request_id in collaboration_request_ids
                    ]
        elif isinstance(contract, SubmoduleAuthoringInput):
            schema.get("properties", {}).get("module_id", {}).update(
                {"const": contract.module_id}
            )
            schema.get("properties", {}).get("submodule_id", {}).update(
                {"const": contract.submodule_id}
            )
            schema.get("properties", {}).get("revision", {}).update(
                {"const": contract.revision}
            )
        elif isinstance(contract, ModuleAuthoringInput):
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
            remove_property(
                schema.get("$defs", {}).get("CrossReviewFinding", {}),
                "id",
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
        examples = schema.get("examples", [])
        if examples and isinstance(examples[0], dict):
            example = examples[0]
            if isinstance(contract, ModuleAuthoringInput):
                example["module_id"] = contract.module_id
                example["revision"] = contract.revision
            elif isinstance(contract, SubmoduleAuthoringInput):
                example["module_id"] = contract.module_id
                example["submodule_id"] = contract.submodule_id
                example["revision"] = contract.revision
            elif isinstance(contract, ModuleRevisionInput):
                example["module_id"] = contract.module_id
                example["base_revision"] = contract.subject.revision
                example["revision"] = contract.subject.revision + 1
            example.pop("coverage", None)
            example.pop("checked_section_ids", None)
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
                elif isinstance(contract, FinalReviewInput):
                    base["evidence_refs"] = [contract.subject_ref]
                example["verdicts"] = [deepcopy(base) for _ in required_findings]
        return schema

    def _collaboration_request_ids(
        self,
        envelope: TaskEnvelope,
        module_id: str | None,
        submodule_id: str | None,
    ) -> list[str] | None:
        """Load the exact sparse Wave 2 inbox for task-scoped schema binding."""

        if (
            not {
                "module_interface_response_submission",
                "submodule_interface_response_submission",
            }.intersection(envelope.allowed_outputs)
        ):
            return None
        if module_id is None:
            raise ValueError("Wave 2 response requires a fixed module identity")
        inbox_refs = [
            ref
            for ref in envelope.input_refs
            if "/collaboration/inboxes/" in ref
        ]
        if len(inbox_refs) != 1:
            raise ValueError(
                "Wave 2 response requires exactly one collaboration inbox ref"
            )
        submodule_response = (
            "submodule_interface_response_submission"
            in envelope.allowed_outputs
        )
        if submodule_response and submodule_id is None:
            raise ValueError("Wave 2 submodule response requires one fixed leaf identity")
        expected_ref = (
            f"Work/runs/{envelope.run_id}/collaboration/inboxes/"
            + (
                f"submodule-{submodule_id}.json"
                if submodule_response
                else f"module-{module_id}.json"
            )
        )
        if inbox_refs[0] != expected_ref:
            raise ValueError(
                "Wave 2 collaboration inbox must use its canonical current-run path"
            )
        inbox_path = (self.workspace / inbox_refs[0]).resolve()
        run_root = (
            self.workspace / f"Work/runs/{envelope.run_id}"
        ).resolve()
        if (
            (self.workspace / inbox_refs[0]).is_symlink()
            or not inbox_path.is_relative_to(run_root)
            or not inbox_path.is_file()
        ):
            raise ValueError("Wave 2 collaboration inbox is not readable")
        try:
            inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("Wave 2 collaboration inbox is invalid JSON") from exc
        requests = inbox.get("requests") if isinstance(inbox, dict) else None
        if (
            inbox.get("kind")
            != (
                "submodule_interface_inbox"
                if submodule_response
                else "module_interface_inbox"
            )
            or inbox.get("run_id") != envelope.run_id
            or inbox.get("module_id") != module_id
            or (
                submodule_response
                and inbox.get("submodule_id") != submodule_id
            )
            or not isinstance(requests, list)
            or not requests
        ):
            raise ValueError(
                "Wave 2 collaboration inbox does not match the active module"
            )
        try:
            typed_requests = [
                InterfaceRequest.model_validate(item)
                for item in requests
            ]
        except ValueError as exc:
            raise ValueError(
                "Wave 2 collaboration inbox contains an invalid interface request"
            ) from exc
        if any(
            request.target_module_id != module_id
            for request in typed_requests
        ):
            raise ValueError(
                "Wave 2 collaboration inbox contains a request for another module"
            )
        if submodule_response and any(
            request.target_submodule_id != submodule_id
            for request in typed_requests
        ):
            raise ValueError(
                "Wave 2 collaboration inbox contains a request for another submodule"
            )
        request_ids = [
            request.request_id for request in typed_requests
        ]
        if (
            any(not isinstance(item, str) or not item for item in request_ids)
            or len(request_ids) != len(set(request_ids))
        ):
            raise ValueError(
                "Wave 2 collaboration inbox request_ids must be non-empty and unique"
            )
        return list(request_ids)

    @staticmethod
    def _result_part_item_schema(
        expected_part_ids: list[str],
        *,
        evidence_binding_required: bool,
    ) -> dict:
        """Return the exact provider-visible shape for one current-task prose part."""

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
        batch_size: int | None = None,
    ) -> dict:
        """Specialize single and batch result-part tools to the active task."""

        item_schema = cls._result_part_item_schema(
            expected_part_ids,
            evidence_binding_required=evidence_binding_required,
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
            [
                *self._reference_artifacts(envelope),
                *reference_shared_artifacts,
            ],
            gateway=gateway,
        )
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
            )
        else:
            library = ReferenceLibrary(self.workspace)
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
        submodule_id = (
            envelope.target_submodule_ids[0]
            if len(envelope.target_submodule_ids) == 1
            else None
        )
        collaboration_outputs = (
            set(envelope.allowed_outputs) & MODULE_COLLABORATION_SUBMISSION_KINDS
        )
        if collaboration_outputs and (
            len(envelope.allowed_outputs) != 1 or module_id is None
        ):
            raise ValueError(
                "a collaboration wave task requires exactly one collaboration "
                "submission kind and one fixed module specialist identity"
            )
        if (
            collaboration_outputs & SUBMODULE_SCOPED_SUBMISSION_KINDS
            and submodule_id is None
        ):
            raise ValueError(
                "a submodule collaboration task requires exactly one fixed leaf scope"
            )
        collaboration_request_ids = self._collaboration_request_ids(
            envelope,
            module_id,
            submodule_id,
        )
        task_submission_schemas = {
            kind: self._task_submission_schema(
                kind,
                input_contract,
                module_id=module_id,
                submodule_id=submodule_id,
                collaboration_request_ids=collaboration_request_ids,
            )
            for kind in envelope.allowed_outputs
        }
        expected_result_part_ids = (
            list(template_inspection.required_part_ids)
            if template_inspection is not None
            else list(input_contract.required_submodule_ids)
            if isinstance(input_contract, ModuleAuthoringInput)
            else [input_contract.submodule_id]
            if isinstance(input_contract, SubmoduleAuthoringInput)
            else list(input_contract.target_submodule_ids)
            if isinstance(input_contract, ModuleRevisionInput)
            else [
                CHIEF_SECTION_RESULT_PART_IDS[section_id]
                for section_id in input_contract.target_section_ids
            ]
            if isinstance(input_contract, ChiefRevisionInput)
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
                "submodule_draft_submission",
            }
            & set(envelope.allowed_outputs)
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
            "inspect_image": InspectImageTool(self.workspace),
            "open_artifact": OpenArtifactTool(
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
            ),
            "open_tool_result": OpenToolResultTool(gateway),
            "search_text": SearchTextTool(
                gateway,
                research_guard,
                allowed_refs=access.readable_refs,
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
            ),
            "write_result_part": WriteResultPartTool(
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
                self.store,
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
                required_synthesis_input_ids=required_synthesis_input_ids,
            ),
            "list_result_parts": ListResultPartsTool(
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
                self.store,
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
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
        for name in access.tool_names:
            if name not in available:
                raise ValueError(f"unsupported tool in {definition.id}: {name}")
            registry.register(available[name])
        if registry.get("write_result_part") is not None:
            # Expose one exact write shape to providers. The former automatic batch
            # companion encouraged providers to stringify ``parts`` or omit fields,
            # then retry already-persisted prose. Internal callers may still use the
            # batch implementation, but model-facing reporting identities always use
            # the single-part protocol plus list_result_parts for durable state.
            registry._schema_cache["write_result_part"] = self._result_part_tool_schema(
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
            )
        if "submit_result" in definition.tools:
            output_schemas = list(task_submission_schemas.values())
            if not output_schemas:
                raise ValueError(
                    f"{definition.id} has submit_result but no known allowed output contract"
                )
            payload_schema = (
                {
                    **output_schemas[0],
                    "description": (
                        f"{output_schemas[0].get('description', '').strip()} "
                        "Pass payload as a native JSON object. Never JSON-encode, quote, "
                        "or stringify the complete object."
                    ).strip(),
                }
                if len(output_schemas) == 1
                else {
                    "oneOf": output_schemas,
                    "description": (
                        "Submit exactly one of the output contracts explicitly allowed "
                        "by this task. Pass payload as a native JSON object; never "
                        "JSON-encode, quote, or stringify the complete object."
                    ),
                }
            )
            registry._schema_cache["submit_result"] = {
                "type": "object",
                "properties": {
                    "payload": payload_schema,
                },
                "required": ["payload"],
                "additionalProperties": False,
            }
        return registry

    @staticmethod
    def _identity_key(
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        session_key: str | None,
    ) -> str:
        """Scope concurrent leaf tasks without weakening durable role ownership."""

        if definition.id == "evidence-auditor" and session_key:
            return session_key
        if (
            session_key
            and re.fullmatch(r"module-2\.[1-5]-specialist", definition.id)
            and len(envelope.target_submodule_ids) == 1
            and (
                set(envelope.allowed_outputs) & SUBMODULE_SCOPED_SUBMISSION_KINDS
                or "module_revision_submission" in envelope.allowed_outputs
            )
            and session_key == f"submodule-{envelope.target_submodule_ids[0]}"
        ):
            return session_key
        return definition.id

    async def run(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        workflow_id: str,
        session_key: str | None = None,
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
        try:
            return await self._run_with_identity_lease(
                definition,
                envelope,
                shared_artifacts,
                workflow_id=workflow_id,
                session_key=session_key,
                identity_lease=lease_handle.lease,
            )
        finally:
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
    ) -> AgentResult:
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
                    runtime_id = f"{definition.id}--{recovery_session_id}"
                    self._record_identity(
                        workflow_id=workflow_id,
                        envelope=envelope,
                        identity_key=identity_key,
                        session_id=recovery_session_id,
                        runtime_id=runtime_id,
                        status="completed",
                    )
                    return recovered_result
        ambiguous_provider_refs = self._ambiguous_provider_call_refs(
            envelope,
            session_id=recovery_session_id,
        )
        if ambiguous_provider_refs:
            raise ProviderAttemptRecoveryRequired(ambiguous_provider_refs)
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
            runtime_id = f"{definition.id}--{session_id}"
            config = resolved_config
            loop = AgentLoop(
                agent_type=runtime_id,
                workspace=self.workspace,
                tools=self._tools(
                    definition,
                    envelope,
                    session_id,
                    workflow_id,
                    gateway=gateway,
                    shared_artifacts=shared_artifacts,
                    task_correlation=task_correlation,
                ),
                bus=self.bus,
                config=config,
                llm_provider=routed_provider,
                system_prompt=system_prompt,
                usage_run_id=envelope.run_id,
                usage_task_id=envelope.task_id,
                artifact_gateway=gateway,
                before_provider_attempt=(
                    None
                    if self._provider_attempt_guard is None
                    else lambda: self._provider_attempt_guard(definition.id, envelope.task_id)
                ),
            )
            self._sessions[cache_key] = (loop, session_id, runtime_id)
            self._session_route_bindings[cache_key] = route_binding
            loop.usage_stage = task_kind
            router.register_session(definition.id, session_id, runtime_id)
            await loop.start()
        else:
            loop, session_id, runtime_id = cached
            task_correlation = self._task_correlation(
                envelope,
                workflow_id=workflow_id,
                identity_key=identity_key,
                session_id=session_id,
                identity_lease=identity_lease,
                execution_profile_sha256=resolved_profile.profile_sha256,
            )
            TaskAttemptStore(self.workspace, envelope.run_id).activate(task_correlation)
            # A durable role identity is not a license to replay every prior task
            # prompt. Each reporting transition carries a complete typed input
            # contract, so start the new task with clean provider working memory.
            # Tool follow-ups and continuation slices inside this run() call still
            # share the same conversation.
            loop.reset_working_memory_for_typed_task()
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
            )
            loop.usage_run_id = envelope.run_id
            loop.usage_task_id = envelope.task_id
            loop.usage_stage = task_kind
            loop.before_provider_attempt = (
                None
                if self._provider_attempt_guard is None
                else lambda: self._provider_attempt_guard(definition.id, envelope.task_id)
            )
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

        async def wait_result() -> AgentResult:
            message = await self.bus.wait_for(
                AgentResultMessage,
                lambda item: (
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
                timeout=self.timeout,
            )
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
            result_waiter = asyncio.create_task(wait_result())
            final_waiter = asyncio.create_task(
                self.bus.wait_for(
                    AgentResponse,
                    lambda item: (
                        item.agent_type == runtime_id
                        and item.message_id == envelope.task_id
                        and item.workflow_id == workflow_id
                        and item.run_id == envelope.run_id
                        and item.task_id == envelope.task_id
                        and item.task_attempt_id == envelope.task_attempt_id
                        and item.session_id == session_id
                        and not item.streaming
                    ),
                    timeout=self.timeout,
                )
            )
            error_waiter = asyncio.create_task(
                self.bus.wait_for(
                    Error,
                    lambda item: (
                        item.source == runtime_id
                        and item.workflow_id == workflow_id
                        and item.run_id == envelope.run_id
                        and item.task_id == envelope.task_id
                        and item.task_attempt_id == envelope.task_attempt_id
                        and item.session_id == session_id
                    ),
                    timeout=self.timeout,
                )
            )
            waiters = {result_waiter, final_waiter, error_waiter}
            try:
                await asyncio.sleep(0)
                await self.bus.publish(
                    UserMessage(
                        agent_type=runtime_id,
                        source="workflow",
                        message_id=envelope.task_id,
                        content=content,
                        internal=internal,
                        provider_stream_idle_timeout_seconds=(provider_stream_idle_timeout_seconds),
                        workflow_id=workflow_id,
                        run_id=envelope.run_id,
                        task_id=envelope.task_id,
                        task_attempt_id=envelope.task_attempt_id,
                        session_id=session_id,
                        turn_kind=turn_kind,
                    )
                )
                done, _pending = await asyncio.wait(
                    waiters,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if result_waiter in done:
                    return result_waiter.result()
                if error_waiter in done:
                    error = error_waiter.result()
                    marker = "REPORTING_RUN_BUDGET_EXHAUSTED:"
                    if marker in error.message:
                        from .workflow import ReportingNeedsDecisionError

                        raise ReportingNeedsDecisionError(error.message.split(marker, 1)[1].strip())
                    raise RuntimeError(error.message)
                return await final_waiter
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

        try:

            async def finish_tool_slices(
                turn: AgentResult | AgentResponse,
                *,
                origin_turn_kind: str = "task_initial",
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
                    profile_limit_reached = count >= limits[continuation_kind]
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
                    if stalled or profile_limit_reached:
                        stop_reason = (
                            "repeated_no_progress"
                            if stalled
                            else "execution_profile_continuation_limit"
                        )
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

                    state["continuation_counts"][continuation_kind] = count + 1
                    event["decision"] = "continue"
                    state["events"].append(event)
                    state["status"] = "continuing"
                    save_state(state)
                    if max_tokens_continuation:
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
                            if "list_result_parts" in definition.tools
                            else "继续使用当前输入引用和已经获得的上下文；不要重新检索或重读已有内容。"
                        )
                    boundary_explanation = (
                        "上一模型输出达到单次生成上限，但任务没有失败。你仍是原 Agent。"
                        if max_tokens_continuation
                        else "上一工具执行片段已达到单次轮次边界，但任务没有失败。你仍是原 Agent。"
                    )
                    continuation_message = (
                        "<same_identity_continuation>\n"
                        f"{boundary_explanation}{continuation_instruction}"
                        "完成后必须调用 submit_result 提交本任务规定的结构化结果。\n"
                        "</same_identity_continuation>"
                    )
                    turn = await one_turn(
                        continuation_message,
                        internal=True,
                        turn_kind=continuation_kind,
                        provider_stream_idle_timeout_seconds=(
                            REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS
                        ),
                    )
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
                        "write_result_part，分别写入 skill、analysis、synthesis、visual、rubric；"
                        "然后调用 submit_result 提交 template_skill_submission，五个长文本字段"
                        "只使用 write_result_part 返回的 artifact_ref，并按 input contract "
                        "补齐 boundary_manifest。只有 submit_result 工具成功才可结束。\n"
                        "</submission_correction>"
                    )
                elif definition.id == "chief-editor":
                    part_instruction = (
                        "只补齐 list_result_parts 列出的目标修订章节"
                        if envelope.input_contract_kind == "chief_revision_input"
                        else "补齐 list_result_parts 列出的十二个固定章节"
                    )
                    submission_instruction = (
                        "提交小型 chief_revision_submission；不得重复父版本全文、"
                        "Cross dispositions、表格、图片或未决问题"
                        if envelope.input_contract_kind == "chief_revision_input"
                        else (
                            "提交 edited_report_submission；只提交当前合同声明的实际章节字段，"
                            "不要恢复已删除的跨领域风险模块或其旧版综合元数据"
                        )
                    )
                    module_instruction = (
                        "不得提交 module_narratives 或任何第二章内容。"
                        if envelope.input_contract_kind == "chief_revision_input"
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
                elif (
                    set(envelope.allowed_outputs)
                    & MODULE_COLLABORATION_SUBMISSION_KINDS
                ):
                    correction = (
                        "<submission_correction>\n"
                        "你已经完成本轮协作分析，但尚未提交类型化结果。"
                        f"立即使用现有上下文调用 submit_result 提交唯一的 {expected}；"
                        "不得开始最终正文、不得调用正文分段工具、不得重新检索。\n"
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
                corrected = await finish_tool_slices(
                    await one_turn(
                        correction,
                        internal=True,
                        turn_kind="submission_correction",
                        provider_stream_idle_timeout_seconds=(
                            REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS
                        ),
                    ),
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
            self._save_conversation_trace(
                loop, envelope, runtime_id, session_id, status="cancelled"
            )
            raise
        except Exception as exc:
            self._save_conversation_trace(
                loop,
                envelope,
                runtime_id,
                session_id,
                status="failed",
                error=str(exc),
            )
            raise
        await loop.wait_until_turn_complete()
        self._save_conversation_trace(
            loop, envelope, runtime_id, session_id, status=result.status.value
        )
        self._save_session_summary(loop, envelope, shared_artifacts, result)
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
        status: str,
        error: str | None = None,
    ) -> Path:
        """Persist the Agent transcript even when the provider fails or is cancelled."""

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
        trace = {
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "agent_id": envelope.agent_id,
            "runtime_id": runtime_id,
            "session_id": session_id,
            "status": status,
            "error": error,
            "messages": json_value(list(loop._conversation_history)),
        }
        serialized = (
            json.dumps(
                trace,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        compressed = gzip.compress(serialized, compresslevel=9, mtime=0)
        trace_root = (
            self.workspace
            / f"Work/runs/{envelope.run_id}/agent-conversations"
        )
        trace_root.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{safe_runtime_id}-",
                suffix=".json.gz.tmp",
                dir=trace_root,
                delete=False,
            ) as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            blob = ContentAddressedStore(self.workspace).ingest_file(temporary_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return self.store.write_json(
            f"Work/runs/{envelope.run_id}/agent-conversations/{safe_runtime_id}.json",
            {
                "manifest_version": 2,
                "run_id": envelope.run_id,
                "task_id": envelope.task_id,
                "agent_id": envelope.agent_id,
                "runtime_id": runtime_id,
                "session_id": session_id,
                "status": status,
                "error": error,
                "encoding": "gzip+json",
                "transcript_semantics": (
                    "provider_working_history_protocol_valid_v3"
                ),
                "forensic_exact_tool_arguments": True,
                "forensic_note": (
                    "Retained provider-history tool calls preserve every required argument, "
                    "including successful write_result_part content. Cost control removes "
                    "only complete older messages through the general working-memory "
                    "checkpoint path; no reusable prose marker or malformed tool call is exposed."
                ),
                "transcript_ref": blob.relative_path.as_posix(),
                "transcript_sha256": hashlib.sha256(serialized).hexdigest(),
                "compressed_sha256": blob.sha256,
                "uncompressed_bytes": len(serialized),
                "compressed_bytes": blob.size,
                "message_count": len(loop._conversation_history),
            },
        )

    def _save_session_summary(
        self,
        loop: AgentLoop,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        result: AgentResult,
    ) -> Path:
        builder = SessionSummaryBuilder(self.workspace)
        skeleton = builder.build_skeleton(
            envelope,
            shared_artifacts,
            session_id=result.session_id,
            message_log=list(loop._conversation_history),
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
            loop, _session_id, runtime_id = self._sessions.pop(key)
            self._session_route_bindings.pop(key, None)
            await loop.stop()
        router = self._routers.pop(workflow_id, None)
        if router is not None:
            router.close()
