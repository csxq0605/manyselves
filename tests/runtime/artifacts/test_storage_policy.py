from pathlib import Path

from manyselves.runtime.artifacts.storage_policy import CasPolicy, StorageMode


def test_role_rules_precede_size_and_suffix_rules() -> None:
    policy = CasPolicy(large_size_bytes=8)

    assert policy.decide(logical_role="input_snapshot", size_bytes=1, suffix=".json").mode is StorageMode.CAS
    assert policy.decide(logical_role="template", size_bytes=1, suffix=".txt").mode is StorageMode.CAS
    assert policy.decide(logical_role="photo", size_bytes=1, suffix=".jpg").mode is StorageMode.CAS
    assert policy.decide(logical_role="final_docx", size_bytes=1, suffix=".docx").mode is StorageMode.CAS
    # Lifecycle metadata is materialized even when a caller supplies a large
    # size, keeping receipts/state independently readable.
    assert policy.decide(logical_role="state", size_bytes=100, suffix=".json").mode is StorageMode.MATERIALIZED


def test_unknown_roles_use_size_then_semantic_suffix() -> None:
    policy = CasPolicy(large_size_bytes=8)

    assert policy.decide(logical_role="evidence", size_bytes=7, suffix=".txt").mode is StorageMode.MATERIALIZED
    assert policy.decide(logical_role="evidence", size_bytes=8, suffix=".txt").mode is StorageMode.CAS
    assert policy.decide(logical_role="artifact", size_bytes=1, suffix=".json").mode is StorageMode.MATERIALIZED
    assert policy.decide(logical_role="artifact", size_bytes=1, suffix=".png").mode is StorageMode.CAS


def test_decision_contains_versioned_audit_record(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.bin"
    source.write_bytes(b"snapshot")
    decision = CasPolicy(policy_version="g1-test").decide(
        source,
        logical_role="input_snapshot",
    )

    assert decision.policy_version == "g1-test"
    assert decision.size_bytes == source.stat().st_size
    assert decision.as_dict()["mode"] == "cas"
    assert source.read_bytes() == b"snapshot"
