from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ProjectStoreError(ValueError):
    pass


class ProjectStore:
    PROJECT_DIRECTORIES = (
        Path("Inputs"),
        Path("Knowledge"),
        Path("Work/runs"),
        Path("Outputs/Modules"),
        Path("Outputs/Reviews"),
        Path("Outputs/Reports"),
    )

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def create(self) -> Path:
        if self.root.exists() and not self.root.is_dir():
            raise ProjectStoreError(f"project root is not a directory: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        for relative in self.PROJECT_DIRECTORIES:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        return self.root

    def write_json(self, relative_path: Path, value: object) -> Path:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=self._json_default,
        )
        return self.write_text(relative_path, f"{text}\n")

    def write_jsonl(self, relative_path: Path, rows: Iterable[object]) -> Path:
        text = "\n".join(
            json.dumps(row, ensure_ascii=False, default=self._json_default) for row in rows
        )
        if text:
            text += "\n"
        return self.write_text(relative_path, text)

    def write_text(self, relative_path: Path, text: str) -> Path:
        target = self._safe_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(f".{target.name}.pending")
        pending.write_text(text, encoding="utf-8")
        pending.replace(target)
        return target

    def save_run(self, run_id: str, value: object) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ProjectStoreError("run_id must be a non-empty filename component")
        return self.write_json(Path("Work/runs") / f"{run_id}.json", value)

    def _safe_path(self, relative_path: Path) -> Path:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ProjectStoreError("path must stay inside the project")
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ProjectStoreError("path must stay inside the project")
        return target

    @staticmethod
    def _json_default(value: Any) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, Path):
            return value.as_posix()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(f"cannot serialize {type(value).__name__}")

