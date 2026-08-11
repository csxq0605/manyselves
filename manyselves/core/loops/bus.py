"""Message bus for async communication between components."""

import asyncio
import inspect
import threading
from collections import Counter, OrderedDict
from typing import Awaitable, Callable, TypeVar

from loguru import logger

from ...interfaces.types import AgentResponse, AgentResultMessage, Error, Message

MessageT = TypeVar("MessageT", bound=Message)


class MessageBus:
    """Async message bus for pub/sub communication."""

    def __init__(self, *, stream_capacity: int = 512) -> None:
        """Initialize message bus."""
        if stream_capacity < 1:
            raise ValueError("stream_capacity must be positive")
        self._subscribers: dict[type[Message], list[Callable]] = {}
        self._subscribers_lock = threading.Lock()
        # Control, terminal, and domain messages retain the legacy durable-in-
        # process FIFO. Transient Provider deltas use a separate bounded plane
        # so a slow UI subscriber cannot starve typed results or errors.
        self._queue: asyncio.Queue[Message] = asyncio.Queue()
        self._stream_capacity = stream_capacity
        self._stream_pending: OrderedDict[tuple[str, ...], AgentResponse] = (
            OrderedDict()
        )
        self._stream_event = asyncio.Event()
        self._stream_fences: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._stream_fence_capacity = max(1_024, stream_capacity * 8)
        self._stream_metrics: Counter[str] = Counter()
        self._shutdown = False
        self._published_counts: Counter[str] = Counter()

    async def publish(self, message: Message) -> None:
        """Publish a message to all subscribers.

        Args:
            message: Message to publish.
        """
        message_type = str(message.type)
        self._published_counts[message_type] += 1
        if self._is_transient_stream(message):
            self._publish_stream(message)
        else:
            terminal_prefix = self._terminal_prefix(message)
            if terminal_prefix is not None:
                self._stream_fences.pop(terminal_prefix, None)
                self._stream_fences[terminal_prefix] = None
                if len(self._stream_fences) > self._stream_fence_capacity:
                    self._stream_fences.popitem(last=False)
                    self._stream_metrics["fence_evicted"] += 1
                stale_keys = [
                    key
                    for key in self._stream_pending
                    if key[:4] == terminal_prefix
                ]
                for key in stale_keys:
                    self._stream_pending.pop(key, None)
                    self._stream_metrics["terminal_discarded_pending"] += 1
            await self._queue.put(message)
        # Provider streams can legitimately publish tens of thousands of
        # AgentResponse deltas during a long report. Per-message diagnostics are
        # TRACE-level; DEBUG retains one aggregate summary at shutdown.
        logger.trace("Published message: {}", message.type)

    async def process_queue(self) -> None:
        """Process messages from queue and notify subscribers."""
        stream_worker = asyncio.create_task(self._process_stream_plane())
        try:
            while not self._shutdown or not self._queue.empty():
                try:
                    message = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await self._notify_subscribers(message)
        finally:
            self._stream_event.set()
            await stream_worker

    @staticmethod
    def _is_transient_stream(message: Message) -> bool:
        return isinstance(message, AgentResponse) and message.streaming

    @staticmethod
    def _stream_key(message: AgentResponse) -> tuple[str, ...]:
        return (
            message.workflow_id,
            message.run_id,
            message.task_id,
            message.task_attempt_id,
            message.agent_type,
            message.session_id,
            str(message.message_id or ""),
        )

    @staticmethod
    def _terminal_prefix(message: Message) -> tuple[str, ...] | None:
        is_terminal = (
            isinstance(message, AgentResultMessage)
            or isinstance(message, Error)
            or isinstance(message, AgentResponse) and not message.streaming
        )
        if not is_terminal:
            return None
        task_attempt_id = str(getattr(message, "task_attempt_id", "") or "")
        if not task_attempt_id:
            return None
        return (
            str(getattr(message, "workflow_id", "") or ""),
            str(getattr(message, "run_id", "") or ""),
            str(getattr(message, "task_id", "") or ""),
            task_attempt_id,
        )

    def _publish_stream(self, message: AgentResponse) -> None:
        key = self._stream_key(message)
        if key[:4] in self._stream_fences:
            self._stream_metrics["after_terminal_dropped"] += 1
            return
        existing = self._stream_pending.pop(key, None)
        if existing is not None:
            # Delta messages are additive. Coalesce them into one bounded UI
            # update instead of retaining one queue object per token.
            content = (existing.content or "") + (message.content or "")
            thinking = (existing.thinking or "") + (message.thinking or "")
            maximum_chars = 262_144
            if len(content) > maximum_chars:
                content = content[-maximum_chars:]
                self._stream_metrics["truncated_content"] += 1
            if len(thinking) > maximum_chars:
                thinking = thinking[-maximum_chars:]
                self._stream_metrics["truncated_thinking"] += 1
            message = message.model_copy(
                update={"content": content, "thinking": thinking or None}
            )
            self._stream_metrics["coalesced"] += 1
        elif len(self._stream_pending) >= self._stream_capacity:
            self._stream_pending.popitem(last=False)
            self._stream_metrics["capacity_dropped"] += 1
        self._stream_pending[key] = message
        self._stream_metrics["high_water"] = max(
            self._stream_metrics["high_water"], len(self._stream_pending)
        )
        self._stream_event.set()

    async def _process_stream_plane(self) -> None:
        while not self._shutdown or self._stream_pending:
            if not self._stream_pending:
                self._stream_event.clear()
                try:
                    await asyncio.wait_for(self._stream_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
            if not self._stream_pending:
                continue
            _key, message = self._stream_pending.popitem(last=False)
            await self._notify_subscribers(message)
            self._stream_metrics["dispatched"] += 1

    @property
    def stream_metrics(self) -> dict[str, int]:
        return dict(self._stream_metrics)

    async def _notify_subscribers(self, message: Message) -> None:
        """Notify all subscribers for a message type."""
        message_type = type(message)
        callbacks = []

        with self._subscribers_lock:
            callbacks.extend(self._subscribers.get(message_type, []))

            for msg_type, subs in self._subscribers.items():
                if msg_type != message_type and isinstance(message, msg_type):
                    callbacks.extend(subs)

        for callback in callbacks:
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as e:
                logger.error("Error in subscriber callback: {}", e)

    def subscribe(
        self,
        message_type: type[Message],
        callback: Callable[[Message], Awaitable[None] | None]
    ) -> None:
        """Subscribe to a message type.

        Thread-safe: can be called from any thread (e.g. during startup
        before the async loop starts processing).
        """
        with self._subscribers_lock:
            if message_type not in self._subscribers:
                self._subscribers[message_type] = []
            self._subscribers[message_type].append(callback)
        logger.debug("Subscribed to message type: {}", message_type.__name__)

    async def wait_for(
        self,
        message_type: type[MessageT],
        predicate: Callable[[MessageT], bool],
        timeout: float | None,
    ) -> MessageT:
        """Wait once for a matching processed message, then always unsubscribe."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[MessageT] = loop.create_future()

        def callback(message: MessageT) -> None:
            if future.done():
                return
            try:
                matches = predicate(message)
            except Exception as exc:
                future.set_exception(exc)
                return
            if matches:
                future.set_result(message)

        self.subscribe(message_type, callback)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.unsubscribe(message_type, callback)

    def unsubscribe(
        self,
        message_type: type[Message],
        callback: Callable[[Message], Awaitable[None] | None]
    ) -> None:
        """Unsubscribe from a message type.

        Thread-safe: safe to call while the bus is processing messages.
        """
        with self._subscribers_lock:
            if message_type in self._subscribers:
                try:
                    self._subscribers[message_type].remove(callback)
                    logger.debug("Unsubscribed from message type: {}", message_type.__name__)
                except ValueError:
                    pass

    def shutdown(self) -> None:
        """Signal the processing loop to exit."""
        if self._shutdown:
            return
        self._shutdown = True
        if self._published_counts:
            logger.debug(
                "Message bus shutdown; published counts: {}",
                dict(sorted(self._published_counts.items())),
            )
