"""Typed, artifact-oriented collaboration and completion tools."""

from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path
from uuid import uuid4

from ...interfaces.types import (
    AgentResultMessage,
    BlockedNoticeMessage,
    PeerQueryMessage,
    PeerReplyMessage,
    ProgressNoteMessage,
    RevisionRequestMessage,
    UserMessage,
)
from ..loops.bus import MessageBus
from ..reporting.agentic_models import AgentResult, AgentRunStatus
from ..reporting.store import ReportingStore
from .registry import Tool


class _ResultTool(Tool):
    def __init__(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        task_id: str,
        store: ReportingStore,
        bus: MessageBus,
        workflow_id: str = "",
    ):
        if Path(task_id).name != task_id or not task_id:
            raise ValueError("task_id must be a single safe path component")
        self.agent_id = agent_id
        self.session_id = session_id
        self.run_id = run_id
        self.task_id = task_id
        self.store = store
        self.bus = bus
        self.workflow_id = workflow_id

    async def _persist_and_publish(self, result: AgentResult) -> str:
        path = self.store.write_run_model(
            self.run_id, f"results/{self.task_id}.json", result
        )
        relative = path.relative_to(self.store.workspace).as_posix()
        await self.bus.publish(
            AgentResultMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                run_id=self.run_id,
                sender=self.agent_id,
                recipient="workflow",
                artifact_refs=[relative],
                result_path=relative,
                status=result.status.value,
                content=result.reason or "",
            )
        )
        return relative


class SubmitResultTool(_ResultTool):
    name = "submit_result"
    description = (
        "Persist the current task's typed final result. Call when the assigned work is "
        "complete; research is optional and is not a prerequisite."
    )

    async def __call__(self, payload: dict) -> dict:
        """Submit a typed result.

        Args:
            payload: One allowed typed workflow submission for the active task.
        """
        result = AgentResult(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            status=AgentRunStatus.COMPLETED,
            payload=payload,
        )
        relative = await self._persist_and_publish(result)
        return {"status": "completed", "result_path": relative}


class ReportBlockedTool(_ResultTool):
    name = "report_blocked"
    description = (
        "Persist a terminal blocked result with the concrete missing input or decision."
    )

    async def __call__(
        self, reason: str, artifact_refs: list[str] | None = None
    ) -> dict:
        """Report a blocked task.

        Args:
            reason: Concrete reason reliable completion is impossible.
            artifact_refs: Existing artifacts that demonstrate or contextualize the block.
        """
        result = AgentResult(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            status=AgentRunStatus.BLOCKED,
            reason=reason,
        )
        relative = await self._persist_and_publish(result)
        await self.bus.publish(
            BlockedNoticeMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                sender=self.agent_id,
                recipient="workflow",
                artifact_refs=[relative, *(artifact_refs or [])],
                content=reason,
                reason=reason,
                priority="high",
            )
        )
        return {"status": "blocked", "result_path": relative}


class QueryPeerTool(Tool):
    name = "query_peer"
    description = (
        "Ask one named peer a focused question, passing artifact references instead of "
        "full evidence or drafts, and wait for the matching session-scoped reply."
    )

    def __init__(
        self,
        bus: MessageBus,
        task_id: str,
        agent_id: str,
        session_id: str,
        workflow_id: str = "",
        timeout: float = 120.0,
    ):
        self.bus = bus
        self.task_id = task_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.workflow_id = workflow_id
        self.timeout = timeout

    async def __call__(
        self,
        target_agent: str,
        question: str,
        artifact_refs: list[str] | None = None,
    ) -> dict:
        """Query a peer.

        Args:
            target_agent: Registry id of the responsible peer.
            question: Focused question the peer can answer independently.
            artifact_refs: Shared project artifacts relevant to the question.
        """
        query_id = f"Q-{uuid4().hex[:12]}"
        waiter = asyncio.create_task(
            self.bus.wait_for(
                PeerReplyMessage,
                lambda message: (
                    message.workflow_id == self.workflow_id
                    and message.task_id == self.task_id
                    and message.query_id == query_id
                    and message.recipient == self.agent_id
                    and message.target_session_id == self.session_id
                ),
                timeout=self.timeout,
            )
        )
        # Let wait_for install its one-shot subscriber before the query is queued.
        await asyncio.sleep(0)
        await self.bus.publish(
            PeerQueryMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                query_id=query_id,
                sender=self.agent_id,
                recipient=target_agent,
                source_session_id=self.session_id,
                question=question,
                artifact_refs=artifact_refs or [],
                content=question,
            )
        )
        reply = await waiter
        return {
            "query_id": query_id,
            "status": "replied",
            "answer": reply.answer,
            "source_ids": reply.source_ids,
            "artifact_refs": reply.artifact_refs,
        }


