from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from pds_report.domain.models import ManifestEntry, ParseStatus, ProjectManifest

SUPPORTED_FORMATS = {
    "csv",
    "docx",
    "jpeg",
    "jpg",
    "json",
    "md",
    "pdf",
    "png",
    "txt",
    "xls",
    "xlsx",
}


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_project(project_root: Path) -> ProjectManifest:
    root = project_root.expanduser().resolve()
    entries: list[ManifestEntry] = []
    for directory_name, purpose in (("Inputs", "input"), ("Knowledge", "knowledge")):
        directory = root / directory_name
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            relative = path.relative_to(root)
            content_digest = _file_digest(path)
            identity_digest = sha256(
                f"{relative.as_posix()}\0{content_digest}".encode()
            ).hexdigest()
            file_format = path.suffix.lower().lstrip(".") or "unknown"
            parse_status = (
                ParseStatus.PENDING
                if file_format in SUPPORTED_FORMATS
                else ParseStatus.UNSUPPORTED
            )
            entries.append(
                ManifestEntry(
                    id=f"file-{identity_digest[:16]}",
                    relative_path=relative,
                    sha256=content_digest,
                    format=file_format,
                    purposes=[purpose],
                    parse_status=parse_status,
                )
            )
    return ProjectManifest(project_id=root.name, files=entries)
