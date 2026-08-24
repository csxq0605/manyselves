"""One lifecycle-owned router and message index per reporting workflow."""

from __future__ import annotations

import re
from html import escape

from manyselves.interfaces.types import (
    BlockedNoticeMessage,
    PeerQueryMessage,
    PeerReplyMessage,
    ProgressNoteMessage,
    ResearchNotePublishedMessage,
    UserMessage,
)
from manyselves.runtime.loops.bus import MessageBus

_SOURCE_RECORD_ID = re.compile(r"^[ERW]-[A-Za-z0-9][A-Za-z0-9._:-]*$")


def is_source_record_id(ref: str) -> bool:
    """Return whether a reference names ledger evidence rather than an artifact."""

    return bool(_SOURCE_RECORD_ID.fullmatch(str(ref).strip()))


def artifact_path_refs(refs: list[str]) -> list[str]:
    """Keep path-like references and reject bare E/R/W ledger identifiers."""

    return [ref for ref in refs if ref and not is_source_record_id(ref)]


def source_record_ids(refs: list[str]) -> list[str]:
    """Extract bare E/R/W ledger identifiers from a mixed reference list."""

    return [ref for ref in refs if ref and is_source_record_id(ref)]


class WorkflowMessageRouter:
    def __init__(self, bus: MessageBus, workflow_id: str):
        self.bus = bus
        self.workflow_id = workflow_id
        self._sessions: dict[tuple[str, str], str] = {}
        self.research_notes: list[str] = []
        self.gaps: list[str] = []
        self.blocked_notices: list[str] = []
        self._subscriptions = (
            (PeerQueryMessage, self._route_query),
            (ResearchNotePublishedMessage, self._consume_research),
            (ProgressNoteMessage, self._consume_progress),
            (BlockedNoticeMessage, self._consume_blocked),
        )
        for message_type, callback in self._subscriptions:
            self.bus.subscribe(message_type, callback)

    def register_session(self, agent_id: str, session_id: str, runtime_id: str) -> None:
        self._sessions[(agent_id, session_id)] = runtime_id

    def unregister_session(self, agent_id: str, session_id: str) -> None:
        self._sessions.pop((agent_id, session_id), None)

    async def _route_query(self, message: PeerQueryMessage) -> None:
        if message.workflow_id != self.workflow_id:
            return
        candidates = [
            (session_id, runtime_id)
            for (agent_id, session_id), runtime_id in self._sessions.items()
            if agent_id == str(message.recipient)
            and (message.target_session_id is None or session_id == message.target_session_id)
        ]
        if len(candidates) != 1:
            reason = "not available" if not candidates else "ambiguous; specify target_session_id"
            await self.bus.publish(
                PeerReplyMessage(
                    workflow_id=self.workflow_id,
                    task_id=message.task_id,
                    query_id=message.query_id,
                    sender="workflow",
                    recipient=message.sender,
                    target_session_id=message.source_session_id,
                    answer=f"Peer '{message.recipient}' is {reason} in this workflow.",
                    content="peer unavailable",
                )
            )
            return
        target_session_id, runtime_id = candidates[0]
        artifacts = "".join(f"<artifact_ref>{escape(ref)}</artifact_ref>" for ref in message.artifact_refs)
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
                    f"<target_session_id>{escape(target_session_id)}</target_session_id>"
                    f"<question>{escape(message.question)}</question>{artifacts}</peer_query>"
                ),
            )
        )

    def _consume_research(self, message: ResearchNotePublishedMessage) -> None:
        if message.workflow_id == self.workflow_id:
            self.research_notes.extend(
                ref
                for ref in artifact_path_refs(message.artifact_refs)
                if ref not in self.research_notes
            )

    def _consume_progress(self, message: ProgressNoteMessage) -> None:
        if message.workflow_id == self.workflow_id and message.note_kind == "gap":
            self.gaps.extend(
                ref
                for ref in artifact_path_refs(message.artifact_refs)
                if ref not in self.gaps
            )

    def _consume_blocked(self, message: BlockedNoticeMessage) -> None:
        if message.workflow_id == self.workflow_id:
            self.blocked_notices.extend(
                ref
                for ref in artifact_path_refs(message.artifact_refs)
                if ref not in self.blocked_notices
            )

    def close(self) -> None:
        for message_type, callback in self._subscriptions:
            self.bus.unsubscribe(message_type, callback)
        self._sessions.clear()
