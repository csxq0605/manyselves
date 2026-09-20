"""Create a stable, input-only manifest for customer workbooks."""

import hashlib
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    ManifestFile,
    ProjectManifest,
)

_INPUT_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".dwg": "image/vnd.dwg",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
}

_CORE_PURPOSE_MARKERS = {
    "s2-1": ("s2-1", "s2_1"),
    "s4-4": ("s4-4", "s4_4"),
    "s4-6": ("s4-6", "s4_6"),
}


def _purpose_for(path: Path) -> str | None:
    normalized_name = path.name.casefold()
    for purpose, markers in _CORE_PURPOSE_MARKERS.items():
        if any(marker in normalized_name for marker in markers):
            return purpose
    return None


def _stable_file_id(relative_path: Path, sha256: str) -> str:
    identity = f"{relative_path.as_posix()}\0{sha256}".encode()
    return f"file-{hashlib.sha256(identity).hexdigest()[:16]}"


def build_manifest(
    workspace: Path,
    *,
    input_root: Path | None = None,
) -> ProjectManifest:
    """Hash supported and explicit-manual-review inputs without opening them."""

    workspace = Path(workspace).resolve()
    inputs = Path(input_root).resolve() if input_root is not None else workspace / "Inputs"
    if not inputs.is_dir():
        return ProjectManifest()

    files: list[ManifestFile] = []
    candidates = sorted(
        path
        for path in inputs.rglob("*")
        if path.is_file() and path.suffix.casefold() in _INPUT_MEDIA_TYPES
    )
    for path in candidates:
        logical_path = Path("Inputs") / path.relative_to(inputs)
        snapshot_ref = (
            path.relative_to(workspace)
            if input_root is not None
            else None
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            ManifestFile(
                id=_stable_file_id(logical_path, digest),
                path=logical_path,
                sha256=digest,
                media_type=_INPUT_MEDIA_TYPES[path.suffix.casefold()],
                purpose=_purpose_for(logical_path),
                snapshot_ref=snapshot_ref,
            )
        )
    return ProjectManifest(files=files)
