"""Offline regression coverage for artifact/tool contract boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from manyselves.runtime.artifacts import (
    ArtifactGateway,
    ArtifactGrant,
    ToolContractError,
    descriptor_for_path,
)
from manyselves.runtime.tools.contracts import normalize_tool_call
from manyselves.runtime.tools.result_memory import RunToolResultIndex


def test_descriptor_routes_known_formats_and_binary_without_text_probe(tmp_path: Path) -> None:
    text = tmp_path / "note.txt"
    text.write_text("hello", encoding="utf-8")
    assert descriptor_for_path(text).kind == "text"

    for suffix, magic, expected in (
        (".docx", b"PK\x03\x04", "document"),
        (".xlsx", b"PK\x03\x04", "spreadsheet"),
        (".pdf", b"%PDF-1.7", "pdf"),
        (".png", b"\x89PNG\r\n\x1a\n", "image"),
        (".bin", b"\x80\x81\x82", "binary"),
    ):
        target = tmp_path / f"artifact{suffix}"
        target.write_bytes(magic + b"payload")
        descriptor = descriptor_for_path(target)
        assert descriptor.kind == expected
        assert len(descriptor.sha256) == 64
        assert descriptor.size_bytes == target.stat().st_size


def test_gateway_binary_open_and_search_are_structured_and_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "binary.bin"
    target.write_bytes(b"\x80\x81\x82\x83")
    gateway = ArtifactGateway(tmp_path, ArtifactGrant("wf", "task", "agent", "session"), secret=b"x" * 32)

    with pytest.raises(ToolContractError) as open_error:
        gateway.open("binary.bin")
    assert open_error.value.code == "unsupported_operation"
    assert open_error.value.as_dict()["details"]["required_tool"] == "manual_review"

    with pytest.raises(ToolContractError) as search_error:
        gateway.search("binary.bin", "needle")
    assert search_error.value.code == "unsupported_operation"


def test_normalize_tool_call_only_repairs_explicitly_authorized_refs() -> None:
    repaired = normalize_tool_call(
        "open_tool_result",
        {"ref": "Inputs/evidence.json", "offset": 4},
        ["Inputs/evidence.json"],
    )
    assert repaired.accepted is True
    assert repaired.name == "open_artifact"
    assert repaired.changed is True

    rejected = normalize_tool_call(
        "open_tool_result",
        {"ref": "Inputs/not-delivered.json"},
        [],
    )
    assert rejected.accepted is False
    assert rejected["error"]["code"] == "non_opaque_reference"

    unresolved = normalize_tool_call(
        "inspect_image",
        {"path": "P-0013"},
        [],
    )
    assert unresolved.accepted is False
    assert unresolved["error"]["code"] == "image_scope_unresolved"

    resolved = normalize_tool_call(
        "inspect_image",
        {"path": "P-0013"},
        ["Inputs/photos/P-0013.png"],
        image_resolver=lambda value: "Inputs/photos/P-0013.png",
    )
    assert resolved.accepted is True
    assert resolved.arguments["path"] == "Inputs/photos/P-0013.png"


def test_result_index_deduplicates_across_instances_and_ignores_torn_line(tmp_path: Path) -> None:
    first = RunToolResultIndex(tmp_path, "run-1")
    recorded = first.record("task", "search_text", {"ref": "a", "q": "b"}, {"matches": []})
    assert recorded["deduplicated"] is False

    second = RunToolResultIndex(tmp_path, "run-1")
    duplicate = second.record("task", "search_text", {"q": "b", "ref": "a"}, {"matches": ["new"]})
    assert duplicate["deduplicated"] is True
    assert duplicate["result"] == {"matches": []}

    second.path.write_text(second.path.read_text(encoding="utf-8") + "{torn\n", encoding="utf-8")
    reloaded = RunToolResultIndex(tmp_path, "run-1")
    assert reloaded.lookup("task", "search_text", {"ref": "a", "q": "b"}) is not None


def test_opaque_ref_signature_identity_and_hash_are_fail_closed(tmp_path: Path) -> None:
    grant = ArtifactGrant("wf", "task", "agent", "session")
    owner = ArtifactGateway(tmp_path, grant, secret=b"x" * 32)
    ref = owner.persist_internal("tool", "call", "payload")
    assert owner.open_internal(ref).content == "payload"

    with pytest.raises(PermissionError):
        owner.scoped(ArtifactGrant("wf", "task", "agent", "other")).open_internal(ref)

    # Signature corruption cannot be converted to a public path.
    corrupted = f"{ref[:-1]}{'0' if ref[-1] != '0' else '1'}"
    with pytest.raises(PermissionError):
        owner.open_internal(corrupted)

    payload = owner._decode(ref)
    target = tmp_path / payload["path"]
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(ToolContractError) as hash_error:
        owner.describe(ref)
    assert hash_error.value.code == "invalid_reference"
