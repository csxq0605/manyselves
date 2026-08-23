"""Artifact/object-store ports for optional no-shared-filesystem deployment."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import StrictModel

from .parallel_runtime import ArtifactRef, atomic_write_json, exclusive_file_lock


class ArtifactObjectStore(Protocol):
    def put(
        self,
        logical_ref: str,
        content: bytes,
        *,
        media_type: str,
        project_lease_epoch: int | None = None,
    ) -> ArtifactRef: ...

    def get(self, artifact: ArtifactRef) -> bytes: ...


class FilesystemObjectStore:
    """Reference object adapter whose root need not be a project volume."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.lock_path = self.root / "objects.lock"

    @staticmethod
    def _logical(value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError("artifact logical ref is invalid")
        return value

    def put(
        self,
        logical_ref: str,
        content: bytes,
        *,
        media_type: str,
        project_lease_epoch: int | None = None,
    ) -> ArtifactRef:
        logical_ref = self._logical(logical_ref)
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / "sha256" / digest[:2] / digest
        manifest = self.root / "refs" / f"{hashlib.sha256(logical_ref.encode()).hexdigest()}.json"
        with exclusive_file_lock(self.lock_path):
            if target.is_file():
                if target.read_bytes() != content:
                    raise ValueError("object digest collision or corruption")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".object-",
                    suffix=".tmp",
                    dir=target.parent,
                    delete=False,
                ) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary = Path(handle.name)
                os.replace(temporary, target)
            ref = ArtifactRef(
                ref=logical_ref,
                sha256=digest,
                size=len(content),
                media_type=media_type,
                project_lease_epoch=project_lease_epoch,
            )
            if manifest.is_file():
                existing = ArtifactRef.model_validate_json(
                    manifest.read_text(encoding="utf-8")
                )
                if existing != ref:
                    raise ValueError("logical artifact ref already has different identity")
            else:
                atomic_write_json(manifest, ref.model_dump(mode="json"))
            return ref

    def get(self, artifact: ArtifactRef) -> bytes:
        self._logical(artifact.ref)
        target = self.root / "sha256" / artifact.sha256[:2] / artifact.sha256
        if not target.is_file():
            raise FileNotFoundError("artifact object is missing")
        content = target.read_bytes()
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError("artifact object failed identity validation")
        return content


class PortableWorkspaceMaterializer:
    """Materialize object refs for document libraries, then upload only outputs."""

    def __init__(self, store: ArtifactObjectStore, temporary_root: Path) -> None:
        self.store = store
        self.temporary_root = Path(temporary_root).resolve()
        self.temporary_root.mkdir(parents=True, exist_ok=True)

    def materialize(self, artifacts: list[ArtifactRef]) -> Path:
        sandbox = Path(
            tempfile.mkdtemp(prefix="manyselves-worker-", dir=self.temporary_root)
        ).resolve()
        try:
            for artifact in artifacts:
                content = self.store.get(artifact)
                target = sandbox / "inputs" / artifact.ref
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o400)
            (sandbox / "outputs").mkdir(parents=True, exist_ok=True)
            return sandbox
        except BaseException:
            self.cleanup(sandbox)
            raise

    def publish_output(
        self,
        sandbox: Path,
        source: Path,
        logical_ref: str,
        *,
        media_type: str,
        project_lease_epoch: int,
    ) -> ArtifactRef:
        sandbox = Path(sandbox).resolve()
        source = Path(source).resolve()
        if not source.is_relative_to(sandbox / "outputs") or not source.is_file():
            raise ValueError("portable worker output escapes sandbox")
        return self.store.put(
            logical_ref,
            source.read_bytes(),
            media_type=media_type,
            project_lease_epoch=project_lease_epoch,
        )

    def cleanup(self, sandbox: Path) -> None:
        sandbox = Path(sandbox).resolve()
        if (
            sandbox.parent != self.temporary_root
            or not sandbox.name.startswith("manyselves-worker-")
        ):
            raise ValueError("portable worker cleanup target is invalid")
        shutil.rmtree(sandbox)


class RevisionPointer(StrictModel):
    pointer_name: str
    revision: str
    artifact: ArtifactRef
    fencing_token: int = Field(ge=1)


class CompareAndSwapPointerStore:
    """CAS current/latest update using expected revision and fencing token."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.lock_path = self.root / "pointers.lock"

    def read(self, pointer_name: str) -> RevisionPointer | None:
        if not pointer_name or Path(pointer_name).name != pointer_name:
            raise ValueError("pointer name is invalid")
        path = self.root / f"{pointer_name}.json"
        return (
            RevisionPointer.model_validate_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )

    def compare_and_swap(
        self,
        pointer_name: str,
        *,
        expected_revision: str | None,
        new_revision: str,
        artifact: ArtifactRef,
        fencing_token: int,
    ) -> RevisionPointer:
        with exclusive_file_lock(self.lock_path):
            current = self.read(pointer_name)
            actual_revision = current.revision if current is not None else None
            if actual_revision != expected_revision:
                raise RuntimeError("revision pointer compare-and-swap conflict")
            if current is not None and fencing_token < current.fencing_token:
                raise RuntimeError("stale fencing token cannot update revision pointer")
            pointer = RevisionPointer(
                pointer_name=pointer_name,
                revision=new_revision,
                artifact=artifact,
                fencing_token=fencing_token,
            )
            atomic_write_json(
                self.root / f"{pointer_name}.json",
                pointer.model_dump(mode="json"),
            )
            return pointer
