"""Production hardening ports and local reference adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import StrictModel

from .parallel_runtime import atomic_write_json, exclusive_file_lock


class IdentityClaims(StrictModel):
    subject: str = Field(min_length=1)
    groups: list[str] = Field(default_factory=list)
    issuer: str = Field(min_length=1)


class IdentityProvider(Protocol):
    def verify(self, bearer_token: str) -> IdentityClaims: ...


class SecretProvider(Protocol):
    def get(self, name: str) -> str: ...


class EnvironmentSecretProvider:
    """Allowlisted environment adapter; arbitrary environment reads are denied."""

    def __init__(
        self,
        allowed_names: set[str],
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.allowed_names = frozenset(allowed_names)
        self.env = env if env is not None else os.environ

    def get(self, name: str) -> str:
        if name not in self.allowed_names or not re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
            raise PermissionError("secret name is not allowlisted")
        value = self.env.get(name)
        if not value:
            raise KeyError(f"required secret is unavailable: {name}")
        return value


class EgressPolicy(StrictModel):
    allowed_https_hosts: set[str] = Field(default_factory=set)

    def require(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or host not in {
            value.casefold() for value in self.allowed_https_hosts
        }:
            raise PermissionError("outbound destination is denied by policy")


class ProductionPolicy(StrictModel):
    upload_max_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    allowed_upload_suffixes: set[str] = Field(
        default_factory=lambda: {
            ".csv",
            ".docx",
            ".jpeg",
            ".jpg",
            ".json",
            ".md",
            ".pdf",
            ".png",
            ".txt",
            ".xlsx",
        }
    )
    data_region: str = Field(min_length=1)
    model_processing_regions: set[str] = Field(default_factory=set)
    retention_days: int = Field(default=365, ge=1)
    backup_rpo_minutes: int = Field(default=60, ge=1)
    recovery_rto_minutes: int = Field(default=240, ge=1)

    def validate_upload(self, logical_ref: str, size: int) -> None:
        if size > self.upload_max_bytes:
            raise ValueError("upload exceeds production size limit")
        suffix = Path(logical_ref).suffix.casefold()
        if suffix not in self.allowed_upload_suffixes:
            raise ValueError("upload type is not allowed")


class DeploymentIdentity(StrictModel):
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    migration_version: str = Field(min_length=1)
    build_epoch: int = Field(ge=1)
    policy: ProductionPolicy


class SecurityAuditLog:
    """Hash-chained, append-only security events with sensitive-field redaction."""

    REDACTED_KEYS = frozenset(
        {"authorization", "token", "api_key", "secret", "password"}
    )

    def __init__(self, state_root: Path) -> None:
        self.path = Path(state_root).resolve() / "audit/security.jsonl"
        self.lock_path = self.path.with_suffix(".lock")

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if key.casefold() in cls.REDACTED_KEYS else cls._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    def append(
        self,
        event_type: str,
        *,
        actor_id: str,
        project_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not event_type.strip() or not actor_id.strip():
            raise ValueError("audit event and actor are required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            lines = (
                [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]
                if self.path.is_file()
                else []
            )
            previous_hash = (
                json.loads(lines[-1])["event_hash"] if lines else "0" * 64
            )
            event = {
                "sequence": len(lines) + 1,
                "occurred_at_ns": time.time_ns(),
                "event_type": event_type,
                "actor_id": actor_id,
                "project_id": project_id,
                "details": self._redact(details or {}),
                "previous_hash": previous_hash,
            }
            canonical = json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            event["event_hash"] = hashlib.sha256(canonical).hexdigest()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def verify(self) -> int:
        previous = "0" * 64
        count = 0
        if not self.path.is_file():
            return 0
        for count, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            event = json.loads(line)
            event_hash = event.pop("event_hash")
            if event.get("sequence") != count or event.get("previous_hash") != previous:
                raise ValueError("security audit chain is discontinuous")
            canonical = json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if hashlib.sha256(canonical).hexdigest() != event_hash:
                raise ValueError("security audit event hash mismatch")
            previous = event_hash
        return count


class RuntimeMetrics:
    """Process-safe structured metric samples suitable for scraping/export."""

    def __init__(self, state_root: Path) -> None:
        self.path = Path(state_root).resolve() / "metrics/samples.jsonl"
        self.lock_path = self.path.with_suffix(".lock")

    def observe(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*", name):
            raise ValueError("metric name is invalid")
        row = {
            "name": name,
            "value": float(value),
            "labels": labels or {},
            "observed_at_ns": time.time_ns(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")


class BackupManager:
    """Immutable local backup/restore reference adapter with full hash manifest."""

    def __init__(self, backup_root: Path) -> None:
        self.root = Path(backup_root).resolve()

    def create(self, backup_id: str, sources: dict[str, Path]) -> Path:
        if not backup_id or Path(backup_id).name != backup_id:
            raise ValueError("backup id must be one safe path component")
        destination = self.root / backup_id
        if destination.exists():
            raise FileExistsError("backup id already exists")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=self.root) as temporary:
            staging = Path(temporary) / backup_id
            manifest_files: list[dict[str, Any]] = []
            for label, source_root in sorted(sources.items()):
                source_root = Path(source_root).resolve()
                if not source_root.is_dir():
                    raise FileNotFoundError(f"backup source is missing: {label}")
                target_root = staging / label
                shutil.copytree(source_root, target_root)
                for path in sorted(target_root.rglob("*")):
                    if path.is_file():
                        content = path.read_bytes()
                        manifest_files.append(
                            {
                                "ref": path.relative_to(staging).as_posix(),
                                "size": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        )
            manifest = {
                "schema_version": 1,
                "backup_id": backup_id,
                "files": manifest_files,
            }
            atomic_write_json(staging / "backup-manifest.json", manifest)
            os.replace(staging, destination)
        return destination

    def verify(self, backup_id: str) -> int:
        root = self.root / backup_id
        manifest = json.loads(
            (root / "backup-manifest.json").read_text(encoding="utf-8")
        )
        for item in manifest["files"]:
            path = root / item["ref"]
            content = path.read_bytes()
            if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError(f"backup file failed validation: {item['ref']}")
        return len(manifest["files"])

    def restore(self, backup_id: str, target_root: Path) -> Path:
        self.verify(backup_id)
        target_root = Path(target_root).resolve()
        if target_root.exists():
            raise FileExistsError("restore target must not already exist")
        shutil.copytree(self.root / backup_id, target_root)
        return target_root


def write_deployment_identity(
    state_root: Path, identity: DeploymentIdentity
) -> Path:
    path = Path(state_root).resolve() / "deployment/identity.json"
    atomic_write_json(path, identity.model_dump(mode="json"))
    return path
