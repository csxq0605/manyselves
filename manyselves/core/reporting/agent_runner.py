"""Run one reporting identity in an isolated Manyselves AgentLoop."""

from __future__ import annotations

import ast
import asyncio
import json
import operator
import os
import re
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from ...config.schema import AgentDefaults
from ...interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    Error,
    PeerQueryMessage,
    PeerReplyMessage,
    UserMessage,
)
from ..loops.agent_loop import AgentLoop
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..artifacts import ArtifactGateway, ArtifactGrant, parse_artifact
from ..tools.artifact_tools import OpenArtifactTool, OpenToolResultTool, SearchTextTool
from ..tools.registry import Tool, ToolRegistry
from ..tools.reporting_collaboration_tools import (
    QueryPeerTool,
    ReplyPeerTool,
    ReportBlockedTool,
    ReportGapTool,
    RequestRevisionTool,
    SubmitResultTool,
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
from .agentic_models import AgentResult, AgentRunStatus, Submission, TaskEnvelope
from .capabilities import compile_agent_access, scoped_gateway
from .config import AgentDefinition
from .message_router import WorkflowMessageRouter
from .module_skills import ModuleSkillLibrary
from .prompts import PromptAssembler
from .research.reference_library import ReferenceLibrary
from .research.web import BraveWebResearchBackend, DisabledWebResearchBackend
from .session_summary import SessionSummaryBuilder, SessionSummaryStore
from .skills.resolver import RuntimeSkillResolver
from .source_ledger import SourceLedger
from .store import ReportingStore
from .versions import SkillProvenance


class InspectDocumentTool(Tool):
    name = "inspect_document"
    description = "Inspect a project-local text or DOCX artifact without changing it."

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    async def __call__(self, path: str, max_chars: int = 30000) -> dict:
        """Inspect a document.

        Args:
            path: Project-relative file path.
            max_chars: Maximum returned characters.
        """
        target = (self.workspace / path).resolve()
        if not target.is_relative_to(self.workspace) or not target.is_file():
            raise ValueError("document must be a file inside the project")
        parsed = parse_artifact(target)
        text = "\n".join(f"[{block.locator}] {block.text}" for block in parsed.blocks)
        return {
            "path": target.relative_to(self.workspace).as_posix(),
            "kind": parsed.kind,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "visual_verified": parsed.visual_verified,
            "error": parsed.error,
        }


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
    """Create a fresh loop per task, preserving identity and session isolation."""

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        llm_provider: LLMProvider,
        defaults: AgentDefaults,
        *,
        timeout: float = 600.0,
        product_skill_root: Path | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.llm_provider = llm_provider
        self.defaults = defaults
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

    def _system_prompt(self, definition: AgentDefinition, envelope: TaskEnvelope) -> str:
        module_id: str | None = None
        if definition.id == "evidence-auditor":
            match = re.search(r"2\.[1-5]", envelope.task_id)
            if match is None:
                raise ValueError("evidence-auditor task must identify one fixed module")
            module_id = match.group(0)
        skills = self.module_skills.for_agent(definition.id, module_id=module_id)
        index = self.module_skills.index_text() if definition.id == "report-planner" else None
        return PromptAssembler.system_prompt(
            definition,
            module_skills=skills,
            module_skill_index=index,
        )

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

    async def _route_peer_query(self, message: PeerQueryMessage) -> None:
        """Backward-compatible delegate; workflow routers own production routing."""
        router = self._routers.get(message.workflow_id)
        if router is not None:
            await router._route_query(message)
            return
        await self.bus.publish(
            PeerReplyMessage(
                workflow_id=message.workflow_id,
                task_id=message.task_id,
                query_id=message.query_id,
                sender="workflow",
                recipient=message.sender,
                target_session_id=message.source_session_id,
                answer=(
                    f"Peer '{message.recipient}' is not available because workflow "
                    f"'{message.workflow_id}' is not active."
                ),
                content="workflow closed",
            )
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
    ) -> ToolRegistry:
        registry = ToolRegistry()
        gateway = gateway or scoped_gateway(
            self._artifact_root,
            workflow_id=workflow_id,
            envelope=envelope,
            agent_id=definition.id,
            session_id=session_id,
        )
        access = compile_agent_access(
            definition,
            envelope,
            [
                *envelope.input_refs,
                *envelope.issue_refs,
                *envelope.context_summary_refs,
                *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
                *(shared_artifacts or []),
            ],
            gateway=gateway,
        )
        ledger = SourceLedger(self.workspace, envelope.run_id)
        library = ReferenceLibrary(self.workspace)
        key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
        web = BraveWebResearchBackend(key) if key else DisabledWebResearchBackend()
        available: dict[str, Tool] = {
            "search_project_evidence": SearchProjectEvidenceTool(self.workspace, ledger),
            "open_project_source": OpenProjectSourceTool(self.workspace, ledger),
            "search_reference_library": SearchReferenceLibraryTool(library, ledger),
            "open_reference": OpenReferenceTool(library, ledger),
            "web_search": WebSearchTool(web),
            "open_web_source": OpenWebSourceTool(web, ledger),
            "open_source": OpenWebSourceTool(web, ledger),
            "inspect_document": InspectDocumentTool(self.workspace),
            "inspect_image": InspectImageTool(self.workspace),
            "open_artifact": OpenArtifactTool(gateway),
            "open_tool_result": OpenToolResultTool(gateway),
            "search_text": SearchTextTool(gateway),
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
            "request_revision": RequestRevisionTool(
                self.bus, workflow_id, envelope.task_id, definition.id
            ),
            "submit_result": SubmitResultTool(
                definition.id,
                session_id,
                envelope.run_id,
                envelope.task_id,
                self.store,
                self.bus,
                workflow_id,
                expected_plan_agent_ids=envelope.expected_plan_agent_ids,
                allowed_outputs=envelope.allowed_outputs,
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
        if "submit_result" in definition.tools:
            registry._schema_cache["submit_result"] = {
                "type": "object",
                "properties": {
                    "payload": TypeAdapter(Submission).json_schema(),
                },
                "required": ["payload"],
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
        inherited_refs = [
            *router.research_notes,
            *router.gaps,
            *router.blocked_notices,
            *router.revision_requests.get(definition.id, []),
        ]
        shared_artifacts = list(dict.fromkeys([*shared_artifacts, *inherited_refs]))
        if router.revision_requests.get(definition.id):
            envelope = envelope.model_copy(
                update={
                    "issue_refs": list(
                        dict.fromkeys(
                            [*envelope.issue_refs, *router.revision_requests[definition.id]]
                        )
                    )
                }
            )
        cache_key = (workflow_id, session_key or envelope.task_id)
        cached = self._sessions.get(cache_key)
        gateway = scoped_gateway(
            self._artifact_root,
            workflow_id=workflow_id,
            envelope=envelope,
            agent_id=definition.id,
            session_id=(cached[1] if cached is not None else "pending"),
        )
        if cached is None:
            session_id = f"session-{uuid4().hex[:12]}"
            gateway = scoped_gateway(
                self._artifact_root,
                workflow_id=workflow_id,
                envelope=envelope,
                agent_id=definition.id,
                session_id=session_id,
            )
            runtime_id = f"{definition.id}--{session_id}"
            config = self.defaults.model_copy(
                update={"max_tool_iterations": min(definition.max_turns, 40)}
            )
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
                system_prompt=self._system_prompt(definition, envelope),
                usage_run_id=envelope.run_id,
                usage_task_id=envelope.task_id,
                artifact_gateway=gateway,
            )
            self._sessions[cache_key] = (loop, session_id, runtime_id)
            router.register_session(definition.id, session_id, runtime_id)
            await loop.start()
        else:
            loop, session_id, runtime_id = cached
            loop.artifact_gateway = gateway
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
        task_message = PromptAssembler.task_message(envelope, shared_artifacts)

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

        async def one_turn(content: str) -> AgentResult | None:
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
                    raise RuntimeError(error.message)
                await final_waiter
                return None
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

        result = await one_turn(task_message)
        if result is None:
            result = await one_turn(
                "<completion_reminder>请整理当前成果并调用 submit_result；若确实无法完成，"
                "调用 report_blocked。不要重新开始研究。</completion_reminder>"
            )
        if result is None:
            incomplete = AgentResult(
                task_id=envelope.task_id,
                run_id=envelope.run_id,
                agent_id=definition.id,
                session_id=session_id,
                status=AgentRunStatus.INCOMPLETE,
                reason="agent ended twice without a typed submission",
            )
            self.store.write_run_model(
                envelope.run_id, f"results/{envelope.task_id}.json", incomplete
            )
            result = incomplete
        self._save_session_summary(loop, envelope, shared_artifacts, result)
        return result

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
