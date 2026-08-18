"""Deterministic, manifest-verified backup and guarded restore helper."""

from __future__ import annotations

import argparse
import hashlib
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


def backup(source: Path, output_directory: Path) -> Path:
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Backup source must be a directory")
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = output_directory / f"manyselves-{stamp}.tar.gz"
    files = _files(source)
    manifest = {
        "createdAt": datetime.now(UTC).isoformat(),
        "files": {path.relative_to(source).as_posix(): _hash(path) for path in files},
        "formatVersion": 1,
    }
    with tempfile.TemporaryDirectory(prefix="manyselves-backup-") as temporary:
        manifest_path = Path(temporary) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(manifest_path, arcname="manifest.json", recursive=False)
            for path in files:
                bundle.add(path, arcname=f"snapshot/{path.relative_to(source).as_posix()}", recursive=False)
    return archive


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
    args = parser.parse_args()
    if args.command == "backup":
        print(backup(args.source, args.output))
    else:
        previous = restore(args.archive, args.target, force=args.force)
        print(json.dumps({"restored": str(args.target.resolve()), "previous": str(previous) if previous else None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
