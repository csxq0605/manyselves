"""Create a stable, input-only manifest for customer workbooks."""

import hashlib
from pathlib import Path

from ..models import ManifestFile, ProjectManifest


_WORKBOOK_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
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


def build_manifest(workspace: Path) -> ProjectManifest:
    """Hash supported workbooks under ``Inputs/`` without opening them."""

    workspace = Path(workspace).resolve()
    inputs = workspace / "Inputs"
    if not inputs.is_dir():
        return ProjectManifest()

    files: list[ManifestFile] = []
    candidates = sorted(
        path
        for path in inputs.rglob("*")
        if path.is_file() and path.suffix.casefold() in _WORKBOOK_MEDIA_TYPES
    )
    for path in candidates:
        relative_path = path.relative_to(workspace)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            ManifestFile(
                id=_stable_file_id(relative_path, digest),
                path=relative_path,
                sha256=digest,
                media_type=_WORKBOOK_MEDIA_TYPES[path.suffix.casefold()],
                purpose=_purpose_for(path),
            )
        )
    return ProjectManifest(files=files)
