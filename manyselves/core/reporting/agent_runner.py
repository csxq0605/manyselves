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
    WriteResultPartsTool,
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
from .input_contracts import (
    AggregateEditorInput,
    ChiefEditorInput,
    ChiefRevisionInput,
    CrossReviewInput,
    FinalReviewInput,
    ModuleAuthoringInput,
    ModuleReviewInput,
    ModuleRevisionInput,
    TemplateDistillationInput,
)
from .message_router import WorkflowMessageRouter, artifact_path_refs
from .models import CHIEF_RESULT_PART_IDS, CHIEF_SECTION_RESULT_PART_IDS
from .module_skills import ModuleSkillLibrary
from .prompts import PromptAssembler
from .research.evidence_memory import EvidenceResearchMemory
from .research.reference_library import ReferenceLibrary
from .research.web import BraveWebResearchBackend, DisabledWebResearchBackend
from .session_summary import SessionSummaryBuilder, SessionSummaryStore
from .skills.resolver import RuntimeSkillResolver
from .source_ledger import SourceLedger
from .store import ReportingStore
from .submission_contracts import submission_schema
from .versions import SkillProvenance

REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS = 600.0
REPORTING_AUDIT_STREAM_IDLE_TIMEOUT_SECONDS = 600.0
REPORTING_AUDIT_AGENT_IDS = frozenset(
    {
        "evidence-auditor",
        "cross-module-reviewer",
        "chief-editor-auditor",
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
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.llm_provider = llm_provider
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
            usage = (
                json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.is_file() else {}
            )
            used = int(usage.get(key, 0))
            usage[key] = used + 1
            self.store.write_json(f"Work/runs/{envelope.run_id}/research-tool-usage.json", usage)

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
                if definition.id == "evidence-auditor"
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
    ) -> Path:
        """Persist content-free context provenance for one provider dispatch."""

        root = f"Work/runs/{envelope.run_id}/context-manifests"
        index_ref = f"{root}/hash-index.json"
        index_path = self.workspace / index_ref
        hash_index = (
            json.loads(index_path.read_text(encoding="utf-8"))
            if index_path.is_file()
            else {}
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
            first = hash_index.setdefault(digest, occurrence)
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
                    first = hash_index.setdefault(digest, occurrence)
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
            "revision": envelope.revision,
            "agent_id": definition.id,
            "identity_key": identity_key,
            "session_id": session_id,
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
        self.store.write_json(index_ref, hash_index)
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
    ) -> Path:
        """Persist pre-adapter hashes, then backfill canonical provider payload metrics."""

        root = f"Work/runs/{envelope.run_id}/context-manifests"
        index_ref = f"{root}/provider-hash-index.json"
        index_path = self.workspace / index_ref
        hash_index = (
            json.loads(index_path.read_text(encoding="utf-8"))
            if index_path.is_file()
            else {}
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
            first = hash_index.setdefault(digest, occurrence)
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
            "provider_context_manifest_version": 2,
            "provider_call_id": provider_call_id,
            "run_id": envelope.run_id,
            "task_id": envelope.task_id,
            "revision": envelope.revision,
            "agent_id": definition.id,
            "identity_key": identity_key,
            "session_id": session_id,
            "provider_call_index": call_index,
            "phase": phase,
            "attempt": attempt,
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
        self.store.write_json(index_ref, hash_index)
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
        return self.store.write_json(relative, manifest)

    def _input_contract(self, envelope: TaskEnvelope):
        if not envelope.input_contract_kind or not envelope.input_contract_ref:
            return None
        model = {
            "module_authoring_input": ModuleAuthoringInput,
            "module_revision_input": ModuleRevisionInput,
            "module_review_input": ModuleReviewInput,
            "cross_review_input": CrossReviewInput,
            "final_review_input": FinalReviewInput,
            "chief_editor_input": ChiefEditorInput,
            "aggregate_editor_input": AggregateEditorInput,
            "chief_revision_input": ChiefRevisionInput,
        }.get(envelope.input_contract_kind)
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
                "description": "Complete reader-visible prose for this one durable part.",
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

    def _tools(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        session_id: str,
        workflow_id: str,
        *,
        gateway: ArtifactGateway | None = None,
        shared_artifacts: list[str] | None = None,
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
            template_path = (self.workspace / template_inspection.template_ref).resolve()
            if (
                not template_path.is_relative_to(self.workspace)
                or not template_path.is_file()
                or template_path.relative_to(self.workspace).as_posix()
                != template_inspection.template_ref
            ):
                raise ValueError(
                    "template distillation template_ref is not one canonical workspace file"
                )
        input_contract = self._input_contract(envelope)
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
            {"module_submission", "module_revision_submission"}
            & set(envelope.allowed_outputs)
        )
        result_part_batch_size = 4 if evidence_binding_required else 8
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
            "write_result_parts": WriteResultPartsTool(
                envelope.run_id,
                envelope.task_id,
                envelope.revision,
                self.store,
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
                required_synthesis_input_ids=required_synthesis_input_ids,
                max_batch_size=result_part_batch_size,
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
            # The batch tool is a compatible companion capability. Existing Agent
            # definitions and recovery envelopes keep their single-part declaration,
            # while current providers may choose either exact task-scoped shape.
            registry.register(available["write_result_parts"])
            registry._schema_cache["write_result_part"] = self._result_part_tool_schema(
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
            )
            registry._schema_cache["write_result_parts"] = self._result_part_tool_schema(
                expected_result_part_ids,
                evidence_binding_required=evidence_binding_required,
                batch_size=result_part_batch_size,
            )
        if "submit_result" in definition.tools:
            output_schemas = [
                self._task_submission_schema(
                    kind,
                    input_contract,
                )
                for kind in envelope.allowed_outputs
            ]
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

    async def run(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        workflow_id: str,
        session_key: str | None = None,
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
        identity_key = (
            session_key if definition.id == "evidence-auditor" and session_key else definition.id
        )
        cache_key = (workflow_id, identity_key)
        cached = self._sessions.get(cache_key)
        gateway = scoped_gateway(
            self._artifact_root,
            workflow_id=workflow_id,
            envelope=envelope,
            agent_id=definition.id,
            session_id=(cached[1] if cached is not None else "pending"),
        )
        system_prompt = self._system_prompt(definition, envelope)
        if cached is None:
            identity_digest = hashlib.sha256(
                f"{workflow_id}:{identity_key}".encode("utf-8")
            ).hexdigest()[:12]
            session_id = f"session-{identity_digest}"
            gateway = scoped_gateway(
                self._artifact_root,
                workflow_id=workflow_id,
                envelope=envelope,
                agent_id=definition.id,
                session_id=session_id,
            )
            runtime_id = f"{definition.id}--{session_id}"
            config = self._loop_config(definition, envelope)
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
                ),
                bus=self.bus,
                config=config,
                llm_provider=self.llm_provider,
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
            loop.usage_stage = self._usage_stage(definition, envelope)
            router.register_session(definition.id, session_id, runtime_id)
            await loop.start()
        else:
            loop, session_id, runtime_id = cached
            # A durable role identity is not a license to replay every prior task
            # prompt. Each reporting transition carries a complete typed input
            # contract, so start the new task with clean provider working memory.
            # Tool follow-ups and continuation slices inside this run() call still
            # share the same conversation.
            loop.reset_working_memory_for_typed_task()
            loop.config = self._loop_config(definition, envelope)
            loop.artifact_gateway = gateway
            loop._system_prompt_override = system_prompt
            loop.tools = self._tools(
                definition,
                envelope,
                session_id,
                workflow_id,
                gateway=gateway,
                shared_artifacts=shared_artifacts,
            )
            loop.usage_run_id = envelope.run_id
            loop.usage_task_id = envelope.task_id
            loop.usage_stage = self._usage_stage(definition, envelope)
            loop.before_provider_attempt = (
                None
                if self._provider_attempt_guard is None
                else lambda: self._provider_attempt_guard(definition.id, envelope.task_id)
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
        )

        async def wait_result() -> AgentResult:
            message = await self.bus.wait_for(
                AgentResultMessage,
                lambda item: (
                    item.workflow_id == workflow_id
                    and item.run_id == envelope.run_id
                    and item.task_id == envelope.task_id
                    and item.sender == definition.id
                ),
                timeout=self.timeout,
            )
            raw = json.loads((self.workspace / message.result_path).read_text(encoding="utf-8"))
            return AgentResult.model_validate(raw)

        async def persist_untyped_completion(response: AgentResponse) -> AgentResult:
            """Return a natural Agent completion to the workflow without guessing its type."""

            incomplete = AgentResult(
                task_id=envelope.task_id,
                run_id=envelope.run_id,
                agent_id=definition.id,
                session_id=session_id,
                status=AgentRunStatus.INCOMPLETE,
                raw_output=response.content,
                reason="agent ended without a typed submission",
            )
            result_relative = f"results/{envelope.task_id}.json"
            canonical = self.workspace / "Work/runs" / envelope.run_id / result_relative
            if canonical.is_file():
                try:
                    existing = AgentResult.model_validate_json(
                        canonical.read_text(encoding="utf-8")
                    )
                except (ValueError, OSError):
                    existing = None
                if existing is not None and existing.status == AgentRunStatus.COMPLETED:
                    result_relative = (
                        f"results/attempts/{envelope.task_id}-r{envelope.revision}-"
                        f"{session_id}-incomplete.json"
                    )
            path = self.store.write_run_model(envelope.run_id, result_relative, incomplete)
            relative = path.relative_to(self.workspace).as_posix()
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
                )
            )
            return incomplete

        async def one_turn(
            content: str,
            *,
            internal: bool = False,
            provider_stream_idle_timeout_seconds: float | None = None,
        ) -> AgentResult | AgentResponse:
            result_waiter = asyncio.create_task(wait_result())
            final_waiter = asyncio.create_task(
                self.bus.wait_for(
                    AgentResponse,
                    lambda item: (
                        item.agent_type == runtime_id
                        and item.message_id == envelope.task_id
                        and not item.streaming
                    ),
                    timeout=self.timeout,
                )
            )
            error_waiter = asyncio.create_task(
                self.bus.wait_for(
                    Error,
                    lambda item: item.source == runtime_id,
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
            ) -> AgentResult | AgentResponse:
                """Continue one durable Agent until the current action yields a real result."""

                while isinstance(turn, AgentResponse) and turn.content in {
                    AGENT_TURN_CONTINUATION_REQUIRED,
                    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
                }:
                    max_tokens_continuation = turn.content == AGENT_MAX_TOKENS_CONTINUATION_REQUIRED
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
                        provider_stream_idle_timeout_seconds=(
                            REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS
                        ),
                    )
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
            if isinstance(turn, AgentResponse) and envelope.allowed_outputs:
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
                        provider_stream_idle_timeout_seconds=(
                            REPORTING_SUBMISSION_STREAM_IDLE_TIMEOUT_SECONDS
                        ),
                    )
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
                    "provider_working_history_compacted_v1"
                ),
                "forensic_exact_tool_arguments": False,
                "forensic_note": (
                    "Successful long write_result_part(s) arguments are replaced "
                    "in provider history by sha256/artifact_ref markers after the "
                    "full content is persisted; bus and UI events remain unmodified."
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
            await loop.stop()
        router = self._routers.pop(workflow_id, None)
        if router is not None:
            router.close()
