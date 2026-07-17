"""Interface type definitions for GUI-backend communication."""

from datetime import datetime
from enum import Enum
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class MessageType(str, Enum):
    """Message types between GUI and backend."""

    # GUI → Backend
    USER_MESSAGE = "user_message"
    CONFIG_CHANGE = "config_change"
    PROJECT_SELECT = "project_select"
    RESTART_REQUEST = "restart_request"
    FILE_ROLLBACK_REQUEST = "file_rollback_request"

    # Backend → GUI
    AGENT_RESPONSE = "agent_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATUS_CHANGE = "status_change"
    ERROR = "error"
    CHECKPOINT = "checkpoint"
    ROLLBACK_STATUS = "rollback_status"
    API_DEBUG = "api_debug"  # API call debugging information
    TASK_UPDATE = "task_update"
    QUEUE_UPDATE = "queue_update"
    REPORT = "report"
    SYSTEM_NOTICE = "system_notice"
    PEER_QUERY = "peer_query"
    PEER_REPLY = "peer_reply"
    PROGRESS_NOTE = "progress_note"
    RESEARCH_NOTE_PUBLISHED = "research_note_published"
    REVISION_REQUEST = "revision_request"
    BLOCKED_NOTICE = "blocked_notice"
    AGENT_RESULT = "agent_result"


class AgentType(str, Enum):
    """Agent identifiers accepted at compatibility boundaries.

    Only ``MAIN`` has a persistent GUI/runtime loop. The remaining values are
    retained so stored task/message records from older projects still decode.
    """

    MAIN = "main"
    DATA_ANALYSIS = "data_analysis"
    PLOTTING = "plotting"
    THEORY = "theory"
    REPORT = "report"


AgentId: TypeAlias = str


def normalize_agent_id(value: str | AgentType) -> str:
    """Return the string identifier used by the runtime.

    ``AgentType`` remains accepted at compatibility boundaries while registry
    agent identifiers flow through unchanged.
    """
    return value.value if isinstance(value, AgentType) else str(value)


class AgentStatus(str, Enum):
    """Agent status."""

    IDLE = "idle"
    THINKING = "thinking"
    RUNNING_TOOL = "running_tool"
    ERROR = "error"
    DEBUG_MODE = "debug_mode"


