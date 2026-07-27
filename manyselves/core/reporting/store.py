"""Project-local persistence for reporting workflow state."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ReportingStore:
    """Write reporting state only beneath the active Manyselves workspace."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    def ensure_layout(self) -> None:
        for relative in (
            "Inputs",
            "Knowledge",
            "Templates",
            "Work/runs",
            "Outputs/Modules",
            "Outputs/Reviews",
            "Outputs/Reports",
        ):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)

    def write_json(self, relative: str, value: Any) -> Path:
        return self._atomic_write(
            relative,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    def write_run_model(self, run_id: str, relative: str, model: BaseModel) -> Path:
        safe_run_id = Path(run_id).name
        relative_path = Path(relative)
        if (
            not run_id
            or safe_run_id != run_id
            or relative_path.is_absolute()
            or ".." in relative_path.parts
        ):
            raise ValueError("run path must stay beneath Work/runs/<run_id>")

        workspace_root = self.workspace.resolve()
        runs_root = (workspace_root / "Work/runs").resolve()
        run_root = (runs_root / safe_run_id).resolve()
        target = (run_root / relative_path).resolve()
        if not (
            runs_root.is_relative_to(workspace_root)
            and run_root.is_relative_to(runs_root)
            and target.is_relative_to(run_root)
        ):
            raise ValueError("run path must stay beneath Work/runs/<run_id>")

        return self.write_json(
            f"Work/runs/{safe_run_id}/{relative}",
            model.model_dump(mode="json"),
        )

    def write_jsonl(self, relative: str, values: list[dict[str, Any]]) -> Path:
        content = "".join(
            json.dumps(value, ensure_ascii=False) + "\n" for value in values
        )
        return self._atomic_write(relative, content)

    def write_text(self, relative: str, content: str) -> Path:
        return self._atomic_write(relative, content)

    def _atomic_write(self, relative: str, content: str) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        return path
