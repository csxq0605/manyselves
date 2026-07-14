from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class TaskBoardError(ValueError):
    pass


class TaskNotFoundError(TaskBoardError):
    pass


class InvalidTaskTransitionError(TaskBoardError):
    pass


@dataclass(slots=True)
class Task:
    id: str
    agent_id: str
    needs: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    error: str | None = None
    blocked_reason: str | None = None
    revision: int = 0
    result_keys: list[str] = field(default_factory=list)


class TaskBoard:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def create(
        self,
        task_id: str,
        agent_id: str,
        *,
        needs: list[str] | None = None,
        revision: int = 0,
    ) -> Task:
        if task_id in self._tasks:
            raise TaskBoardError(f"duplicate task id: {task_id}")
        dependencies = list(needs or [])
        unknown = set(dependencies) - set(self._tasks)
        if unknown:
            raise TaskBoardError(f"unknown task dependencies: {', '.join(sorted(unknown))}")
        task = Task(task_id, agent_id, dependencies, revision=revision)
        self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(f"task not found: {task_id}") from exc

    def start(self, task_id: str) -> Task:
        task = self.get(task_id)
        self._require_status(task, TaskStatus.PENDING)
        incomplete = [
            dependency
            for dependency in task.needs
            if self.get(dependency).status is not TaskStatus.COMPLETED
        ]
        if incomplete:
            raise InvalidTaskTransitionError(
                f"task {task.id} dependencies are not completed: {', '.join(incomplete)}"
            )
        task.status = TaskStatus.RUNNING
        return task

    def complete(self, task_id: str, *, result_keys: list[str] | None = None) -> Task:
        task = self.get(task_id)
        self._require_status(task, TaskStatus.RUNNING)
        task.status = TaskStatus.COMPLETED
        task.result_keys = list(result_keys or [])
        return task

    def fail(self, task_id: str, error: str) -> Task:
        task = self.get(task_id)
        if task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            raise InvalidTaskTransitionError(
                f"task {task.id} is {task.status.value}, expected pending or running"
            )
        task.status = TaskStatus.FAILED
        task.error = error
        return task

    def block(self, task_id: str, reason: str) -> Task:
        task = self.get(task_id)
        if task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            raise InvalidTaskTransitionError(
                f"task {task.id} is {task.status.value}, expected pending or running"
            )
        task.status = TaskStatus.BLOCKED
        task.blocked_reason = reason
        return task

    def skip_dependents(self, task_id: str) -> list[str]:
        self.get(task_id)
        skipped: list[str] = []
        frontier = [task_id]
        while frontier:
            dependency = frontier.pop(0)
            for task in self._tasks.values():
                if dependency not in task.needs or task.status is not TaskStatus.PENDING:
                    continue
                task.status = TaskStatus.SKIPPED
                task.error = f"dependency {dependency} did not complete"
                skipped.append(task.id)
                frontier.append(task.id)
        return skipped

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "id": task.id,
                "agent_id": task.agent_id,
                "needs": list(task.needs),
                "status": task.status.value,
                "error": task.error,
                "blocked_reason": task.blocked_reason,
                "revision": task.revision,
                "result_keys": list(task.result_keys),
            }
            for task in self._tasks.values()
        ]

    def _require_status(self, task: Task, status: TaskStatus) -> None:
        if task.status is not status:
            raise InvalidTaskTransitionError(
                f"task {task.id} is {task.status.value}, expected {status.value}"
            )
