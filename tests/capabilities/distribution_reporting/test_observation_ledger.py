"""A routed source observation is judged once, by the subsection the ownership rules name."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from manyselves.capabilities.distribution_reporting.runtime.assessment_context import (
    normalize_assessments,
    part_assessment,
)
from manyselves.capabilities.distribution_reporting.runtime.joint_dossiers import (
    build_joint_dossiers,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ObservationAssessment,
    PartAssessment,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.observation_ledger import (
    build_observation_ledger,
    check_ledger,
    observation_relations,
)
from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
    prepare_module_fact_context,
)
from tests.capabilities.distribution_reporting.test_project_fact_packet import _write_facts


def _load_row(evidence_id: str, module_id: str, submodule_id: str, row: int) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id, subject=f"1#配电房/G{row}", fact="运行电流=120A；变压器容量=630kVA",
        value=19.0, unit="%", needs_confirmation=True,
        module_id=module_id, submodule_id=submodule_id,
        source=SourceLocation(file_id="s4-4", path=Path("Inputs/S4-4.xlsx"), sheet="负荷",
                              cell=f"C{row}:E{row}", row=row, column="C"),
    )


def _items() -> list[EvidenceItem]:
    # Mapper order: 2.4.1.1 first (load rate), then the 2.1.1 copy.
    return [
        _load_row("E-0001", "2.4", "2.4.1.1", 5),
        _load_row("E-0002", "2.1", "2.1.1", 5),
        _load_row("E-0003", "2.4", "2.4.1.1", 6),
        _load_row("E-0004", "2.1", "2.1.1", 6),
    ]


def test_ownership_rules_pick_the_owner_over_mapper_order(tmp_path):
    # Load rate belongs to 2.1.1 (负荷分配与过载风险) although the mapper lists 2.4.1.1 first.
    _write_facts(tmp_path, _items())
    ledger = build_observation_ledger(tmp_path, "facts")
    assert [group.observation_id for group in ledger.groups] == ["O-0001", "O-0002"]
    assert ledger.groups[0].owner.evidence_id == "E-0002"
    assert [route.evidence_id for route in ledger.groups[0].references] == ["E-0001"]
    assert ledger.groups[0].exact_routed_copy
    assert ledger.owner_for_reference("E-0001").submodule_id == "2.1.1"
    assert ledger.owner_for_reference("E-0002") is None


def test_unlisted_route_sets_fall_back_to_the_first_mapper_route(tmp_path):
    items = [
        _load_row("E-0001", "2.3", "2.3.1", 5),
        _load_row("E-0002", "2.4", "2.4.2.4", 5),
    ]
    _write_facts(tmp_path, items)
    ledger = build_observation_ledger(tmp_path, "facts")
    assert ledger.groups[0].owner.evidence_id == "E-0001"


def test_every_copy_is_marked_and_each_place_may_judge_its_copy(tmp_path):
    _write_facts(tmp_path, _items())
    owner_packet = prepare_module_fact_context(tmp_path, "facts", "2.1").inline_context
    other_packet = prepare_module_fact_context(tmp_path, "facts", "2.4").inline_context
    assert '"shared":"O-0001；负责=本小节；另见=2.4.1.1/E-0001"' in owner_packet
    assert '"shared":"O-0001；负责=2.1.1/E-0002；另见=2.1.1/E-0002"' in other_packet
    assert "按本小节的角度判断" in other_packet

    expected = part_assessment(tmp_path, "facts", "2.4", "2.4.1.1", {}).expected_evidence_ids
    assert {"E-0001", "E-0003"} <= set(expected)
    record = {"evidence_ids": ["E-0001"], "disposition": "concern", "narrative_anchor": "负荷"}
    assert normalize_assessments([record], ["E-0001"], set())[0]["evidence_ids"] == ["E-0001"]


def _module(part_id: str, records: list[dict], narrative: str = "") -> SimpleNamespace:
    # The checks read only assessments and prose; a full ModuleSubmission adds nothing here.
    return SimpleNamespace(
        assessments={part_id: PartAssessment(records=[
            ObservationAssessment.model_validate(record) for record in records
        ])},
        submodule_narratives={part_id: narrative},
    )


def test_program_checks_report_unjudged_duplicate_and_conflicting_observations(tmp_path):
    _write_facts(tmp_path, _items())
    ledger = build_observation_ledger(tmp_path, "facts")
    modules = {
        "2.1": _module("2.1.1", [
            {"evidence_ids": ["E-0002"], "disposition": "no_concern", "narrative_anchor": "负荷"},
        ]),
        "2.4": _module("2.4.1.1", [
            {"evidence_ids": ["E-0001"], "disposition": "concern", "narrative_anchor": "负荷"},
        ]),
    }
    report = check_ledger(ledger, modules)
    assert [item["observation_id"] for item in report["owner_unjudged"]] == ["O-0002"]
    assert [item["observation_id"] for item in report["judged_in_several_places"]] == ["O-0001"]
    assert report["conflicting_dispositions"][0]["dispositions"] == ["concern", "no_concern"]

    relations = observation_relations(
        ledger, modules, {item.id: item.subject for item in _items()},
    )
    assert relations and relations[0]["subject"] == "1#配电房/G5"


def test_joint_dossiers_keep_objects_whose_module_judgments_differ(tmp_path):
    _write_facts(tmp_path, _items())
    modules = {
        "2.1": _module("2.1.1", [
            {"evidence_ids": ["E-0002"], "disposition": "concern", "narrative_anchor": "负荷偏高"},
        ], "G5回路负荷偏高，需复核。"),
        "2.4": _module("2.4.1.1", [
            {"evidence_ids": ["E-0001"], "disposition": "undetermined", "narrative_anchor": "无法判断"},
            {"evidence_ids": ["E-0003"], "disposition": "no_concern", "narrative_anchor": "正常"},
        ], "G5无法判断负荷率。G6正常。"),
    }
    dossiers = build_joint_dossiers(tmp_path, "facts", modules)
    assert [dossier["subject"] for dossier in dossiers] == ["1#配电房/G5"]
    dossier = dossiers[0]
    assert dossier["dossier_id"] == "D-001"
    assert dossier["module_ids"] == ["2.1", "2.4"]
    assert set(dossier["reasons"]) == {"modules_disagree", "several_modules_raise_attention"}
    assert {fact["evidence_id"] for fact in dossier["facts"]} == {"E-0001", "E-0002"}
    assert any("负荷偏高" in judgment["excerpt"] for judgment in dossier["judgments"])
    assert all("shared" in fact for fact in dossier["facts"])
