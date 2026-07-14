from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pds_report.workflow.bus import MessageBus
from pds_report.workflow.tasks import TaskBoard


@dataclass(slots=True)
class AgentContext:
    project_root: Path
    run_id: str
    state: dict[str, object]
    bus: MessageBus
    task_board: TaskBoard

    def with_state(self, state: dict[str, object]) -> AgentContext:
        return AgentContext(
            project_root=self.project_root,
            run_id=self.run_id,
            state=state,
            bus=self.bus,
            task_board=self.task_board,
        )


class AgentPort(Protocol):
    async def run(self, context: AgentContext) -> Mapping[str, object]: ...

