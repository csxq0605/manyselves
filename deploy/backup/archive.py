"""Deterministic, manifest-verified backup and guarded restore helper."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
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


def _internal_link(root: Path, path: Path) -> str:
    """Return a symlink's target when it is relative and stays inside ``root``.

    The product links run input views to its own content store with relative
    links; anything else (absolute, leaving the data root, dangling) is refused.
    """

    target = os.readlink(path)
    if os.path.isabs(target):
        raise ValueError(f"Backup refuses absolute symbolic links: {path}")
    resolved = (path.parent / target).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"Backup refuses symbolic links outside the data: {path}")
    return Path(target).as_posix()


def _entries(root: Path) -> tuple[list[Path], dict[str, str]]:
    """Regular files and internal symbolic links (name -> target) under ``root``."""

    root = root.resolve()
    files: list[Path] = []
    links: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            links[path.relative_to(root).as_posix()] = _internal_link(root, path)
        elif path.is_file():
            files.append(path)
    return files, links


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
    files, links = _entries(source)
    with tarfile.open(archive, "w:gz") as bundle:
        for path in files:
            name = path.relative_to(source).as_posix()
            info = bundle.gettarinfo(path, arcname=f"snapshot/{name}")
            with path.open("rb") as handle:
                reader = _HashingReader(handle)
                bundle.addfile(info, reader)
            hashes[name] = reader.digest.hexdigest()
        # Internal links are stored as links, so content-store views keep
        # pointing at their blobs after a restore and nothing is stored twice.
        for name, target in links.items():
            info = tarfile.TarInfo(f"snapshot/{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mtime = int(datetime.now(UTC).timestamp())
            info.mode = 0o777
            bundle.addfile(info)
        manifest = json.dumps(
            {"createdAt": created, "files": hashes, "formatVersion": 2, "links": links},
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


def _manifest(bundle: tarfile.TarFile) -> dict:
    member = bundle.getmember("manifest.json")
    if not member.isfile():
        raise ValueError("Backup manifest is not a regular file")
    handle = bundle.extractfile(member)
    if handle is None:
        raise ValueError("Backup manifest is unreadable")
    manifest = json.loads(handle.read().decode("utf-8"))
    if manifest.get("formatVersion") not in {1, 2}:
        raise ValueError("Unsupported backup format")
    return manifest


def _safe_members(bundle: tarfile.TarFile, links: dict[str, str]) -> list[tarfile.TarInfo]:
    """Plain files and directories, plus exactly the internal links the manifest lists."""

    members = bundle.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.islnk():
            raise ValueError(f"Unsafe archive member: {member.name}")
        if member.issym():
            name = path.relative_to("snapshot").as_posix() if path.parts[:1] == ("snapshot",) else None
            target = PurePosixPath(member.linkname)
            if name is None or links.get(name) != member.linkname or target.is_absolute():
                raise ValueError(f"Unsafe archive member: {member.name}")
            # The target must stay inside the snapshot.
            depth = len(path.parent.parts) - 1
            for part in target.parts:
                depth = depth - 1 if part == ".." else depth + (part != ".")
                if depth < 0:
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
            manifest = _manifest(bundle)
            expected_links = dict(manifest.get("links") or {})
            members = _safe_members(bundle, expected_links)
            bundle.extractall(staging, members=members, filter="data")
        snapshot = staging / "snapshot"
        snapshot.mkdir(exist_ok=True)
        files, links = _entries(snapshot)
        actual = {path.relative_to(snapshot).as_posix(): _hash(path) for path in files}
        if actual != manifest.get("files") or links != expected_links:
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
