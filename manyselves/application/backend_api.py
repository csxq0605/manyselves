"""Desktop backend adapter implementing the frontend-facing API."""

import asyncio
from typing import Any, Dict

from loguru import logger

from ..config import ConfigManager
from ..core.loops import LoopManager, MessageBus
from ..interfaces.protocol import BackendAPI
from ..utils.editor_context import build_editor_context_prompt


class BackendAPIImpl(BackendAPI):
    """Backend API implementation."""

    def __init__(
        self,
        config_manager: ConfigManager,
        bus: MessageBus,
    ):
        """Initialize backend API.

        Args:
            config_manager: Configuration manager.
            bus: Message bus.
        """
        self.config_manager = config_manager
        self.bus = bus
        self.loop_manager: LoopManager | None = None

    def set_loop_manager(self, loop_manager: LoopManager | None) -> None:
        """Set the loop manager (called after it's created).

        Args:
            loop_manager: Loop manager instance, or ``None`` when the host is failed/stopped.
        """
        self.loop_manager = loop_manager

    async def send_user_message(
        self,
        content: str,
        agent_type: str,
        message_id: str | None = None,
        source: str = "user",
    ) -> None:
        """Send a user message to an agent.

        Args:
            content: Message content.
            agent_type: Target agent type.
            message_id: Optional message ID for tracking.
            source: "user" for direct input, "main_agent" for coordination.
        """
        from ..interfaces.types import UserMessage, normalize_agent_id

        agent_id = "main" if agent_type == "sub" else normalize_agent_id(agent_type)

        message = UserMessage(
            content=content,
            agent_type=agent_id,
            message_id=message_id,
            source=source,
        )
        await self.bus.publish(message)

    async def send_file_context(
        self,
        file_context: dict,
        agent_type: str,
    ) -> None:
        """Send file context to an agent as system message (invisible to user)."""
        from ..interfaces.types import UserMessage, normalize_agent_id

        agent_id = "main" if agent_type == "sub" else normalize_agent_id(agent_type)

        # Format file context as system message.
        # Keep context strictly scoped to the attachment shown in agent composer.
        if file_context.get("type") == "selection":
            context_msg = build_editor_context_prompt(
                {
                    "type": "selection",
                    "file": file_context.get("file", ""),
                    "selected_lines": f"{file_context.get('start_line', '')}-{file_context.get('end_line', '')}",
                },
                "",
            )
            context_msg = (
                f"{context_msg}\n"
                "Constraint: selected text is not attached; do not infer or list other open tabs.\n"
            ).strip()
        elif file_context.get("type") == "file":
            context_msg = build_editor_context_prompt(
                {
                    "type": "file",
                    "file": file_context.get("file", ""),
                },
                "",
            )
            context_msg = (
                f"{context_msg}\n"
                "Constraint: do not infer or list other open tabs.\n"
            ).strip()
        else:
            return

        # Send as system message (source="system")
        message = UserMessage(
            content=context_msg,
            agent_type=agent_id,
            source="system",
        )
        await self.bus.publish(message)

    async def interrupt_current_message(self, agent_type: str) -> None:
        """Interrupt the currently processing message for an agent."""
        if self.loop_manager is None:
            logger.warning("Loop manager not initialized, cannot interrupt")
            return
        self.loop_manager.cancel_current_operation(agent_type)

    async def restart_agents(self, reason: str) -> None:
        """Restart the agent system."""
        from ..interfaces.types import RestartRequest
        message = RestartRequest(reason=reason)
        await self.bus.publish(message)

    async def switch_provider(self, provider: str) -> None:
        """Switch to a different provider."""
        # Update config
        self.config_manager.config.agents.defaults.provider = provider
        # Restart agents
        await self.restart_agents(reason="config_change")

    async def switch_model(self, model: str) -> None:
        """Switch to a different model."""
        # Update config
        self.config_manager.config.agents.defaults.model = model
        # No restart needed for model change

    async def sync_agent_conversation(
        self,
        agent_type: str,
        messages: list[dict[str, str]] | None = None,
        session_id: str | None = None,
        clear_pending: bool = False,
    ) -> None:
        """Replace in-memory conversation history for an agent loop."""
        if self.loop_manager is None:
            return

        from ..core.providers.base import Message as LLMMessage
        loop = self.loop_manager.get_loop(agent_type)
        if loop is None:
            return

        loop._current_session_id = session_id  # noqa: SLF001
        if clear_pending:
            loop.cancel_current()  # noqa: SLF001
            while not loop._message_queue.empty():  # noqa: SLF001
                try:
                    loop._message_queue.get_nowait()  # noqa: SLF001
                except asyncio.QueueEmpty:
                    break
            await loop._publish_queue_update()  # noqa: SLF001

        loop._conversation_history.clear()  # noqa: SLF001
        for msg in messages or []:
            role = str(msg.get("role", "")).strip()
            content = str(msg.get("content", ""))
            is_tool_result = bool(msg.get("is_tool_result", False))
            if role not in {"user", "assistant", "system"}:
                continue
            loop._conversation_history.append(LLMMessage(role=role, content=content, is_tool_result=is_tool_result))  # noqa: SLF101

    async def rollback_to_checkpoint(self, agent_type: str, checkpoint_id: str) -> Dict[str, Any]:
        """Rollback an agent to a specific checkpoint.

        Returns:
            Dictionary with restored_files count and conversation_history.
        """
        if self.loop_manager is None:
            raise RuntimeError("Loop manager not initialized")

        result = await self.loop_manager.rollback_to_checkpoint(
            agent_type, checkpoint_id, restore_conversation=True
        )
        return result

    async def prepare_rollback(self, agent_type: str, checkpoint_id: str) -> Dict[str, Any]:
        """Validate the checkpoint and its recorded effects before committing."""
        if self.loop_manager is None:
            raise RuntimeError("Loop manager not initialized")
        manager = getattr(self.loop_manager, "checkpoint_manager", None)
        if manager is None:
            return {"checkpoint_id": checkpoint_id, "effect_paths": []}
        target = manager.get_checkpoint(agent_type, checkpoint_id)
        if target is None:
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")

        workspace = manager.workspace.resolve()
        effect_paths: list[str] = []
        for checkpoint in manager.list_checkpoints(agent_type):
            if checkpoint.epoch < target.epoch:
                continue
            for operation in checkpoint.operations:
                if operation.kind not in {"add", "modify", "delete"}:
                    raise ValueError(f"Unsupported checkpoint operation: {operation.kind}")
                candidate = (workspace / operation.path).resolve(strict=False)
                if not candidate.is_relative_to(workspace):
                    raise ValueError("Checkpoint effect escapes the active workspace")
                if operation.kind == "modify" and operation.before is None:
                    raise ValueError("Checkpoint modify effect has no prior content")
                if operation.kind == "delete" and (
                    operation.before is None and operation.before_binary_b64 is None
                ):
                    raise ValueError("Checkpoint delete effect has no prior content")
                effect_paths.append(str(candidate.relative_to(workspace)))
        return {"checkpoint_id": checkpoint_id, "effect_paths": effect_paths}

    def set_agent_debug_mode(self, agent_type: str, enabled: bool) -> None:
        """Enable or disable debug mode for an agent."""
        if self.loop_manager is None:
            raise RuntimeError("Loop manager not initialized")

        self.loop_manager.set_agent_debug_mode(agent_type, enabled)

    def subscribe_to_messages(
        self,
        callback
    ) -> None:
        """Subscribe to all backend messages."""
        from ..interfaces.types import Message
        self.bus.subscribe(Message, callback)