class TaskStatus(str, Enum):
    """Task lifecycle status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"  # sub-agent cannot proceed; needs dispatcher action
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Message(BaseModel):
    """Base message for GUI-backend communication."""

    type: MessageType
    timestamp: datetime = Field(default_factory=datetime.now)

    model_config = ConfigDict(use_enum_values=True)


class UserMessage(Message):
    """User message to agent.

    source distinguishes direct user input ("user") from
    main-agent coordination commands ("main_agent").  Debug
    mode filters out the latter.
    """

    type: MessageType = MessageType.USER_MESSAGE
    content: str
    # Short visible summary for inter-agent coordination. Tools enforce this
    # for new coordination messages; default keeps older stored messages valid.
    summary: str = ""
    agent_type: AgentId = AgentType.MAIN.value
    message_id: str | None = None
    source: str = "user"  # "user" | "system" | "main_agent" | "<agent_type>"


class AgentResponse(Message):
    """Agent response to user."""

    type: MessageType = MessageType.AGENT_RESPONSE
    agent_type: AgentId
    content: str
    message_id: str | None = None
    streaming: bool = False  # True for stream chunks, False for final completion
    thinking: str | None = None


class ToolCallMessage(Message):
    """Tool being executed by agent."""

    type: MessageType = MessageType.TOOL_CALL
    agent_type: AgentId
    tool_name: str
    arguments: dict[str, Any]


class ToolResult(Message):
    """Result of tool execution."""

    type: MessageType = MessageType.TOOL_RESULT
    agent_type: AgentId
    tool_name: str
    result: Any
    error: str | None = None


class StatusChange(Message):
    """Agent status change."""

    type: MessageType = MessageType.STATUS_CHANGE
    agent_type: AgentId
    status: AgentStatus
    extra: dict[str, Any] = Field(default_factory=dict)


class ConfigChange(Message):
    """Configuration change notification."""

    type: MessageType = MessageType.CONFIG_CHANGE
    config_type: str  # "provider", "model", etc.
    old_value: Any
    new_value: Any


class RestartRequest(Message):
    """Request to restart agent system."""

    type: MessageType = MessageType.RESTART_REQUEST
    reason: str  # "config_change", "user_request"


class Checkpoint(Message):
    """Checkpoint created for rollback — per-agent."""

    type: MessageType = MessageType.CHECKPOINT
    agent_type: AgentId
    checkpoint_id: str
    description: str
    # Deprecated: kept for backward compatibility. The operation-log checkpoint
    # model no longer publishes per-file hashes (the GUI doesn't consume them).
    file_states: dict[str, str] = Field(default_factory=dict)
    message_id: str | None = None  # The message that triggered this checkpoint


class FileRollbackRequest(Message):
    """GUI request to rollback files to a specific checkpoint.

    Sent when the user right-clicks a message in the chat and selects
    "Rollback files to this point".
    """

    type: MessageType = MessageType.FILE_ROLLBACK_REQUEST
    checkpoint_id: str
    agent_type: AgentId
    message_id: str | None = None  # The message that triggered this rollback


class RollbackStatus(Message):
    """Backend → GUI: result of a file rollback operation."""

    type: MessageType = MessageType.ROLLBACK_STATUS
    checkpoint_id: str
    agent_type: str
    success: bool
    restored_files: int = 0
    error: str | None = None


class Error(Message):
    """Error message."""

    type: MessageType = MessageType.ERROR
    source: str  # "agent", "tool", "system"
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class TaskItem(BaseModel):
    """Single task record — dual view: waitlist for source_agent, todolist for target_agent."""

    task_id: str
    brief: str
    source_agent: AgentId
    target_agent: AgentId
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    blocking: bool = False
    session_id: str | None = None


class TaskUpdateMessage(Message):
    """Task lifecycle notification routed through the message bus."""

    type: MessageType = MessageType.TASK_UPDATE
    task_id: str
    action: str  # "created" | "started" | "completed" | "failed" | "cancelled"
    source_agent: AgentId
    target_agent: AgentId
    brief: str = ""
    previous_status: str | None = None


class QueueUpdateMessage(Message):
    """Queued follow-up messages waiting for the next agent turn."""

    type: MessageType = MessageType.QUEUE_UPDATE
    agent_type: AgentId
    queued_messages: list[str] = Field(default_factory=list)


class ApiDebugMessage(Message):
    """API debug information for monitoring LLM calls.

    Published by AgentLoop when making LLM API calls, allowing
    GUI components to display timing, token usage, and error information.
    """

    type: MessageType = MessageType.API_DEBUG
    timestamp: datetime = Field(default_factory=datetime.now)
    model: str  # Model name (e.g., "claude-sonnet-4-20250514")
    tokens_in: int  # Input tokens
    tokens_out: int  # Output tokens
    duration_ms: int  # Duration in milliseconds
    status: str  # "success" or "error"
    error: str | None = None  # Error message if status is "error"


class ReportMessage(Message):
    """Task-scoped reporting role result published to the GUI timeline."""

    type: MessageType = MessageType.REPORT
    agent_type: AgentId
    task_id: str
    report_type: str  # "reply" | "missing_data" | "quality"
    # Short visible summary shown in coordination bubbles.
    summary: str = ""
    content: str = ""


class SystemNotice(Message):
    """Backend -> GUI: a notice explaining why an agent is waiting/busy.

    Rendered as a bubble so the user understands agent activity that is
    NOT a normal LLM turn (loop guards, manifest wrap-up, etc.). The
    AgentLoop does NOT subscribe to this type, so publishing it never
    triggers an extra agent turn.
    """

    type: MessageType = MessageType.SYSTEM_NOTICE
    agent_type: AgentId
    content: str
    # "notice" (default) renders as a normal system bubble.
    # "interrupt" renders as a muted italic "Interrupted" marker — emitted when
    # the user stops a running response so the UI avoids a fake "[已取消]" bubble.
    kind: str = "notice"


class WorkflowMessage(Message):
    """Common routing metadata for persisted reporting-workflow messages."""

    message_id: str = Field(default_factory=lambda: f"msg-{uuid4().hex}")
    workflow_id: str = ""
    task_id: str
    sender: AgentId = Field(
        validation_alias=AliasChoices("sender", "agent_type", "source_agent")
    )
    recipient: AgentId = Field(
        default="workflow",
        validation_alias=AliasChoices("recipient", "target_agent"),
    )
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    requires_reply: bool = False
    artifact_refs: list[str] = Field(default_factory=list)
    content: str = ""


class PeerQueryMessage(WorkflowMessage):
    """A targeted question; long evidence remains in referenced artifacts."""

    type: MessageType = MessageType.PEER_QUERY
    query_id: str
    source_session_id: str
    target_session_id: str | None = None
    question: str
    requires_reply: bool = True

    @property
    def source_agent(self) -> AgentId:
        return self.sender

    @property
    def target_agent(self) -> AgentId:
        return self.recipient


class PeerReplyMessage(WorkflowMessage):
    """A reply isolated to the original query and requesting session."""

    type: MessageType = MessageType.PEER_REPLY
    query_id: str
    target_session_id: str
    answer: str
    source_ids: list[str] = Field(default_factory=list)

    @property
    def source_agent(self) -> AgentId:
        return self.sender

    @property
    def target_agent(self) -> AgentId:
        return self.recipient


class ProgressNoteMessage(WorkflowMessage):
    type: MessageType = MessageType.PROGRESS_NOTE
    note_kind: Literal["progress", "gap"] = "progress"


class ResearchNotePublishedMessage(WorkflowMessage):
    type: MessageType = MessageType.RESEARCH_NOTE_PUBLISHED
    note_id: str


class RevisionRequestMessage(WorkflowMessage):
    type: MessageType = MessageType.REVISION_REQUEST
    issue_refs: list[str] = Field(default_factory=list)
    requires_reply: bool = True


class BlockedNoticeMessage(WorkflowMessage):
    type: MessageType = MessageType.BLOCKED_NOTICE
    reason: str


class AgentResultMessage(WorkflowMessage):
    type: MessageType = MessageType.AGENT_RESULT
    run_id: str
    result_path: str
    status: Literal["completed", "blocked", "incomplete", "failed"] = "completed"

    @property
    def agent_type(self) -> AgentId:
        return self.sender
