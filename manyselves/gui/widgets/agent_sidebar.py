"""Live Agent switcher for the main window."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QFrame, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ..scale import scaled
from ..theme import get_theme_colors
from ...utils.agent_labels import get_agent_name


class _AgentRow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(scaled(2), 0, scaled(2), 0)
        layout.setSpacing(0)
        self.name = QLabel(self)
        self.name.setObjectName("agentListName")
        self.status = QLabel(self)
        self.status.setObjectName("agentListStatus")
        layout.addWidget(self.name)
        layout.addWidget(self.status)


class AgentSidebar(QFrame):
    """A compact, runtime-backed list of Agent conversations."""

    agent_selected = pyqtSignal(str)

    _STATUS_LABELS = {
        "idle": "Idle",
        "thinking": "Thinking",
        "running_tool": "Using tool",
        "error": "Error",
        "debug_mode": "Debug",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agentSidebar")
        self.setMinimumWidth(scaled(190))
        self.setMaximumWidth(scaled(280))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(scaled(8), scaled(10), scaled(8), scaled(8))
        layout.setSpacing(scaled(8))

        title = QLabel("AGENTS", self)
        title.setObjectName("agentSidebarTitle")
        layout.addWidget(title)

        self.list = QListWidget(self)
        self.list.setObjectName("agentList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self._on_current_item_changed)
        layout.addWidget(self.list, 1)

        self._items: dict[str, QListWidgetItem] = {}
        self._rows: dict[str, _AgentRow] = {}
        self._status: dict[str, str] = {}
        self._current_task: dict[str, str] = {}

    def ensure_agent(self, agent_id: str, status: str = "idle") -> None:
        agent_id = str(agent_id).strip()
        if not agent_id:
            return
        item = self._items.get(agent_id)
        if item is None:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, agent_id)
            item.setSizeHint(QSize(scaled(120), scaled(46)))
            self._items[agent_id] = item
            if agent_id == "main":
                self.list.insertItem(0, item)
            else:
                self.list.addItem(item)
            row = _AgentRow(self.list)
            self._rows[agent_id] = row
            self.list.setItemWidget(item, row)
        self.set_agent_status(agent_id, status)

    def set_agent_status(self, agent_id: str, status: str) -> None:
        self.ensure_agent(agent_id) if agent_id not in self._items else None
        item = self._items.get(agent_id)
        if item is None:
            return
        status = str(status or "idle")
        self._status[agent_id] = status
        if status in {"idle", "error"}:
            self._current_task.pop(agent_id, None)
        label = self._STATUS_LABELS.get(status, status.replace("_", " ").title())
        display_name = get_agent_name(agent_id)
        name, separator, instance = display_name.partition(" · ")
        status_text = f"{label} · {instance}" if separator else label
        current_task = self._current_task.get(agent_id, "")
        if current_task:
            compact = " ".join(current_task.split())
            if len(compact) > 42:
                compact = compact[:39].rstrip() + "..."
            status_text = f"{status_text} · {compact}"
        item.setText("")
        item.setData(
            Qt.ItemDataRole.AccessibleTextRole,
            f"{name}, {status_text}",
        )
        item.setToolTip(agent_id)

        colors = get_theme_colors()
        color_key = {
            "thinking": "status_running",
            "running_tool": "status_tool",
            "error": "status_error",
            "debug_mode": "status_debug",
        }.get(status, "fg")
        item.setForeground(QColor(colors[color_key]))
        row = self._rows[agent_id]
        row.name.setText(name)
        row.name.setStyleSheet(f"color: {colors['fg']}; font-weight: 500;")
        row.status.setText(status_text)
        row.status.setStyleSheet(f"color: {colors[color_key]}; font-size: {scaled(10)}px;")

    def set_agent_task(self, agent_id: str, brief: str | None) -> None:
        """Show the Agent's current task beside its live status."""
        self.ensure_agent(agent_id, self._status.get(agent_id, "idle"))
        text = str(brief or "").strip()
        if text:
            self._current_task[agent_id] = text
            if self._status.get(agent_id, "idle") == "idle":
                self._status[agent_id] = "thinking"
        else:
            self._current_task.pop(agent_id, None)
        self.set_agent_status(agent_id, self._status.get(agent_id, "idle"))

    def select_agent(self, agent_id: str) -> None:
        self.ensure_agent(agent_id, self._status.get(agent_id, "idle"))
        item = self._items.get(agent_id)
        if item is not None and self.list.currentItem() is not item:
            self.list.setCurrentItem(item)

    def agent_ids(self) -> list[str]:
        return [
            str(self.list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.list.count())
        ]

    def retain_agents(self, agent_ids: set[str]) -> None:
        """Keep only the requested rows without deleting persisted history."""
        keep = {str(agent_id) for agent_id in agent_ids}
        for agent_id in list(self._items):
            if agent_id in keep:
                continue
            item = self._items.pop(agent_id)
            row = self.list.row(item)
            if row >= 0:
                self.list.takeItem(row)
            self._rows.pop(agent_id, None)
            self._status.pop(agent_id, None)
            self._current_task.pop(agent_id, None)

    def _on_current_item_changed(self, current, _previous) -> None:
        if current is None:
            return
        agent_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        if agent_id:
            self.agent_selected.emit(agent_id)
