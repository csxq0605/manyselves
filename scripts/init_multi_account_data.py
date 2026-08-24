#!/usr/bin/env python3
"""Initialize isolated account storage without overwriting existing data."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import yaml


PROJECT_MARKERS = frozenset({"inputs", "knowledge", "templates", "work", "outputs"})


def _is_project_workspace(path: Path) -> bool:
    return any(
        child.is_dir() and child.name.casefold() in PROJECT_MARKERS
        for child in path.iterdir()
    )


def _copy_missing(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink() or source.is_symlink():
        return
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _migrate_legacy_admin_data(data_root: Path, admin_root: Path) -> None:
    admin_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    for child in data_root.iterdir():
        if child.name in {".manyselves", "accounts"}:
            continue
        if child.is_dir() and not child.is_symlink() and _is_project_workspace(child):
            _copy_missing(child, admin_root / child.name)

    legacy_hidden = data_root / ".manyselves"
    if legacy_hidden.is_dir() and not legacy_hidden.is_symlink():
        admin_hidden = admin_root / ".manyselves"
        admin_hidden.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in legacy_hidden.iterdir():
            if child.name in {"accounts.yaml", "auth"}:
                continue
            _copy_missing(child, admin_hidden / child.name)

    legacy_config = data_root / "manyselves.config.yaml"
    tenant_config = admin_root / ".manyselves" / "config" / "manyselves.config.yaml"
    _copy_missing(legacy_config, tenant_config)


def _write_missing(source: Path, destination: Path, mode: int) -> None:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        temporary.replace(destination)
    os.chmod(destination, mode)


def initialize_multi_account_data(
    data_root: Path,
    accounts_template: Path,
    config_template: Path,
    *,
    accounts_file: Path | None = None,
) -> None:
    data_root = data_root.resolve()
    accounts_template = accounts_template.resolve()
    config_template = config_template.resolve()
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    manifest_payload = yaml.safe_load(accounts_template.read_text(encoding="utf-8")) or {}
    accounts = manifest_payload.get("accounts")
    if manifest_payload.get("version") != 1 or not isinstance(accounts, list):
        raise ValueError("accounts template must contain version: 1 and an accounts list")

    manifest_path = (
        accounts_file.resolve()
        if accounts_file is not None
        else data_root / ".manyselves" / "accounts.yaml"
    )
    _write_missing(accounts_template, manifest_path, 0o600)

    accounts_root = data_root / "accounts"
    accounts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    admin_root = accounts_root / "admin"
    _migrate_legacy_admin_data(data_root, admin_root)

    for account in accounts:
        account_id = str(account.get("id", "")).strip()
        if not account_id:
            raise ValueError("each account requires an id")
        account_root = accounts_root / account_id
        account_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(account_root, 0o700)
        config_path = account_root / ".manyselves" / "config" / "manyselves.config.yaml"
        _write_missing(config_template, config_path, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--accounts-file", type=Path)
    parser.add_argument("--accounts-template", type=Path, required=True)
    parser.add_argument("--config-template", type=Path, required=True)
    arguments = parser.parse_args()
    initialize_multi_account_data(
        arguments.data_root,
        arguments.accounts_template,
        arguments.config_template,
        accounts_file=arguments.accounts_file,
    )


if __name__ == "__main__":
    main()
