"""Deterministic, manifest-verified backup and guarded restore helper."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Backup refuses symbolic links: {path}")
        if path.is_file():
            result.append(path)
    return result


class _HashingReader:
    """Read a file for the archive and hash exactly the bytes archived."""

    def __init__(self, handle) -> None:
        self._handle = handle
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        self.digest.update(chunk)
        return chunk


def backup(source: Path, output_directory: Path) -> Path:
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Backup source must be a directory")
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = output_directory / f"manyselves-{stamp}.tar.gz"
    hashes: dict[str, str] = {}
    created = datetime.now(UTC).isoformat()
    # Each file is hashed while it is archived, so the manifest always
    # describes the archived bytes even if a file changes during the backup.
    with tarfile.open(archive, "w:gz") as bundle:
        for path in _files(source):
            name = path.relative_to(source).as_posix()
            info = bundle.gettarinfo(path, arcname=f"snapshot/{name}")
            with path.open("rb") as handle:
                reader = _HashingReader(handle)
                bundle.addfile(info, reader)
            hashes[name] = reader.digest.hexdigest()
        manifest = json.dumps(
            {"createdAt": created, "files": hashes, "formatVersion": 1},
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        info.mtime = int(datetime.now(UTC).timestamp())
        info.mode = 0o600
        bundle.addfile(info, io.BytesIO(manifest))
    return archive


def accounts(path: Path) -> list[tuple[str, str]]:
    """(username, password environment variable) of each account in an accounts file."""

    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        payload = yaml.safe_load(text) or {}
        entries = payload.get("accounts") or []
    else:
        # The file's fixed shape: a list of flat "key: value" entries.
        entries, current = [], None
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].rstrip()
            stripped = line.strip()
            if stripped.startswith("- "):
                current = {}
                entries.append(current)
                stripped = stripped[2:].strip()
            if current is not None and ":" in stripped:
                key, value = stripped.split(":", 1)
                current[key.strip()] = value.strip().strip("'\"")
    result = []
    for entry in entries:
        username = str(entry.get("username", "")).strip()
        password_env = str(entry.get("passwordEnv", "")).strip()
        if not username or not password_env:
            raise ValueError("each account needs username and passwordEnv")
        result.append((username, password_env))
    if not result:
        raise ValueError(f"no accounts in {path}")
    return result


def _safe_members(bundle: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = bundle.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise ValueError(f"Unsafe archive member: {member.name}")
    return members


def restore(archive: Path, target: Path, *, force: bool) -> Path | None:
    archive = archive.resolve(strict=True)
    target = target.resolve()
    if target == target.parent:
        raise ValueError("Restore target cannot be a filesystem root")
    previous = None
    if target.exists() and any(target.iterdir()):
        if not force:
            raise ValueError("Restore target is not empty; pass --force to preserve and replace it")
        previous = target.with_name(f"{target.name}.previous-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
        if previous.exists():
            raise ValueError(f"Preservation target already exists: {previous}")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = _safe_members(bundle)
            bundle.extractall(staging, members=members, filter="data")
        manifest = json.loads((staging / "manifest.json").read_text("utf-8"))
        snapshot = staging / "snapshot"
        actual = {path.relative_to(snapshot).as_posix(): _hash(path) for path in _files(snapshot)}
        if manifest.get("formatVersion") != 1 or actual != manifest.get("files"):
            raise ValueError("Backup manifest verification failed")
        if target.exists():
            if previous is not None:
                target.replace(previous)
            else:
                target.rmdir()
        snapshot.replace(target)
        return previous
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source", required=True, type=Path)
    backup_parser.add_argument("--output", required=True, type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--archive", required=True, type=Path)
    restore_parser.add_argument("--target", required=True, type=Path)
    restore_parser.add_argument("--force", action="store_true")
    accounts_parser = subparsers.add_parser("accounts")
    accounts_parser.add_argument("--file", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        print(backup(args.source, args.output))
    elif args.command == "accounts":
        for username, password_env in accounts(args.file):
            print(f"{username}\t{password_env}")
    else:
        previous = restore(args.archive, args.target, force=args.force)
        print(json.dumps({"restored": str(args.target.resolve()), "previous": str(previous) if previous else None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
