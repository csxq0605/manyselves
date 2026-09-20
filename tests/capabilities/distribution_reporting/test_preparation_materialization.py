"""Characterization for the final preparation materialization tools."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    PreparationContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    ReportRequest,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.preparation_tools import (
    PreparationTools,
    build_preparation_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.definitions import DefinitionKind, ToolDefinition, WorkflowDefinition


def _context(
    run_id: str = "materialization-characterization",
    *,
    operation: str = "full_report",
) -> PreparationContext:
    request_kwargs = {
        "operation": operation,
        "instruction": "characterize preparation materialization",
    }
    if operation == "module_report":
        request_kwargs["target_modules"] = ["2.1"]
    return PreparationContext(
        run_id=run_id,
        request=ReportRequest(**request_kwargs),
    )


def _tools(tmp_path: Path) -> PreparationTools:
    return PreparationTools(
        workspace=tmp_path,
        input_snapshot=SimpleNamespace(inventory_digest="digest", files=[]),
        snapshot_content=lambda source, target: (target, "unused", target),
        runtime_photo_ids=lambda _evidence, _photos: [],
        store=ReportingStore(tmp_path),
    )


def test_preparation_defines_special_topic_and_materialization_tools() -> None:
    _capability, registry = load_distribution_reporting_capability()
    definitions = {
        definition.id: definition
        for definition in registry.all(DefinitionKind.TOOL)
        if isinstance(definition, ToolDefinition)
    }
    for tool_id in ("load-special-topic-plan", "finalize-preparation"):
        definition = definitions[tool_id]
        assert definition.input_contract == "preparation_context"
        assert definition.output_contract == "preparation_context"
        assert definition.model_visible is False

    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting-preparation",
    )
    assert isinstance(workflow, WorkflowDefinition)
    actions = workflow.actions
    action_ids = [action["id"] for action in actions]
    assert action_ids.index("load-special-topic-plan") < action_ids.index(
        "persist-preparation-snapshot"
    )
    assert action_ids.index("persist-preparation-snapshot") < action_ids.index(
        "finalize-preparation"
    )


def test_full_report_loads_special_topic_plan_before_persist(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    (inputs / "专项问题分析.md").write_text(
        "# 专项问题分析\n\n## 4.1 运行约束\n\n检查运行约束。\n",
        encoding="utf-8",
    )

    from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
        RunInputSnapshotStore,
    )

    RunInputSnapshotStore(tmp_path).freeze(_context().run_id)
    (inputs / "专项问题分析.md").write_text("# 专项问题分析\n\n## 4.1 已修改的实时文件\n\n不能进入已冻结 Run。\n", encoding="utf-8")
    result = _tools(tmp_path).load_special_topic_plan(_context())

    assert result.special_topic_plan is not None
    assert result.special_topic_plan.source_ref == Path("Inputs/专项问题分析.md")
    assert [section.section_id for section in result.special_topic_plan.sections] == [
        "4.1"
    ]
    assert result.special_topic_plan.sections[0].title == "运行约束"

    unchanged = _tools(tmp_path).load_special_topic_plan(
        _context(operation="module_report")
    )
    assert unchanged.special_topic_plan is None


def test_finalize_materialization_writes_index_and_source_ledger_refs(
    tmp_path: Path,
) -> None:
    context = _context()
    evidence = EvidenceItem(
        id="E-0001",
        subject="设备",
        fact="存在可追溯观察",
        source=SourceLocation(file_id="file-a", path=Path("Inputs/a.txt")),
    )
    evidence_path = (
        tmp_path / "Work" / "runs" / context.run_id / "preparation" / "evidence.jsonl"
    )
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        evidence.model_dump_json() + "\n",
        encoding="utf-8",
    )
    context.evidence_items = [evidence]

    result = _tools(tmp_path).finalize_preparation(context)

    assert result.evidence_index_ref is not None
    assert (tmp_path / result.evidence_index_ref).is_file()
    assert result.source_ledger_ref == (
        f"Work/runs/{context.run_id}/ledgers/sources.json"
    )
    ledger = json.loads((tmp_path / result.source_ledger_ref).read_text(encoding="utf-8"))
    assert [record["id"] for record in ledger] == ["E-0001"]
    assert result.preparation_refs["evidence_index"] == result.evidence_index_ref
    assert result.preparation_refs["source_ledger"] == result.source_ledger_ref


def test_preparation_implementation_bundle_binds_materialization_tools(
    tmp_path: Path,
) -> None:
    implementations = build_preparation_tool_implementations(
        workspace=tmp_path,
        input_snapshot=SimpleNamespace(inventory_digest="digest", files=[]),
        snapshot_content=lambda source, target: (target, "unused", target),
        runtime_photo_ids=lambda _evidence, _photos: [],
        store=ReportingStore(tmp_path),
    )

    assert "load-special-topic-plan" in implementations
    assert "finalize-preparation" in implementations
