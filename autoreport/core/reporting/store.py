"""Project-local persistence for reporting workflow state."""

import json
from pathlib import Path
from typing import Any


class ReportingStore:
    """Write reporting state only beneath the active AutoReport workspace."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    def ensure_layout(self) -> None:
        for relative in (
            "Inputs",
            "Knowledge",
            "Work/runs",
            "Outputs/Modules",
            "Outputs/Reviews",
            "Outputs/Reports",
        ):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)

    def write_json(self, relative: str, value: Any) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def write_jsonl(self, relative: str, values: list[dict[str, Any]]) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(
            json.dumps(value, ensure_ascii=False) + "\n" for value in values
        )
        path.write_text(content, encoding="utf-8")
        return path

    def write_text(self, relative: str, content: str) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
