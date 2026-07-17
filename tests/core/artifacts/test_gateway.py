from pathlib import Path

import pytest

from manyselves.core.artifacts import ArtifactGateway, ArtifactGrant


def test_gateway_pages_and_isolates_internal_refs(tmp_path: Path) -> None:
    (tmp_path / "Work").mkdir()
    (tmp_path / "Work/long.txt").write_text("x" * 1000, encoding="utf-8")
    owner = ArtifactGateway(tmp_path, ArtifactGrant("wf", "task", "agent", "s1"), secret=b"x" * 32)
    page = owner.open("Work/long.txt", limit=120)
    assert (page.total, page.returned, page.next_offset) == (1000, 120, 120)
    ref = owner.persist_internal("tool-result", "call", "secret")
    other = owner.scoped(ArtifactGrant("wf", "task", "agent", "s2"))
    with pytest.raises(PermissionError):
        other.open_internal(ref)
    with pytest.raises(PermissionError):
        owner.open(".manyselves/artifacts/tool-result/raw.txt")


def test_opaque_refs_survive_gateway_recreation(tmp_path: Path) -> None:
    grant = ArtifactGrant("wf", "task", "agent", "session")
    first = ArtifactGateway(tmp_path, grant)
    ref = first.persist_internal("checkpoint", "one", "recoverable")
    second = ArtifactGateway(tmp_path, grant)
    assert second.open_internal(ref).content == "recoverable"
