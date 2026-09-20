"""TaskBoard - central in-memory task store for agent task delegation."""

from datetime import datetime, timezone

from loguru import logger

from ...interfaces.types import AgentId, AgentType, TaskItem, TaskStatus, normalize_agent_id


class TaskBoard:
    """Central store for task items keyed by a shared link id."""

    def __init__(self):
        self._tasks: list[TaskItem] = []
        self._counter: int = 0

    def _encode_counter(self, value: int) -> str:
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        if value <= 0:
            return "0"
        chars: list[str] = []
        current = value
        while current:
            current, remainder = divmod(current, len(alphabet))
            chars.append(alphabet[remainder])
        return "".join(reversed(chars))

    def _next_id(self) -> str:
        self._counter += 1
        return f"tk{self._encode_counter(self._counter).zfill(3)}"

    def create_task(
        self,
        source: AgentId | AgentType,
        target: AgentId | AgentType,
        brief: str,
        blocking: bool = False,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> TaskItem:
        """Create a new task item. task_id may be reused across a routed chain."""
        text = str(brief or "").strip()
        task = TaskItem(
            task_id=task_id or self._next_id(),
            brief=text or "task",
            source_agent=normalize_agent_id(source),
            target_agent=normalize_agent_id(target),
            status=TaskStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            blocking=blocking,
            session_id=session_id,
        )
        self._tasks.append(task)
        logger.debug(
            "TaskBoard: created task {} ({} -> {}, {})",
            task.task_id,
            source,
            target,
            task.brief[:60],
        )
        return task

    def get_task(
        self,
        task_id: str,
        *,
        target_agent: AgentId | AgentType | None = None,
        source_agent: AgentId | AgentType | None = None,
        active_only: bool = False,
        session_id: str | None = None,
    ) -> TaskItem | None:
        target_id = normalize_agent_id(target_agent) if target_agent is not None else None
        source_id = normalize_agent_id(source_agent) if source_agent is not None else None
        for task in self._tasks:
            if task.task_id != task_id:
                continue
            if target_id is not None and task.target_agent != target_id:
                continue
            if source_id is not None and task.source_agent != source_id:
                continue
            if active_only and task.status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                continue
            if session_id is not None and task.session_id != session_id:
                continue
            return task
        return None

    def get_tasks_by_id(self, task_id: str) -> list[TaskItem]:
        return [task for task in self._tasks if task.task_id == task_id]

    def remove_tasks_by_id(
        self,
        task_id: str,
        *,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> list[TaskItem]:
        target_id = normalize_agent_id(target_agent) if target_agent is not None else None
        removed: list[TaskItem] = []
        kept: list[TaskItem] = []
        for task in self._tasks:
            if task.task_id != task_id:
                kept.append(task)
                continue
            if target_id is not None and task.target_agent != target_id:
                kept.append(task)
                continue
            if session_id is not None and task.session_id != session_id:
                kept.append(task)
                continue
            removed.append(task)
        self._tasks = kept
        return removed

    def start_task(
        self,
        task_id: str,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> TaskItem:
        task = self._require_task(
            task_id, target_agent=target_agent, active_only=False, session_id=session_id
        )
        if task.status != TaskStatus.PENDING:
            raise ValueError(f"Task {task_id} is {task.status}, expected {TaskStatus.PENDING}")
        task.status = TaskStatus.IN_PROGRESS
        logger.debug("TaskBoard: started task {}", task_id)
        return task

    def complete_task(
        self,
        task_id: str,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> list[TaskItem]:
        return self._update_chain(
            task_id, TaskStatus.COMPLETED, target_agent=target_agent, session_id=session_id
        )

    def fail_task(
        self,
        task_id: str,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> list[TaskItem]:
        return self._update_chain(
            task_id, TaskStatus.FAILED, target_agent=target_agent, session_id=session_id
        )

    def cancel_task(
        self,
        task_id: str,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> list[TaskItem]:
        return self._update_chain(
            task_id, TaskStatus.CANCELLED, target_agent=target_agent, session_id=session_id
        )

    def block_task(
        self,
        task_id: str,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> list[TaskItem]:
        """Mark a delegated task BLOCKED and propagate up the chain to the dispatcher.

        BLOCKED means the target cannot proceed and needs the dispatcher
        (source agent) to act. Propagation reuses _update_chain so the
        source's waitlist entry is also marked blocked.
        """
        return self._update_chain(
            task_id, TaskStatus.BLOCKED, target_agent=target_agent, session_id=session_id
        )

    def _update_chain(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        target_agent: AgentId | AgentType | None = None,
        session_id: str | None = None,
    ) -> list[TaskItem]:
        task = self._require_task(
            task_id, target_agent=target_agent, active_only=False, session_id=session_id
        )
        if task.status not in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
            raise ValueError(f"Task {task_id} is {task.status}, cannot {new_status.value}")
        affected: list[TaskItem] = []
        self._mark(task, new_status)
        affected.append(task)

        current_target = task.source_agent
        while True:
            upstream = self.get_task(
                task_id,
                target_agent=current_target,
                active_only=True,
                session_id=session_id,
            )
            if upstream is None:
                break
            self._mark(upstream, new_status)
            affected.append(upstream)
            current_target = upstream.source_agent

        logger.debug(
            "TaskBoard: {} task {} (chain: {} affected)", new_status.value, task_id, len(affected)
        )
        return affected

    def _mark(self, task: TaskItem, new_status: TaskStatus) -> None:
        task.status = new_status
        task.completed_at = datetime.now(timezone.utc)

    @staticmethod
    def _agent_label(agent_type: str) -> str:
        return agent_type.replace("_", " ").replace("-", " ").title()

    def _source_followup_view(self, task: TaskItem) -> TaskItem:
        if task.status == TaskStatus.COMPLETED:
            brief = f"Check {self._agent_label(task.target_agent)} completed: {task.brief}"
        elif task.status == TaskStatus.FAILED:
            brief = f"Handle {self._agent_label(task.target_agent)} failure: {task.brief}"
        elif task.status == TaskStatus.CANCELLED:
            brief = f"Handle {self._agent_label(task.target_agent)} cancellation: {task.brief}"
        else:
            brief = task.brief
        return task.model_copy(update={"brief": brief, "status": TaskStatus.PENDING})

    def get_todolist(
        self, agent_type: AgentId | AgentType, session_id: str | None = None
    ) -> list[TaskItem]:
        agent_id = normalize_agent_id(agent_type)
        active_assigned = [
            t
            for t in self._tasks
            if t.target_agent == agent_id
            and t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
            and (session_id is None or t.session_id == session_id)
        ]
        local_resolved = [
            t
            for t in self._tasks
            if t.source_agent == agent_id
            and t.target_agent == agent_id
            and t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            and (session_id is None or t.session_id == session_id)
        ]
        resolved_followups = [
            self._source_followup_view(t)
            for t in self._tasks
            if t.source_agent == agent_id
            and t.target_agent != agent_id
            and t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            and (session_id is None or t.session_id == session_id)
        ]
        seen = set()
        merged: list[TaskItem] = []
        for t in [*active_assigned, *local_resolved, *resolved_followups]:
            key = (t.task_id, t.source_agent, t.target_agent, t.created_at)
            if key in seen:
                continue
            seen.add(key)
            merged.append(t)
        return merged

    def get_waitlist(
        self, agent_type: AgentId | AgentType, session_id: str | None = None
    ) -> list[TaskItem]:
        agent_id = normalize_agent_id(agent_type)
        # Waitlist = tasks this agent delegated to *another* agent. Keep
        # resolved delegated tasks visible as completed/failed/cancelled wait
        # entries so the source agent retains the "what I was waiting on"
        # history while also seeing the resolved follow-up in todolist.
        return [
            t
            for t in self._tasks
            if t.source_agent == agent_id
            and t.target_agent != agent_id
            and t.status
            in (
                TaskStatus.PENDING,
                TaskStatus.IN_PROGRESS,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            )
            and (session_id is None or t.session_id == session_id)
        ]

    def get_blocked_waitlist(
        self, agent_type: AgentId | AgentType, session_id: str | None = None
    ) -> list[TaskItem]:
        """Tasks this agent dispatched that are currently BLOCKED (need its action)."""
        agent_id = normalize_agent_id(agent_type)
        return [
            t
            for t in self._tasks
            if t.source_agent == agent_id
            and t.target_agent != agent_id
            and t.status == TaskStatus.BLOCKED
            and (session_id is None or t.session_id == session_id)
        ]

    def get_all_tasks(self) -> dict[str, dict[str, list[TaskItem]]]:
        agent_ids = sorted(
            {task.source_agent for task in self._tasks}
            | {task.target_agent for task in self._tasks}
        )
        return {
            agent_id: {
                "todolist": self.get_todolist(agent_id),
                "waitlist": self.get_waitlist(agent_id),
            }
            for agent_id in agent_ids
        }

    def get_all(self) -> list[TaskItem]:
        """Return a snapshot of every task record, including terminal history."""

        return list(self._tasks)

    def _require_task(
        self,
        task_id: str,
        *,
        target_agent: AgentId | AgentType | None = None,
        source_agent: AgentId | AgentType | None = None,
        active_only: bool = False,
        session_id: str | None = None,
    ) -> TaskItem:
        task = self.get_task(
            task_id,
            target_agent=target_agent,
            source_agent=source_agent,
            active_only=active_only,
            session_id=session_id,
        )
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        return task
