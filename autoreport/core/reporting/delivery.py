"""Transactional project delivery contract for complete five-module reports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from docx import Document
from pydantic import Field, field_validator

from .agentic_models import StrictModel
from .models import REPORT_MODULE_IDS


class DeliveryPackage(StrictModel):
    report_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    module_files: dict[str, Path]
    final_docx: Path
    report_state: Path

    @field_validator("module_files")
    @classmethod
    def complete_module_set(cls, value: dict[str, Path]) -> dict[str, Path]:
        if set(value) != set(REPORT_MODULE_IDS):
            raise ValueError("delivery requires exactly modules 2.1-2.5")
        return value


class DeliveryReceipt(StrictModel):
    success: bool
    delivery_dir: Path
    final_docx: Path
    module_files: dict[str, Path]
    report_state: Path
    manifest_path: Path
    artifact_sha256: dict[str, str]


class ProjectDelivery:
    """Validate all artifacts before atomically publishing a success package."""

    def __init__(self, delivery_root: Path):
        self.delivery_root = Path(delivery_root)

    def deliver(self, package: DeliveryPackage) -> DeliveryReceipt:
        self._validate_inputs(package)
        destination = self.delivery_root / f"{package.report_id}-{package.version}"
        if destination.exists():
            raise FileExistsError(f"delivery version already exists: {destination}")

        self.delivery_root.parent.mkdir(parents=True, exist_ok=True)
        staging_parent = self.delivery_root.parent
        with tempfile.TemporaryDirectory(prefix=".autoreport-delivery-", dir=staging_parent) as temp_dir:
            staging = Path(temp_dir) / destination.name
            modules_dir = staging / "modules"
            modules_dir.mkdir(parents=True)
            copied_modules: dict[str, Path] = {}
            for module_id in REPORT_MODULE_IDS:
                target = modules_dir / f"{module_id}.md"
                shutil.copyfile(package.module_files[module_id], target)
                copied_modules[module_id] = target
            final_target = staging / "配电安全专家咨询报告.docx"
            state_target = staging / "report-state.json"
            shutil.copyfile(package.final_docx, final_target)
            shutil.copyfile(package.report_state, state_target)

            hashes = {
                "final_docx": self._sha256(final_target),
                "report_state": self._sha256(state_target),
                **{
                    f"module:{module_id}": self._sha256(path)
                    for module_id, path in copied_modules.items()
                },
            }
            manifest = {
                "report_id": package.report_id,
                "version": package.version,
                "status": "success",
                "modules": list(REPORT_MODULE_IDS),
                "artifacts": hashes,
            }
            manifest_path = staging / "delivery-manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            hashes["manifest"] = self._sha256(manifest_path)

            self.delivery_root.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)

        return DeliveryReceipt(
            success=True,
            delivery_dir=destination,
            final_docx=destination / final_target.name,
            module_files={
                module_id: destination / "modules" / f"{module_id}.md"
                for module_id in REPORT_MODULE_IDS
            },
            report_state=destination / state_target.name,
            manifest_path=destination / manifest_path.name,
            artifact_sha256=hashes,
        )

    @staticmethod
    def _validate_inputs(package: DeliveryPackage) -> None:
        if set(package.module_files) != set(REPORT_MODULE_IDS):
            raise ValueError("delivery requires exactly modules 2.1-2.5")
        paths = [*package.module_files.values(), package.final_docx, package.report_state]
        missing = [str(path) for path in paths if not Path(path).is_file()]
        if missing:
            raise FileNotFoundError(f"delivery artifacts missing: {missing}")
        if package.final_docx.suffix.lower() != ".docx":
            raise ValueError("final report must be a .docx file")
        try:
            Document(package.final_docx)
        except Exception as exc:
            raise ValueError("final report is not Word/WPS-openable") from exc
        try:
            state = json.loads(package.report_state.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("report state must be valid UTF-8 JSON") from exc
        if not isinstance(state, dict):
            raise ValueError("report state must be a JSON object")

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
