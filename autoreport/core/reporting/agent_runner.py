"""Run one reporting identity in an isolated, real AutoReport AgentLoop."""

from __future__ import annotations

import asyncio
import ast
import json
import operator
import os
from html import escape
from pathlib import Path
from uuid import uuid4

from docx import Document
from PIL import Image
from pydantic import TypeAdapter

from ...config.schema import AgentDefaults
from ...interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    PeerQueryMessage,
    UserMessage,
)
from ..loops.agent_loop import AgentLoop
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
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
from .agentic_models import AgentResult, AgentRunStatus, Submission, TaskEnvelope
from .config import AgentDefinition
from .prompts import PromptAssembler
from .research.reference_library import ReferenceLibrary
from .research.web import BraveWebResearchBackend, DisabledWebResearchBackend
from .source_ledger import SourceLedger
from .store import ReportingStore


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
        if "02_本地skill提示词资料_禁止导入" in target.parts:
            raise ValueError("runtime access to 02 is forbidden")
        if target.suffix.casefold() == ".docx":
            document = Document(target)
            text = "\n".join(p.text for p in document.paragraphs)
        else:
            text = target.read_text(encoding="utf-8", errors="ignore")
        return {"path": target.relative_to(self.workspace).as_posix(), "text": text[:max_chars]}


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
        with Image.open(target) as image:
            return {"path": path, "width": image.width, "height": image.height, "format": image.format}


class CalculateTool(Tool):
    name = "calculate"
    description = "Evaluate a basic arithmetic expression with no names or code execution."
    _ops = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos,
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
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.llm_provider = llm_provider
        self.defaults = defaults
        self.timeout = timeout
        self.store = ReportingStore(self.workspace)
        self._runtime_by_identity: dict[str, str] = {}
        self._sessions: dict[tuple[str, str], tuple[AgentLoop, str, str]] = {}
        self.bus.subscribe(PeerQueryMessage, self._route_peer_query)

    async def _route_peer_query(self, message: PeerQueryMessage) -> None:
        runtime_id = self._runtime_by_identity.get(str(message.recipient))
        if runtime_id is None:
            return
        artifacts = "".join(
            f"<artifact_ref>{escape(ref)}</artifact_ref>" for ref in message.artifact_refs
        )
        await self.bus.publish(
            UserMessage(
                agent_type=runtime_id,
                source=str(message.sender),
                message_id=message.query_id,
                content=(
                    "<peer_query>"
                    f"<workflow_id>{escape(message.workflow_id)}</workflow_id>"
                    f"<task_id>{escape(message.task_id)}</task_id>"
                    f"<query_id>{escape(message.query_id)}</query_id>"
                    f"<source_agent>{escape(str(message.sender))}</source_agent>"
                    f"<source_session_id>{escape(message.source_session_id)}</source_session_id>"
                    f"<question>{escape(message.question)}</question>"
                    f"{artifacts}</peer_query>"
                ),
            )
        )

    def _tools(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        session_id: str,
        workflow_id: str,
    ) -> ToolRegistry:
        registry = ToolRegistry()
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
            "calculate": CalculateTool(),
            "publish_research_note": PublishResearchNoteTool(
                self.workspace, self.bus, workflow_id, envelope.run_id,
                envelope.task_id, definition.id,
            ),
            "query_peer": QueryPeerTool(
                self.bus, envelope.task_id, definition.id, session_id, workflow_id
            ),
            "reply_peer": ReplyPeerTool(self.bus, definition.id, workflow_id),
            "report_gap": ReportGapTool(
                definition.id, envelope.run_id, envelope.task_id, self.store,
                self.bus, workflow_id,
            ),
            "request_revision": RequestRevisionTool(
                self.bus, workflow_id, envelope.task_id, definition.id
            ),
            "submit_result": SubmitResultTool(
                definition.id, session_id, envelope.run_id, envelope.task_id,
                self.store, self.bus, workflow_id,
            ),
            "report_blocked": ReportBlockedTool(
                definition.id, session_id, envelope.run_id, envelope.task_id,
                self.store, self.bus, workflow_id,
            ),
        }
        for name in definition.tools:
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
        cache_key = (workflow_id, session_key or envelope.task_id)
        cached = self._sessions.get(cache_key)
        if cached is None:
            session_id = f"session-{uuid4().hex[:12]}"
            runtime_id = f"{definition.id}--{session_id}"
            config = self.defaults.model_copy(
                update={"max_tool_iterations": min(definition.max_turns, 40)}
            )
            loop = AgentLoop(
                agent_type=runtime_id,
                workspace=self.workspace,
                tools=self._tools(definition, envelope, session_id, workflow_id),
                bus=self.bus,
                config=config,
                llm_provider=self.llm_provider,
                system_prompt=PromptAssembler.system_prompt(definition),
            )
            self._sessions[cache_key] = (loop, session_id, runtime_id)
            self._runtime_by_identity[definition.id] = runtime_id
            await loop.start()
        else:
            loop, session_id, runtime_id = cached
            loop.tools = self._tools(definition, envelope, session_id, workflow_id)
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
            await asyncio.sleep(0)
            await self.bus.publish(
                UserMessage(
                    agent_type=runtime_id,
                    source="workflow",
                    message_id=envelope.task_id,
                    content=content,
                )
            )
            done, pending = await asyncio.wait(
                {result_waiter, final_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for waiter in pending:
                waiter.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if result_waiter in done:
                return result_waiter.result()
            winner = final_waiter
            await winner
            return None

        try:
            result = await one_turn(task_message)
            if result is not None:
                return result
            result = await one_turn(
                "<completion_reminder>请整理当前成果并调用 submit_result；若确实无法完成，"
                "调用 report_blocked。不要重新开始研究。</completion_reminder>"
            )
            if result is not None:
                return result
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
            return incomplete
        finally:
            pass

    async def close_workflow(self, workflow_id: str) -> None:
        """Stop all isolated sessions retained for peer questions and local revision."""
        keys = [key for key in self._sessions if key[0] == workflow_id]
        for key in keys:
            loop, _session_id, runtime_id = self._sessions.pop(key)
            await loop.stop()
            for identity, active_runtime in list(self._runtime_by_identity.items()):
                if active_runtime == runtime_id:
                    self._runtime_by_identity.pop(identity, None)
