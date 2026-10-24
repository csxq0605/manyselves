"""Complete facts must reach authors and auditors without a keyword query."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.module_authoring_preparation import (
    build_module_dispatch,
    prepare_module_authoring,
)
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import (
    ProjectEvidenceIndex,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from tests.capabilities.distribution_reporting.test_module_runtime import (
    _author_state,
    _write_author_skill_fixture,
)


def _facts(count: int = 14) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            id=f"E-{i:04d}", subject=f"设备{i}",
            fact=f"实测剩余电流={11.3 if i == count else 0.1}A",
            value=11.3 if i == count else 0.1, unit="A",
            needs_confirmation=i == count,
            module_id="2.4", submodule_id="2.4.3.1",
            source=SourceLocation(file_id="input-1", path=Path("Inputs/检测.xlsx"),
                                  sheet="测点", cell=f"J{i}", row=i, column="J"),
        )
        for i in range(1, count + 1)
    ]


def _write_facts(workspace: Path, items: list[EvidenceItem]) -> dict:
    _write_author_skill_fixture(workspace)
    state = _author_state(workspace, "facts")
    (workspace / state["preparation_refs"]["evidence"]).write_text(
        "\n".join(item.model_dump_json() for item in items) + "\n", encoding="utf-8",
    )
    return state


def test_dispatch_delivers_all_fourteen_facts_and_registers_uncited_sources(tmp_path):
    items = _facts()
    state = _write_facts(tmp_path, items)
    dispatch = build_module_dispatch(state, workspace=tmp_path, store=ReportingStore(tmp_path))
    envelope = dispatch.module_tasks[0]
    assert all(item.id in envelope.inline_context for item in items)
    assert "11.3" in envelope.inline_context
    assert "needs_confirmation" in envelope.inline_context
    assert "J14" in envelope.inline_context
    assert {record.id for record in SourceLedger(tmp_path, "facts").records} >= {
        item.id for item in items
    }
    packet_ref = next(ref for ref in envelope.input_refs if ref.endswith("-facts.md"))
    assert "E-0014" in (tmp_path / packet_ref).read_text(encoding="utf-8")
    # The actual author preparation must not discard the dispatch's fact ref.
    prepared = prepare_module_authoring(
        "2.4", state, "module-report", workspace=tmp_path, store=ReportingStore(tmp_path),
    )
    assert packet_ref in prepared.envelope.input_refs
    assert prepared.envelope.artifact_delivery_modes[packet_ref] == "reference"
    assert "E-0014" in prepared.envelope.inline_context
    assert len(prepared.envelope.input_refs) == len(set(prepared.envelope.input_refs))


def test_large_packet_is_explicitly_referenced_without_silently_dropping_tail(tmp_path):
    items = _facts()
    items[0] = items[0].model_copy(update={"fact": "长原文" * 20000})
    state = _write_facts(tmp_path, items)
    envelope = build_module_dispatch(
        state, workspace=tmp_path, store=ReportingStore(tmp_path),
    ).module_tasks[0]
    assert 'delivery_mode="reference"' in envelope.inline_context
    assert "完整" in envelope.inline_context
    packet_ref = next(ref for ref in envelope.input_refs if ref.endswith("-facts.md"))
    assert "E-0014" in (tmp_path / packet_ref).read_text(encoding="utf-8")
    assert len(envelope.inline_context) < 40000
    prepare_module_authoring(
        "2.4", state, "module-report", workspace=tmp_path, store=ReportingStore(tmp_path),
    )
    part = tmp_path / "Work/runs/facts/drafts/module-2.4/r0/2.4.1.1.md"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_text("## 2.4.1.1 已有正文\n待复核。", encoding="utf-8")
    state["resume"] = True
    resumed = prepare_module_authoring(
        "2.4", state, "module-report", workspace=tmp_path, store=ReportingStore(tmp_path),
    )
    assert packet_ref in resumed.envelope.input_refs
    assert "open_artifact" in resumed.envelope.allowed_tools


def test_template_instructions_are_not_customer_search_hits(tmp_path):
    items = _facts(1)
    items.append(EvidenceItem(
        id="E-9999", subject="SKILL.md", fact="实测剩余电流 写作方法",
        source=SourceLocation(file_id="skill", path=Path(
            "Inputs/report-template-role-skills/author-2.4/SKILL.md")),
    ))
    _write_facts(tmp_path, items)
    hits = ProjectEvidenceIndex(tmp_path, "facts").search("实测剩余电流", 12)
    assert [item.id for item in hits] == ["E-0001"]


def test_reviewer_receives_evidence_that_author_did_not_cite(tmp_path):
    from types import SimpleNamespace

    from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
        _review_evidence,
    )

    _write_facts(tmp_path, _facts())
    subject = SimpleNamespace(module_id="2.4", claims=[])
    excerpts = _review_evidence(tmp_path, subject, "facts", {"2.4.3.1"})
    assert len(excerpts) == 14
    assert json.loads(excerpts[-1].content)["needs_confirmation"] is True


def test_initial_review_contract_accepts_uncited_scope_evidence(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
        prepare_current_module_review,
    )
    from tests.capabilities.distribution_reporting.test_public_reporting_runtime import (
        _module_submission,
    )

    state = _write_facts(tmp_path, _facts())
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4", workflow_id="module-report", reporting_state=state,
        status="authored", module=_module_submission(),
    )
    prepared = prepare_current_module_review(context, store=ReportingStore(tmp_path))
    packet = prepared.review.prepared.review_input
    assert packet is not None
    assert {item.evidence_id for item in packet.evidence} == {item.id for item in _facts()}
    assert json.loads(packet.evidence[-1].content)["needs_confirmation"] is True


def test_packet_preserves_conflicts_unassigned_facts_and_exact_values(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
        render_fact_packet,
    )

    items = _facts(2)
    items[0] = items[0].model_copy(update={
        "value": 0, "fact": '实测=0\n备注="待复核"', "confidence": 0.4,
        "photo_refs": ["photo-1"], "observed_at": "2026-01-01",
    })
    # Same source coordinate, different observation: keep both, never dedupe by location.
    items[1] = items[1].model_copy(update={"source": items[0].source})
    unassigned = items[0].model_copy(update={
        "id": "E-0003", "module_id": None, "submodule_id": None,
    })
    other_module = items[0].model_copy(update={
        "id": "E-0004", "module_id": "2.3", "submodule_id": "2.3.3",
    })
    _write_facts(tmp_path, [*items, unassigned, other_module])
    scoped = ProjectEvidenceIndex(tmp_path, "facts").module_items("2.4", {"2.4.3.1"})
    assert [item.id for item in scoped] == ["E-0001", "E-0002", "E-0003"]
    text = render_fact_packet(scoped, "2.4", submodule_ids={"2.4.3.1"})
    rows = [json.loads(line) for line in text.splitlines() if line.startswith("[")]
    assert len(rows) == 3
    assert rows[0][3] == items[0].fact
    assert rows[0][4] == {"cell": "J1", "row": 1, "column": "J"}
    assert rows[0][5] == {
        "value": 0, "unit": "A", "confidence": 0.4,
        "photo_refs": ["photo-1"], "observed_at": "2026-01-01",
    }


def test_unavailable_snapshot_is_not_presented_as_missing_customer_data(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
        prepare_module_fact_context,
    )

    context = prepare_module_fact_context(tmp_path, "absent", "2.4")
    assert "快照不可用" in context.inline_context
    assert "不能据此认定客户未提供" in context.inline_context


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["_IndexedOpenArtifactTool", "_IndexedOpenToolResultTool"])
async def test_recovery_does_not_reuse_legacy_empty_read_markers(tool_name):
    from types import SimpleNamespace

    from manyselves.capabilities.distribution_reporting.runtime import module_provider_tools

    recorded = []
    index = SimpleNamespace(
        lookup=lambda *args, **kwargs: {
            "status": "completed", "result": {"status": "already_read", "content": ""},
        },
        record=lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    page = {"ref": "fact-ref", "content": "异常在最后一个测点", "next_offset": None}
    gateway = SimpleNamespace(
        open=lambda *args, **kwargs: SimpleNamespace(as_dict=lambda: page),
        open_internal=lambda *args, **kwargs: SimpleNamespace(as_dict=lambda: page),
    )
    tool = getattr(module_provider_tools, tool_name)(gateway, result_index=index, task_id="test")
    assert await tool("fact-ref") == page
    assert len(recorded) == 1
