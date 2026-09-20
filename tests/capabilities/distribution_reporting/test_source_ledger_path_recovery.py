"""Existing evidence bytes stay authoritative across path serialization fixes."""

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
)
from manyselves.capabilities.distribution_reporting.runtime.research_tools import (
    OpenProjectSourceTool,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger


def _legacy_evidence(workspace: Path):
    item = EvidenceItem.model_validate(
        {
            "id": "E-0318",
            "subject": "measured current",
            "fact": "The measured current is 10 A.",
            "source": {"file_id": "file-1", "path": "Inputs/evidence.xlsx"},
        }
    )
    old_payload = item.model_dump(mode="json")
    old_payload["source"]["path"] = r"Inputs\evidence.xlsx"
    old_content = json.dumps(old_payload, ensure_ascii=False, separators=(",", ":"))
    ledger = SourceLedger(workspace, "same-run")
    original = ledger.register_project(
        item.id, item.subject, "Inputs/evidence.xlsx", old_content
    )
    return item, ledger, original, old_content


@pytest.mark.asyncio
async def test_open_project_source_reuses_pre_fix_windows_evidence(tmp_path: Path):
    item, ledger, original, old_content = _legacy_evidence(tmp_path)
    tool = OpenProjectSourceTool(tmp_path, ledger=ledger)
    tool.index.get = lambda _source_id: item

    result = await tool(source_id=item.id)

    assert result["evidence"]["source"]["path"] == "Inputs/evidence.xlsx"
    assert ledger.records == [original]
    persisted = (ledger.content_root / f"{item.id}.txt").read_text(encoding="utf-8")
    assert persisted == old_content
    assert ledger._digest(persisted) == original.content_sha256


def test_canonical_path_does_not_hide_changed_fact_or_locator(tmp_path: Path):
    item, ledger, original, old_content = _legacy_evidence(tmp_path)
    changed = item.model_copy(update={"fact": "The measured current is 20 A."})
    for locator, content in (
        ("Inputs/evidence.xlsx", changed.model_dump_json()),
        ("Inputs/another.xlsx", item.model_dump_json()),
    ):
        with pytest.raises(ValueError, match="conflicting project evidence id"):
            ledger.register_project(item.id, item.subject, locator, content)
    assert ledger.records == [original]
    assert (ledger.content_root / f"{item.id}.txt").read_text(encoding="utf-8") == old_content


def test_canonical_path_does_not_hide_changed_source_path(tmp_path: Path):
    item, ledger, original, old_content = _legacy_evidence(tmp_path)
    changed = item.model_dump(mode="json")
    changed["source"]["path"] = "Inputs/another.xlsx"

    with pytest.raises(ValueError, match="conflicting project evidence id"):
        ledger.register_project(
            item.id, item.subject, original.locator, json.dumps(changed)
        )

    assert ledger.records == [original]
    assert (ledger.content_root / f"{item.id}.txt").read_text(encoding="utf-8") == old_content


def test_canonical_path_does_not_replace_tampered_original(tmp_path: Path):
    item, ledger, original, old_content = _legacy_evidence(tmp_path)
    content_path = ledger.content_root / f"{item.id}.txt"
    content_path.write_text(old_content + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting project evidence id"):
        ledger.register_project(
            item.id, item.subject, original.locator, item.model_dump_json()
        )

    assert ledger.records == [original]
    assert content_path.read_text(encoding="utf-8") == old_content + "\n"