class ReplyPeerTool(Tool):
    name = "reply_peer"
    description = "Reply to an existing peer query and identify any supporting sources."

    def __init__(
        self, bus: MessageBus, agent_id: str, workflow_id: str = ""
    ):
        self.bus = bus
        self.agent_id = agent_id
        self.workflow_id = workflow_id

    async def __call__(
        self,
        task_id: str,
        query_id: str,
        target_agent: str,
        target_session_id: str,
        answer: str,
        source_ids: list[str] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> dict:
        """Reply to a peer.

        Args:
            task_id: Task id from the peer query.
            query_id: Query id from the peer query.
            target_agent: Agent that issued the query.
            target_session_id: Session id from the peer query.
            answer: Focused answer to the question.
            source_ids: E-*, R-*, or W-* records supporting the answer.
            artifact_refs: Shared artifacts containing longer supporting material.
        """
        await self.bus.publish(
            PeerReplyMessage(
                workflow_id=self.workflow_id,
                task_id=task_id,
                query_id=query_id,
                sender=self.agent_id,
                recipient=target_agent,
                target_session_id=target_session_id,
                answer=answer,
                source_ids=source_ids or [],
                artifact_refs=artifact_refs or [],
                content=answer,
            )
        )
        return {"query_id": query_id, "status": "replied"}


class ReportGapTool(Tool):
    name = "report_gap"
    description = (
        "Persist a non-terminal evidence or context gap so the workflow can request input "
        "or preserve a verification boundary while the agent continues useful work."
    )

    def __init__(
        self,
        agent_id: str,
        run_id: str,
        task_id: str,
        store: ReportingStore,
        bus: MessageBus,
        workflow_id: str = "",
    ):
        if Path(task_id).name != task_id or not task_id:
            raise ValueError("task_id must be a single safe path component")
        if Path(run_id).name != run_id or not run_id:
            raise ValueError("run_id must be a single safe path component")
        self.agent_id = agent_id
        self.run_id = run_id
        self.task_id = task_id
        self.store = store
        self.bus = bus
        self.workflow_id = workflow_id

    async def __call__(
        self,
        gap: str,
        impact: str,
        requested_input: str = "",
        artifact_refs: list[str] | None = None,
    ) -> dict:
        """Report a non-terminal gap.

        Args:
            gap: Missing customer fact, reference, or decision.
            impact: How the gap limits the current analysis.
            requested_input: Specific material or confirmation that would resolve it.
            artifact_refs: Existing artifacts demonstrating the gap.
        """
        gap_id = f"GAP-{uuid4().hex[:12]}"
        relative = f"Work/runs/{self.run_id}/gaps/{gap_id}.json"
        self.store.write_json(
            relative,
            {
                "id": gap_id,
                "workflow_id": self.workflow_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "gap": gap,
                "impact": impact,
                "requested_input": requested_input,
                "artifact_refs": artifact_refs or [],
            },
        )
        await self.bus.publish(
            ProgressNoteMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                sender=self.agent_id,
                recipient="workflow",
                note_kind="gap",
                artifact_refs=[relative, *(artifact_refs or [])],
                content=f"{gap}\nImpact: {impact}\nRequested input: {requested_input}",
                priority="high",
            )
        )
        return {"status": "reported", "gap_id": gap_id, "artifact_ref": relative}


class RequestRevisionTool(Tool):
    name = "request_revision"
    description = (
        "Send concrete review issue references back to the responsible agent without "
        "rewriting that agent's professional conclusion."
    )

    def __init__(
        self,
        bus: MessageBus,
        workflow_id: str,
        task_id: str,
        agent_id: str,
    ):
        self.bus = bus
        self.workflow_id = workflow_id
        self.task_id = task_id
        self.agent_id = agent_id

    async def __call__(
        self,
        target_agent: str,
        issue_refs: list[str],
        summary: str,
        artifact_refs: list[str] | None = None,
    ) -> dict:
        """Request a targeted revision.

        Args:
            target_agent: Agent responsible for the affected artifact.
            issue_refs: Persisted ReviewIssue identifiers.
            summary: Concise explanation of the requested correction.
            artifact_refs: Affected draft or review artifacts.
        """
        message = RevisionRequestMessage(
            workflow_id=self.workflow_id,
            task_id=self.task_id,
            sender=self.agent_id,
            recipient=target_agent,
            issue_refs=issue_refs,
            artifact_refs=artifact_refs or [],
            content=summary,
            priority="high",
        )
        await self.bus.publish(message)
        return {"status": "requested", "message_id": message.message_id}


class PeerMessageRouter:
    """Deliver typed peer queries to the target AgentLoop's existing input channel."""

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.bus.subscribe(PeerQueryMessage, self._route_query)

    async def _route_query(self, message: PeerQueryMessage) -> None:
        artifacts = "".join(
            f"<artifact_ref>{escape(ref)}</artifact_ref>"
            for ref in message.artifact_refs
        )
        await self.bus.publish(
            UserMessage(
                agent_type=message.recipient,
                source=message.sender,
                message_id=message.query_id,
                summary=message.question,
                content=(
                    "<peer_query>"
                    f"<workflow_id>{escape(message.workflow_id)}</workflow_id>"
                    f"<task_id>{escape(message.task_id)}</task_id>"
                    f"<query_id>{escape(message.query_id)}</query_id>"
                    f"<source_agent>{escape(message.sender)}</source_agent>"
                    f"<source_session_id>{escape(message.source_session_id)}</source_session_id>"
                    f"<question>{escape(message.question)}</question>"
                    f"{artifacts}</peer_query>"
                ),
            )
        )

    def close(self) -> None:
        self.bus.unsubscribe(PeerQueryMessage, self._route_query)
