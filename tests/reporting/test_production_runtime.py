from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.core.reporting.production_runtime import (
    BackupManager,
    EgressPolicy,
    EnvironmentSecretProvider,
    ProductionPolicy,
    SecurityAuditLog,
)


def test_default_deny_secret_egress_and_upload_policies() -> None:
    secrets = EnvironmentSecretProvider(
        {"MODEL_API_KEY"}, {"MODEL_API_KEY": "secret-value", "OTHER": "hidden"}
    )
    egress = EgressPolicy(allowed_https_hosts={"api.example.test"})
    policy = ProductionPolicy(
        data_region="cn-east",
        upload_max_bytes=10,
        allowed_upload_suffixes={".txt"},
    )

    assert secrets.get("MODEL_API_KEY") == "secret-value"
    with pytest.raises(PermissionError, match="allowlisted"):
        secrets.get("OTHER")
    egress.require("https://api.example.test/v1")
    with pytest.raises(PermissionError, match="denied"):
        egress.require("http://api.example.test/v1")
    with pytest.raises(PermissionError, match="denied"):
        egress.require("https://unapproved.example/v1")
    policy.validate_upload("Inputs/a.txt", 10)
    with pytest.raises(ValueError, match="size"):
        policy.validate_upload("Inputs/a.txt", 11)
    with pytest.raises(ValueError, match="type"):
        policy.validate_upload("Inputs/a.exe", 1)


def test_security_audit_is_hash_chained_and_redacts_secrets(tmp_path: Path) -> None:
    audit = SecurityAuditLog(tmp_path)
    audit.append(
        "login",
        actor_id="user-1",
        details={"token": "must-not-appear", "method": "oidc"},
    )
    audit.append("file.downloaded", actor_id="user-1", details={"ref": "x"})

    assert audit.verify() == 2
    raw = audit.path.read_text(encoding="utf-8")
    assert "must-not-appear" not in raw
    assert "[REDACTED]" in raw
    rows = raw.splitlines()
    tampered = json.loads(rows[1])
    tampered["actor_id"] = "attacker"
    rows[1] = json.dumps(tampered)
    audit.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        audit.verify()


def test_backup_manifest_verifies_and_restores_to_empty_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    state = tmp_path / "state"
    (project / "Work").mkdir(parents=True)
    state.mkdir()
    (project / "Work/data.json").write_text('{"ok": true}', encoding="utf-8")
    (state / "jobs.json").write_text("[]", encoding="utf-8")
    manager = BackupManager(tmp_path / "backups")

    manager.create("backup-001", {"project": project, "state": state})
    restored = manager.restore("backup-001", tmp_path / "restored")

    assert manager.verify("backup-001") == 2
    assert (restored / "project/Work/data.json").is_file()
    assert (restored / "state/jobs.json").is_file()
