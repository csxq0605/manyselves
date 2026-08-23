import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.adapters import (
    build_module_cohort_definition,
    build_module_lane_definitions,
    build_reporting_tail_definition,
    load_reporting_agents,
)
from manyselves.capabilities.distribution_reporting.runtime.models import preparation
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.core.reporting.config import (
    AgentDefinition as ReportingAgentDefinition,
)
from manyselves.core.reporting.config import (
    load_agent_definitions,
    load_packaged_agents,
)
from manyselves.core.reporting.declarative_reporting_runner import (
    _compile_reporting_runtime,
)
from manyselves.kernel.contracts import ContractValidationError, build_contract_catalog
from manyselves.kernel.definitions import ContractDefinition, DefinitionKind
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import EndWorkflowAction, WorkflowCompiler


def test_reporting_taxonomy_is_physically_owned_by_the_capability() -> None:
    module_name = (
        "manyselves.capabilities.distribution_reporting.domain.taxonomy"
    )
    taxonomy = import_module(module_name)

    assert taxonomy.SubmoduleDefinition.__module__ == module_name
    assert taxonomy.ModuleDefinition.__module__ == module_name
    assert taxonomy.REPORT_TAXONOMY["2.4"].id == "2.4"
    assert taxonomy.resolve_submodule.__module__ == module_name
    assert find_spec("manyselves.core.reporting" + ".taxonomy") is None


def test_reporting_models_are_physically_owned_by_the_capability() -> None:
    _, registry = load_distribution_reporting_capability()
    module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.models.reporting"
    )
    definition = registry.require(
        DefinitionKind.CONTRACT,
        "distribution_reporting_input",
    )
    assert isinstance(definition, ContractDefinition)
    assert definition.model == f"{module_name}:ReportRequest"
    reporting = import_module(module_name)
    model_names = (
        "ReportingModel",
        "SpecialTopicSectionRequirement",
        "SpecialTopicPlan",
        "SourceLocation",
        "UserSupplement",
        "ReportRequest",
        "EvidenceDecisionRequest",
        "RevisionRequest",
        "ScopeExpansionRequest",
        "PhotoAsset",
        "EvidenceItem",
        "CoverageStatus",
        "SubmoduleCoverageEntry",
        "CoverageEntry",
        "CoverageMatrix",
        "OutputArtifact",
    )
    for model_name in model_names:
        assert getattr(reporting, model_name).__module__ == module_name
    preparation_module = preparation.__name__
    for model_name in (
        "ManifestFile",
        "ProjectManifest",
        "ParsedArtifact",
        "FilePreparationResult",
        "MappingGap",
        "MappingResult",
    ):
        assert getattr(preparation, model_name).__module__ == preparation_module
    assert reporting.chapter_section_ids.__module__ == module_name
    assert "CrossDecisionPack" not in vars(reporting)
    assert "CrossDecisionPackView" not in vars(reporting)

    source = Path(reporting.__file__).read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all(
        not any(
            name.name.startswith("manyselves.core.reporting")
            for name in node.names
        )
        if isinstance(node, ast.Import)
        else not (node.module or "").startswith("manyselves.core.reporting")
        for node in imports
    )

    contracts = build_contract_catalog(registry)
    assert contracts["distribution_reporting_input"].json_schema() == (
        reporting.ReportRequest.model_json_schema()
    )


def test_core_reporting_does_not_reexport_capability_models_or_keep_a_shim() -> None:
    core_reporting = import_module("manyselves.core.reporting")

    assert find_spec("manyselves.core.reporting.models") is None
    for model_name in (
        "CoverageEntry",
        "CoverageMatrix",
        "CoverageStatus",
        "EvidenceItem",
        "OutputArtifact",
        "ParsedArtifact",
        "ProjectManifest",
        "ReportRequest",
        "UserSupplement",
        "SourceLocation",
    ):
        assert model_name not in vars(core_reporting)


def test_importing_capability_package_loads_only_the_definition_entrypoint() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    "import manyselves.capabilities.distribution_reporting as capability",
                    "print(json.dumps({",
                    "    'exports': sorted(capability.__all__),",
                    "    'reporting_modules': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "    'adapter_modules': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith(",
                    "            'manyselves.capabilities.distribution_reporting.adapters'",
                    "        )",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "exports": [
            "CAPABILITY_FILE",
            "CAPABILITY_ROOT",
            "load_distribution_reporting_capability",
        ],
        "reporting_modules": [],
        "adapter_modules": [],
    }


def test_distribution_reporting_capability_loads_all_definition_indexes() -> None:
    capability, registry = load_distribution_reporting_capability()

    assert capability.id == "distribution-reporting"
    assert capability.entrypoints == ["distribution-reporting"]
    assert capability.runtime == (
        "manyselves.capabilities.distribution_reporting.adapters.runtime:build_runtime_binding"
    )
    assert {definition.id for definition in registry.all(DefinitionKind.AGENT)} == {
        "chief-editor-auditor",
        "chief-editor",
        "citation-builder",
        "coverage-evaluator",
        "cross-module-reviewer",
        "docx-renderer",
        "evidence-auditor",
        "evidence-normalizer",
        "intake-parser",
        "main-agent",
        "manifest-builder",
        "module-2.1-specialist",
        "module-2.2-specialist",
        "module-2.3-specialist",
        "module-2.4-specialist",
        "module-2.5-specialist",
        "product-skill-maintainer",
        "project-delivery",
        "template-distiller",
    }
    assert {definition.id for definition in registry.all(DefinitionKind.WORKFLOW)} == {
        "distribution-chief-chapter-1-lane",
        "distribution-chief-chapter-3-lane",
        "distribution-chief-chapter-4-lane",
        "distribution-chief-chapter-cohort",
        "distribution-chief-chapter-lane",
        "distribution-cross-owner-cohort",
        "distribution-cross-owner-pipeline",
        "distribution-final-chapter-1-lane",
        "distribution-final-chapter-3-lane",
        "distribution-final-chapter-4-lane",
        "distribution-final-chapter-cohort",
        "distribution-final-chapter-lane",
        "distribution-final-chief-revision-1-lane",
        "distribution-final-chief-revision-3-lane",
        "distribution-final-chief-revision-4-lane",
        "distribution-final-chief-revision-cohort",
        "distribution-final-chief-revision-lane",
        "distribution-final-recheck-1-lane",
        "distribution-final-recheck-3-lane",
        "distribution-final-recheck-4-lane",
        "distribution-final-recheck-cohort",
        "distribution-final-recheck-lane",
        "distribution-final-review-cycle",
            "distribution-report-delivery",
            "distribution-reporting-preparation",
            "distribution-module-review-lane",
        "distribution-module-runtime-lane",
        "distribution-module-cohort",
        "distribution-reporting-tail",
        "distribution-reporting",
    }
    assert registry.all(DefinitionKind.TASK)
    assert registry.all(DefinitionKind.CONTRACT)
    assert registry.all(DefinitionKind.TOOL)
    assert "continue-current-cross-owner-pipeline" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert "continue-current-final-review" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert "run-reporting-delivery" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert "run-reporting-cross" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert {definition.id for definition in registry.all(DefinitionKind.RECOVERY)} == {
        "current-reporting-recovery"
    }
    assert {
        definition.recovery for definition in registry.all(DefinitionKind.TASK)
    } == {"current-reporting-recovery"}
    assert registry.all(DefinitionKind.GATE) == ()
    assert {definition.id for definition in registry.all(DefinitionKind.INTERACTION)} == {
        "cross-owner-main-exception-decision",
        "module-main-exception-decision",
    }


def test_distribution_reporting_task_tools_are_model_visible_definitions() -> None:
    _, registry = load_distribution_reporting_capability()
    contracts = build_contract_catalog(registry)
    tools = {
        definition.id: definition
        for definition in registry.all(DefinitionKind.TOOL)
    }

    for task in registry.all(DefinitionKind.TASK):
        for tool_id in task.tools:
            assert tool_id in tools, f"task {task.id} references unknown tool {tool_id}"
            assert tools[tool_id].model_visible is True, (
                f"task {task.id} exposes runtime-only tool {tool_id} to its agent"
            )
            if tool_id not in {"submit_result", "write_result_part"}:
                assert contracts[tools[tool_id].input_contract].json_schema() not in (
                    {},
                    {"type": "object"},
                ), f"model-visible tool {tool_id} requires a concrete input contract"


def test_module_runtime_contract_models_are_owned_by_the_capability() -> None:
    _, registry = load_distribution_reporting_capability()
    lane_module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.models.module_lane"
    )
    contract_models = {
        "declarative_module_authoring_agent_result": (
            lane_module_name,
            "DeclarativeModuleAuthoringAgentResult",
        ),
        "declarative_module_recheck_agent_result": (
            lane_module_name,
            "DeclarativeModuleRecheckAgentResult",
        ),
        "declarative_module_review_agent_result": (
            lane_module_name,
            "DeclarativeModuleReviewAgentResult",
        ),
        "declarative_module_revision_agent_result": (
            lane_module_name,
            "DeclarativeModuleRevisionAgentResult",
        ),
        "declarative_module_runtime_lane_context": (
            lane_module_name,
            "DeclarativeModuleRuntimeLaneContext",
        ),
        "declarative_module_lane_outcome": (
            "manyselves.capabilities.distribution_reporting.runtime.models."
            "module_cohort",
            "DeclarativeModuleLaneOutcome",
        ),
    }

    for contract_id, (module_name, model_name) in contract_models.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{module_name}:{model_name}"
        model = getattr(import_module(module_name), model_name)
        assert model.__module__ == module_name

    old_cohort_module = import_module(
        "manyselves.core.reporting.declarative_module_cohort"
    )
    assert "DeclarativeModuleLaneOutcome" not in vars(old_cohort_module)


def test_module_lane_outcome_contract_preserves_join_state() -> None:
    """Characterize the typed value consumed by Cohort resume and reduction."""

    _, registry = load_distribution_reporting_capability()
    definition = registry.require(
        DefinitionKind.CONTRACT,
        "declarative_module_lane_outcome",
    )
    assert isinstance(definition, ContractDefinition)
    module_name, model_name = definition.model.split(":", maxsplit=1)
    model = getattr(import_module(module_name), model_name)

    outcome = model.model_validate(
        {
            "module_id": "2.1",
            "status": "deferred",
            "lane_state": {"status": "reviewer_exception_deferred"},
            "completion_ref": "completion-2.1",
            "completion": {"status": "deferred"},
            "retry_requested": True,
        }
    )

    assert outcome.model_dump(mode="json", exclude_none=True) == {
        "module_id": "2.1",
        "status": "deferred",
        "lane_state": {"status": "reviewer_exception_deferred"},
        "completion_ref": "completion-2.1",
        "completion": {"status": "deferred"},
        "retry_requested": True,
    }
    with pytest.raises(ValueError):
        model.model_validate(
            {
                "module_id": "2.1",
                "status": "completed",
                "unexpected": True,
            }
        )


def test_chief_chapter_contract_models_are_owned_by_the_capability() -> None:
    _, registry = load_distribution_reporting_capability()
    module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.models.chief_chapter"
    )
    contract_models = {
        "declarative_chief_chapter_agent_result": (
            "DeclarativeChiefChapterAgentResult"
        ),
        "declarative_chief_chapter_context": "DeclarativeChiefChapterContext",
        "declarative_chief_chapter_outcome": "DeclarativeChiefChapterOutcome",
    }

    for contract_id, model_name in contract_models.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{module_name}:{model_name}"
        model = getattr(import_module(module_name), model_name)
        assert model.__module__ == module_name
def test_core_chief_chapter_module_does_not_expose_capability_contract_models() -> None:
    old_module = import_module(
        "manyselves.core.reporting.declarative_chief_chapter_cohort"
    )

    assert "DeclarativeChiefChapterAgentResult" not in vars(old_module)
    assert "DeclarativeChiefChapterContext" not in vars(old_module)
    assert "DeclarativeChiefChapterOutcome" not in vars(old_module)


def test_chief_chapter_contract_models_preserve_lane_values() -> None:
    module = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.chief_chapter"
    )

    agent_result = module.DeclarativeChiefChapterAgentResult.model_validate(
        {"status": "failed", "error": "provider unavailable"}
    )
    context = module.DeclarativeChiefChapterContext.model_validate(
        {
            "chapter_id": "4",
            "status": "skipped",
            "output_ref": "Work/runs/run-chief/chapters/4.json",
        }
    )
    outcome = module.DeclarativeChiefChapterOutcome.model_validate(
        {
            "chapter_id": "4",
            "status": "skipped",
            "output_ref": "Work/runs/run-chief/chapters/4.json",
        }
    )

    assert agent_result.model_dump(mode="json", exclude_none=True) == {
        "status": "failed",
        "error": "provider unavailable",
    }
    expected_lane = {
        "chapter_id": "4",
        "status": "skipped",
        "output_ref": "Work/runs/run-chief/chapters/4.json",
    }
    assert context.model_dump(mode="json", exclude_none=True) == expected_lane
    assert outcome.model_dump(mode="json", exclude_none=True) == expected_lane
    for model, value in (
        (
            module.DeclarativeChiefChapterAgentResult,
            {"status": "failed", "unexpected": True},
        ),
        (
            module.DeclarativeChiefChapterContext,
            {"chapter_id": "4", "status": "skipped", "unexpected": True},
        ),
        (
            module.DeclarativeChiefChapterOutcome,
            {"chapter_id": "4", "status": "skipped", "unexpected": True},
        ),
    ):
        with pytest.raises(ValueError):
            model.model_validate(value)


def test_final_chapter_contract_models_are_owned_by_the_capability() -> None:
    _, registry = load_distribution_reporting_capability()
    module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.models.final_chapter"
    )
    contract_models = {
        "declarative_final_chapter_agent_result": (
            "DeclarativeFinalChapterAgentResult"
        ),
        "declarative_final_chapter_context": "DeclarativeFinalChapterContext",
        "declarative_final_chapter_outcome": "DeclarativeFinalChapterOutcome",
    }

    for contract_id, model_name in contract_models.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{module_name}:{model_name}"
        model = getattr(import_module(module_name), model_name)
        assert model.__module__ == module_name


def test_core_final_chapter_module_does_not_expose_capability_contract_models() -> None:
    old_module = import_module(
        "manyselves.core.reporting.declarative_final_chapter_cohort"
    )

    assert "DeclarativeFinalChapterAgentResult" not in vars(old_module)
    assert "DeclarativeFinalChapterContext" not in vars(old_module)
    assert "DeclarativeFinalChapterOutcome" not in vars(old_module)


def test_final_chapter_contract_models_preserve_lane_values() -> None:
    module = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.final_chapter"
    )

    agent_result = module.DeclarativeFinalChapterAgentResult.model_validate(
        {"status": "failed", "error": "auditor unavailable"}
    )
    context = module.DeclarativeFinalChapterContext.model_validate(
        {
            "chapter_id": "4",
            "status": "skipped",
            "output_ref": "Work/runs/run-final/reviews/chapter-4.json",
        }
    )
    outcome = module.DeclarativeFinalChapterOutcome.model_validate(
        {
            "chapter_id": "4",
            "status": "skipped",
            "output_ref": "Work/runs/run-final/reviews/chapter-4.json",
        }
    )

    assert agent_result.model_dump(mode="json", exclude_none=True) == {
        "status": "failed",
        "error": "auditor unavailable",
    }
    expected_lane = {
        "chapter_id": "4",
        "status": "skipped",
        "output_ref": "Work/runs/run-final/reviews/chapter-4.json",
    }
    assert context.model_dump(mode="json", exclude_none=True) == expected_lane
    assert outcome.model_dump(mode="json", exclude_none=True) == expected_lane
    for model, value in (
        (
            module.DeclarativeFinalChapterAgentResult,
            {"status": "failed", "unexpected": True},
        ),
        (
            module.DeclarativeFinalChapterContext,
            {"chapter_id": "4", "status": "skipped", "unexpected": True},
        ),
        (
            module.DeclarativeFinalChapterOutcome,
            {"chapter_id": "4", "status": "skipped", "unexpected": True},
        ),
    ):
        with pytest.raises(ValueError):
            model.model_validate(value)


def test_final_review_contract_models_are_owned_by_the_capability() -> None:
    _, registry = load_distribution_reporting_capability()
    module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.models.final_review"
    )
    contract_models = {
        "declarative_final_chief_revision_agent_result": (
            "DeclarativeFinalChiefRevisionAgentResult"
        ),
        "declarative_final_chief_revision_context": (
            "DeclarativeFinalChiefRevisionContext"
        ),
        "declarative_final_chief_revision_outcome": (
            "DeclarativeFinalChiefRevisionOutcome"
        ),
        "declarative_final_recheck_agent_result": (
            "DeclarativeFinalRecheckAgentResult"
        ),
        "declarative_final_recheck_context": "DeclarativeFinalRecheckContext",
        "declarative_final_recheck_outcome": "DeclarativeFinalRecheckOutcome",
        "declarative_final_review_cycle_context": "DeclarativeFinalReviewContext",
    }

    for contract_id, model_name in contract_models.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{module_name}:{model_name}"
        model = getattr(import_module(module_name), model_name)
        assert model.__module__ == module_name
    verdict_record = getattr(
        import_module(module_name),
        "DeclarativeFinalVerdictRecord",
    )
    assert verdict_record.__module__ == module_name


def test_core_final_review_module_does_not_expose_capability_contract_models() -> None:
    old_module = import_module(
        "manyselves.core.reporting.declarative_final_review_cycle"
    )

    for model_name in (
        "DeclarativeFinalChiefRevisionAgentResult",
        "DeclarativeFinalChiefRevisionContext",
        "DeclarativeFinalChiefRevisionOutcome",
        "DeclarativeFinalRecheckAgentResult",
        "DeclarativeFinalRecheckContext",
        "DeclarativeFinalRecheckOutcome",
        "DeclarativeFinalReviewContext",
        "DeclarativeFinalVerdictRecord",
    ):
        assert model_name not in vars(old_module)


def test_final_review_contract_models_preserve_round_state_and_forbid_extra() -> None:
    module = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.final_review"
    )
    edited_report = {
        "title": "Final review report",
        "assessment_background": "Assessment background.",
        "findings_overview": "Findings overview.",
        "regional_executive_summary": "Regional executive summary.",
        "module_narratives": {
            module_id: f"Approved narrative for {module_id}."
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        "risk_panorama": "Risk panorama.",
        "dimension_risk_analysis": "Dimension risk analysis.",
        "data_gap_analysis": "Data gap analysis.",
        "improvement_action_plan": "Improvement action plan.",
    }
    review = module.DeclarativeFinalReviewContext.model_validate(
        {
            "state": {"run_id": "run-final-review"},
            "current": edited_report,
            "subject_ref": "Work/runs/run-final-review/edited-revisions/chief-r1.json",
            "findings_by_chapter": {},
            "pending_by_chapter": {},
            "initial_lane_refs": {"1": "reviews/final-chapter-1.json"},
            "revision_number": 1,
            "verdict_history": [
                {
                    "submission": {
                        "run_id": "run-final-review",
                        "chapter_id": "1",
                        "checked_section_ids": ["1.1"],
                    },
                    "output_ref": "reviews/final-recheck-1-r1.json",
                }
            ],
        }
    )
    round_tripped = module.DeclarativeFinalReviewContext.model_validate_json(
        review.model_dump_json()
    )

    assert round_tripped.revision_number == 1
    assert round_tripped.verdict_history[0].output_ref == (
        "reviews/final-recheck-1-r1.json"
    )
    assert round_tripped.verdict_history[0].submission.chapter_id == "1"
    with pytest.raises(ValueError):
        module.DeclarativeFinalVerdictRecord.model_validate(
            {
                **review.verdict_history[0].model_dump(mode="json"),
                "unexpected": True,
            }
        )

    cases = (
        (
            module.DeclarativeFinalChiefRevisionAgentResult,
            {"status": "failed", "error": "chief unavailable"},
        ),
        (
            module.DeclarativeFinalChiefRevisionContext,
            {"chapter_id": "4", "status": "skipped"},
        ),
        (
            module.DeclarativeFinalChiefRevisionOutcome,
            {"chapter_id": "4", "status": "skipped", "parts": {"4.1": "body"}},
        ),
        (
            module.DeclarativeFinalRecheckAgentResult,
            {"status": "failed", "error": "auditor unavailable"},
        ),
        (
            module.DeclarativeFinalRecheckContext,
            {"chapter_id": "4", "status": "skipped"},
        ),
        (
            module.DeclarativeFinalRecheckOutcome,
            {"chapter_id": "4", "status": "skipped"},
        ),
    )
    for model, value in cases:
        parsed = model.model_validate(value)
        assert model.model_validate_json(parsed.model_dump_json()) == parsed
        with pytest.raises(ValueError):
            model.model_validate({**value, "unexpected": True})
    with pytest.raises(ValueError):
        module.DeclarativeFinalReviewContext.model_validate(
            {
                **review.model_dump(mode="json"),
                "unexpected": True,
            }
        )


def test_cross_owner_contract_models_are_owned_by_the_capability() -> None:
    _, registry = load_distribution_reporting_capability()
    module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.models.cross_owner"
    )
    contract_models = {
        "declarative_cross_owner_pipeline_outcome": (
            "DeclarativeCrossOwnerPipelineOutcome"
        ),
        "declarative_main_exception_user_input": (
            "DeclarativeMainExceptionUserInput"
        ),
    }

    for contract_id, model_name in contract_models.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{module_name}:{model_name}"
        model = getattr(import_module(module_name), model_name)
        assert model.__module__ == module_name


def test_core_cross_owner_module_does_not_expose_capability_contract_models() -> None:
    old_module = import_module(
        "manyselves.core.reporting.declarative_cross_owner_cohort"
    )

    assert "DeclarativeCrossOwnerPipelineOutcome" not in vars(old_module)
    assert "DeclarativeMainExceptionUserInput" not in vars(old_module)


def test_cross_owner_contract_models_preserve_pipeline_and_interaction_values() -> None:
    module = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.cross_owner"
    )

    outcome = module.DeclarativeCrossOwnerPipelineOutcome.model_validate(
        {
            "owner_module_id": "2.1",
            "status": "completed",
            "pipeline": {"completion_ref": "cross/2.1/completion.json"},
        }
    )
    user_input = module.DeclarativeMainExceptionUserInput.model_validate(
        {
            "decision": "return_to_author",
            "rationale": "The original author must address the remaining finding.",
        }
    )

    assert outcome.model_dump(mode="json", exclude_none=True) == {
        "owner_module_id": "2.1",
        "status": "completed",
        "pipeline": {"completion_ref": "cross/2.1/completion.json"},
    }
    assert user_input.model_dump(mode="json") == {
        "decision": "return_to_author",
        "rationale": "The original author must address the remaining finding.",
    }
    with pytest.raises(ValueError):
        module.DeclarativeCrossOwnerPipelineOutcome.model_validate(
            {
                "owner_module_id": "2.1",
                "status": "failed",
                "unexpected": True,
            }
        )
    with pytest.raises(ValueError):
        module.DeclarativeMainExceptionUserInput.model_validate(
            {
                "decision": "accept_dispute",
                "rationale": "Accepted at the declared evidence boundary.",
                "unexpected": True,
            }
        )


def test_pure_read_agent_tools_match_their_python_execution_metadata() -> None:
    _, registry = load_distribution_reporting_capability()

    for tool_id in ("calculate", "inspect_image", "open_artifact", "search_text"):
        tool = registry.require(DefinitionKind.TOOL, tool_id)
        assert tool.side_effect == "pure_read"
        assert tool.parallel_safe is True
        assert tool.model_visible is True

    for tool_id in ("inspect_image", "open_artifact", "search_text"):
        assert registry.require(DefinitionKind.TOOL, tool_id).reuse_result is True

    # open_tool_result reads only opaque references minted by the Runtime's
    # truncation boundary, so it deliberately has no Capability definition.
    assert "open_tool_result" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }


def test_capability_agents_project_to_the_current_reporting_contract() -> None:
    agents = load_reporting_agents()

    assert agents
    assert all(isinstance(agent, ReportingAgentDefinition) for agent in agents.values())
    assert agents["module-2.1-specialist"].tools == [
        "search_project_evidence",
        "open_project_source",
        "search_reference_library",
        "open_reference",
        "web_search",
        "open_web_source",
        "inspect_document",
        "inspect_image",
        "calculate",
        "publish_research_note",
        "query_peer",
        "reply_peer",
        "report_gap",
        "write_result_part",
        "list_result_parts",
            "report_blocked",
            "submit_result",
            "open_artifact",
            "search_text",
        ]
    assert "<role_and_perspective>" in agents["evidence-auditor"].instructions


def test_legacy_packaged_agent_loader_remains_a_compatibility_import() -> None:
    current = load_packaged_agents()
    projected = load_reporting_agents()

    assert current.keys() == projected.keys()
    for agent_id in current:
        assert current[agent_id].model_dump(exclude={"source_path"}) == projected[
            agent_id
        ].model_dump(exclude={"source_path"})


def test_capability_agent_projection_preserves_the_legacy_template_corpus() -> None:
    legacy = load_agent_definitions(
        Path(__file__).parents[3] / "manyselves/templates/reporting/agents"
    )
    projected = load_reporting_agents()

    assert legacy.keys() == projected.keys()
    for agent_id in legacy:
        legacy_payload = legacy[agent_id].model_dump(
            exclude={"source_path", "reads", "writes", "tools"}
        )
        projected_payload = projected[agent_id].model_dump(
            exclude={"source_path", "reads", "writes", "tools"}
        )
        assert legacy_payload == projected_payload
        assert set(legacy[agent_id].reads) <= set(projected[agent_id].reads)
        assert set(legacy[agent_id].writes) <= set(projected[agent_id].writes)
        assert set(legacy[agent_id].tools) <= set(projected[agent_id].tools)


def test_capability_adapters_expose_the_executable_reporting_definitions() -> None:
    _, packaged = load_distribution_reporting_capability()
    packaged_lane = packaged.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-review-lane",
    )
    packaged_cohort = packaged.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-cohort",
    )
    packaged_tail = packaged.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting-tail",
    )
    _, _, lane = build_module_lane_definitions("2.1")
    _, _, cohort = build_module_cohort_definition(max_concurrency=2)
    _, _, tail = build_reporting_tail_definition()

    assert packaged_lane.actions
    assert packaged_cohort.actions
    assert packaged_tail.actions
    assert lane.id == "distribution-module-2.1-review-lane"
    assert lane.actions[0]["tool"] == "build-module-initial-review-input"
    assert lane.actions[2]["id"] == "module-2.1-initial-review-r0"
    assert cohort.id == "distribution-module-cohort"
    assert (
        next(action for action in cohort.actions if action["id"] == "module-cohort")[
            "max_concurrency"
        ]
        == 2
    )
    initial_lane = next(action for action in cohort.actions if action["id"] == "execute-module-2.1")
    assert initial_lane["kind"] == "subworkflow"
    assert initial_lane["input_variables"] == {
        "reporting-state": "prepared-module-inputs",
        "module-id": "module-id-2.1",
        "lane-outcomes": "lane-outcomes",
    }
    assert tail.id == "distribution-reporting-tail"
    assert tail.actions == packaged_tail.actions


def test_legacy_midstage_compiler_does_not_replay_file_preparation() -> None:
    plan = _compile_reporting_runtime(
        {"run_id": "report-characterized"},
        full_report=True,
    )
    plan = plan.plan

    assert [action.kind for action in plan.actions] == [
        "subworkflow",
        "if",
        "subworkflow",
        "end_workflow",
    ]
    assert plan.workflow_ids == [
        "distribution-module-cohort",
        "distribution-reporting-tail",
    ]
    assert "distribution-module-2.1-runtime-lane" in plan.subworkflow_plans


def test_top_level_reporting_output_contract_is_typed_and_partial_safe() -> None:
    compiled = _compile_reporting_runtime(
        {"run_id": "report-contract-characterization"},
        full_report=False,
    )
    registry = compiled.definitions
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting",
    )
    plan = compiled.plan
    finish = next(
        action for action in plan.actions if isinstance(action, EndWorkflowAction)
    )

    assert workflow.output_contract == "distribution_reporting_output"
    assert workflow.input_variable == "reporting-state"
    assert plan.input_variable == "reporting-state"
    assert plan.final_output_contract == "distribution_reporting_output"
    assert finish.output_contract == "distribution_reporting_output"

    output_contract = build_contract_catalog(registry)[
        "distribution_reporting_output"
    ]
    assert output_contract.validate({"run_id": "report-partial"}) == {
        "run_id": "report-partial"
    }
    completed = {
        "run_id": "report-complete",
        "delivery_completion_ref": "Work/runs/report-complete/delivery.json",
        "delivery_status": "delivered",
        "output_artifacts": [
            {
                "kind": "module",
                "path": "Outputs/Modules/2.1.md",
                "module_id": "2.1",
            },
            {
                "kind": "report",
                "path": "Outputs/Reports/report.docx",
            },
        ],
        "existing_reporting_state": {"remains": "compatible"},
    }
    assert output_contract.validate(completed) is completed
    with pytest.raises(ContractValidationError):
        output_contract.validate({"delivery_status": "delivered"})
    with pytest.raises(ContractValidationError):
        output_contract.validate(
            {
                "run_id": "report-invalid-artifact",
                "output_artifacts": [{"kind": "report"}],
            }
        )


def test_reporting_tail_state_requires_only_the_run_identity() -> None:
    _, registry = load_distribution_reporting_capability()
    tail_contract = build_contract_catalog(registry)["reporting_tail_state"]

    assert tail_contract.validate({"run_id": "report-tail-partial"}) == {
        "run_id": "report-tail-partial"
    }
    with pytest.raises(ContractValidationError):
        tail_contract.validate({"module_submissions": {}})


def test_production_delivery_is_a_typed_render_publish_completion_subworkflow() -> None:
    registry, _, tail = build_reporting_tail_definition()
    run_delivery = next(action for action in tail.actions if action["id"] == "run-delivery")
    assert run_delivery == {
        "id": "run-delivery",
        "kind": "subworkflow",
        "workflow": "distribution-report-delivery",
        "input_variables": {"reporting-state": "reporting-state"},
        "child_output_name": "result",
        "output_variable": "reporting-state",
    }

    delivery = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-report-delivery",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        delivery,
        registry,
    )

    assert delivery.gates == []
    assert [action.kind for action in plan.actions] == [
        "invoke_tool",
        "invoke_tool",
        "invoke_tool",
        "end_workflow",
    ]
    assert [action.id for action in plan.actions] == [
        "prepare-render-delivery",
        "publish-materialize-delivery",
        "complete-delivery",
        "finish-report-delivery",
    ]
    assert plan.workflow_ids == []
    assert plan.tool_ids == [
        "prepare-render-delivery",
        "publish-materialize-delivery",
        "complete-delivery",
    ]
    assert all(action.kind not in {"if", "parallel", "join", "goto"} for action in plan.actions)

    for tool_id in plan.tool_ids:
        tool = registry.require(DefinitionKind.TOOL, tool_id)
        assert tool.input_contract == "reporting_tail_state"
        assert tool.output_contract == "reporting_tail_state"
        assert tool.side_effect == "ordered_state"
        assert tool.model_visible is False


def test_production_module_runtime_lane_declares_its_lifecycle_steps() -> None:
    registry, _, _ = build_module_cohort_definition(max_concurrency=2)
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-2.1-runtime-lane",
    )

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    assert [action.kind for action in plan.actions] == [
        action["kind"] for action in workflow.actions
    ]
    action_ids = [action.id for action in plan.actions]
    assert action_ids == [action["id"] for action in workflow.actions]
    recheck_start = action_ids.index("prepare-current-module-recheck")
    assert action_ids[recheck_start : recheck_start + 9] == [
        "prepare-current-module-recheck",
        "module-recheck-preflight-needs-revision",
        "choose-module-recheck-preflight",
        "prepare-current-module-recheck-preflight-revision",
        "create-module-recheck-preflight-revision-conversation",
        "invoke-current-module-recheck-preflight-revision",
        "accept-current-module-recheck-preflight-revision",
        "continue-after-module-recheck-preflight-revision",
        "module-recheck-requires-agent",
    ]
    assert plan.tool_ids == [
        "start-current-module-lane",
        "module-lane-retries-preflight-revision",
        "accept-current-module-preflight-revision",
        "module-preflight-revision-needs-recheck",
        "module-lane-has-deferred-main-exception",
        "prepare-current-module-main-exception",
        "module-main-exception-requires-agent",
        "accept-current-module-main-exception",
        "module-main-exception-requests-user",
        "apply-current-module-main-exception-user-input",
        "route-current-module-after-main-exception",
        "prepare-current-module-authoring",
        "module-authoring-requires-agent",
        "accept-current-module-authoring",
        "resume-current-module-authoring",
        "module-lane-can-review",
        "prepare-current-module-review",
        "module-review-preflight-needs-revision",
        "prepare-current-module-preflight-revision",
        "module-review-requires-agent",
        "resume-current-module-review",
        "accept-current-module-review",
        "module-review-needs-recheck",
        "module-review-needs-revision",
        "prepare-current-module-revision",
        "accept-current-module-revision",
        "prepare-current-module-author-exception",
        "prepare-current-module-recheck",
        "module-recheck-requires-agent",
        "resume-current-module-recheck",
        "accept-current-module-recheck",
        "complete-current-module-lane",
    ]
    assert plan.agent_ids == [
        "module-2.1-specialist",
        "main-agent",
        "evidence-auditor",
    ]
    assert plan.task_ids == [
        "module-2.1-runtime-revision",
        "module-runtime-main-exception",
        "module-2.1-authoring",
        "module-runtime-initial-review",
        "module-runtime-recheck",
    ]
    assert plan.interaction_ids == ["module-main-exception-decision"]
    assert (
        next(
            action for action in plan.actions if action.id == "continue-after-module-recheck"
        ).target
        == "module-recheck-exception-is-deferred"
    )
    assert "execute-current-module-lane" not in plan.tool_ids
    assert "review-current-module-lane" not in plan.tool_ids


def test_production_cross_is_an_owner_cohort_subworkflow() -> None:
    """Characterize the next Cross boundary before its implementation exists."""

    registry, _, tail = build_reporting_tail_definition()
    run_cross = next(action for action in tail.actions if action["id"] == "run-cross")
    assert run_cross["kind"] == "subworkflow"
    assert run_cross["workflow"] == "distribution-cross-owner-cohort"

    cohort = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-cohort",
    )
    cohort_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        cohort,
        registry,
    )
    parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
    assert set(parallel.branches) == set(REPORT_MODULE_IDS)

    branch_workflows = {}
    for module_id, branch_id in parallel.branches.items():
        branch = next(action for action in cohort_plan.actions if action.id == branch_id)
        assert branch.kind == "subworkflow"
        expected_workflow_id = f"distribution-cross-owner-{module_id}-pipeline"
        assert branch.workflow == expected_workflow_id
        branch_workflows[module_id] = registry.require(
            DefinitionKind.WORKFLOW,
            expected_workflow_id,
        )

    join = next(action for action in cohort_plan.actions if action.kind == "join")
    assert join.parallel == parallel.id
    assert set(join.inputs) == set(REPORT_MODULE_IDS)

    for module_id, pipeline in branch_workflows.items():
        pipeline_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            pipeline,
            registry,
        )
        assert pipeline_plan.agent_ids == [
            "cross-module-reviewer",
            f"module-{module_id}-specialist",
            "main-agent",
            "evidence-auditor",
        ]
        assert pipeline_plan.task_ids == [
            "cross-owner-runtime-initial-review",
            f"cross-owner-module-{module_id}-revision-r1",
            "cross-owner-runtime-main-exception",
            "cross-owner-runtime-local-review",
            "cross-owner-runtime-recheck",
        ]
        assert pipeline_plan.tool_ids == [
            "prepare-current-cross-owner-initial",
            "cross-owner-initial-requires-agent",
            "accept-current-cross-owner-initial",
            "cross-owner-initial-has-findings",
            "prepare-current-cross-owner-revision",
            "cross-owner-revision-requires-agent",
            "accept-current-cross-owner-revision",
            "prepare-current-cross-owner-author-exception",
            "cross-owner-main-exception-requires-agent",
            "accept-current-cross-owner-main-exception",
            "cross-owner-main-exception-requests-user",
            "apply-current-cross-owner-main-exception-user-input",
            "cross-owner-author-exception-returns-to-author",
            "prepare-current-cross-owner-local-review",
            "cross-owner-local-review-requires-agent",
            "accept-current-cross-owner-local-review",
            "prepare-current-cross-owner-recheck",
            "cross-owner-recheck-requires-agent",
            "accept-current-cross-owner-recheck",
            "prepare-current-cross-owner-reviewer-exception",
            "advance-current-cross-owner-round",
            "cross-owner-round-needs-revision",
            "complete-current-cross-owner-pipeline",
            "complete-current-cross-owner-without-findings",
        ]
        conversation = next(
            action
            for action in pipeline_plan.actions
            if action.id == "create-cross-owner-conversation"
        )
        assert conversation.conversation_key == f"cross-owner-{module_id}"


def test_production_chief_is_a_chapter_cohort_subworkflow() -> None:
    """Chief chapter Agents and their Join must be owned by files."""

    registry, _, tail = build_reporting_tail_definition()
    run_chief = next(action for action in tail.actions if action["id"] == "run-chief")
    assert run_chief["kind"] == "subworkflow"
    assert run_chief["workflow"] == "distribution-chief-chapter-cohort"

    cohort = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-chief-chapter-cohort",
    )
    cohort_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        cohort,
        registry,
    )
    parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
    assert set(parallel.branches) == {"1", "3", "4"}
    join = next(action for action in cohort_plan.actions if action.kind == "join")
    assert join.parallel == parallel.id
    assert set(join.inputs) == {"1", "3", "4"}

    for chapter_id, branch_id in parallel.branches.items():
        branch = next(action for action in cohort_plan.actions if action.id == branch_id)
        assert branch.kind == "subworkflow"
        workflow_id = f"distribution-chief-chapter-{chapter_id}-lane"
        assert branch.workflow == workflow_id
        lane = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        lane_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            lane,
            registry,
        )
        assert lane_plan.agent_ids == ["chief-editor"]
        assert lane_plan.task_ids == ["chief-chapter-edit"]
        conversation = next(
            action for action in lane_plan.actions if action.kind == "create_conversation"
        )
        assert conversation.conversation_key == f"chief-chapter-{chapter_id}"
        assert "run-reporting-chief" not in lane_plan.tool_ids


def test_production_final_is_a_chapter_auditor_cohort_subworkflow() -> None:
    """Characterize the file-owned Final initial chapter wave."""

    registry, _, tail = build_reporting_tail_definition()
    run_final = next(action for action in tail.actions if action["id"] == "run-final")
    assert run_final["kind"] == "subworkflow"
    assert run_final["workflow"] == "distribution-final-chapter-cohort"

    cohort = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-chapter-cohort",
    )
    cohort_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        cohort,
        registry,
    )
    parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
    assert set(parallel.branches) == {"1", "3", "4"}
    join = next(action for action in cohort_plan.actions if action.kind == "join")
    assert join.parallel == parallel.id
    assert set(join.inputs) == {"1", "3", "4"}

    for chapter_id, branch_id in parallel.branches.items():
        branch = next(action for action in cohort_plan.actions if action.id == branch_id)
        assert branch.kind == "subworkflow"
        workflow_id = f"distribution-final-chapter-{chapter_id}-lane"
        assert branch.workflow == workflow_id
        lane = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        lane_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            lane,
            registry,
        )
        assert lane_plan.agent_ids == ["chief-editor-auditor"]
        assert lane_plan.task_ids == ["final-chapter-review"]
        conversation = next(
            action for action in lane_plan.actions if action.kind == "create_conversation"
        )
        assert conversation.agent == "chief-editor-auditor"
        assert conversation.conversation_key == f"final-chapter-{chapter_id}"
        invoke = next(action for action in lane_plan.actions if action.kind == "invoke_agent")
        assert invoke.agent == "chief-editor-auditor"
        assert invoke.task == "final-chapter-review"
        assert "run-reporting-final" not in lane_plan.tool_ids

    cycle = next(action for action in cohort_plan.actions if action.id == "run-final-review-cycle")
    assert cycle.kind == "subworkflow"
    assert cycle.workflow == "distribution-final-review-cycle"
    assert cycle.input_variables == {
        "reporting-state": "completed-final-state",
        "initial-outcomes": "final-chapter-outcomes",
    }
    assert all(
        action.kind != "invoke_tool" or action.tool != "continue-current-final-review"
        for action in cohort_plan.actions
    )


def test_production_final_review_is_an_affected_only_revision_recheck_cycle() -> None:
    """Characterize the file-owned Final revision and recheck loop."""

    registry, _, _ = build_reporting_tail_definition()
    compiler = WorkflowCompiler(build_builtin_executor_registry())
    cycle = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-review-cycle",
    )
    cycle_plan = compiler.compile(cycle, registry)

    assert cycle.gates == []
    assert cycle.max_iterations is None
    assert cycle_plan.workflow_ids == [
        "distribution-final-chief-revision-cohort",
        "distribution-final-recheck-cohort",
    ]
    assert cycle_plan.tool_ids == [
        "start-final-review-cycle",
        "final-review-needs-round",
        "advance-final-review-round",
        "complete-final-review",
    ]
    entry = next(
        action for action in cycle_plan.actions if action.id == "choose-final-review-entry"
    )
    assert entry.then == "advance-final-review-round"
    assert entry.otherwise == "complete-final-review"
    after_recheck = next(
        action for action in cycle_plan.actions if action.id == "choose-final-review-next-step"
    )
    assert after_recheck.then == "advance-final-review-round"
    assert after_recheck.otherwise == "complete-final-review"

    for cohort_id, parallel_id, lane_prefix, expected_task, expected_agent in [
        (
            "distribution-final-chief-revision-cohort",
            "final-chief-revision-cohort",
            "distribution-final-chief-revision-",
            "final-chief-chapter-revision",
            "chief-editor",
        ),
        (
            "distribution-final-recheck-cohort",
            "final-recheck-cohort",
            "distribution-final-recheck-",
            "final-chapter-recheck",
            "chief-editor-auditor",
        ),
    ]:
        cohort = registry.require(DefinitionKind.WORKFLOW, cohort_id)
        cohort_plan = compiler.compile(cohort, registry)
        parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
        assert parallel.id == parallel_id
        assert set(parallel.branches) == {"1", "3", "4"}
        join = next(action for action in cohort_plan.actions if action.kind == "join")
        assert join.parallel == parallel.id
        assert set(join.inputs) == {"1", "3", "4"}
        for chapter_id, branch_id in parallel.branches.items():
            branch = next(action for action in cohort_plan.actions if action.id == branch_id)
            assert branch.workflow == f"{lane_prefix}{chapter_id}-lane"
            lane = registry.require(DefinitionKind.WORKFLOW, branch.workflow)
            lane_plan = compiler.compile(lane, registry)
            assert lane_plan.agent_ids == [expected_agent]
            assert lane_plan.task_ids == [expected_task]
            conversation = next(
                action for action in lane_plan.actions if action.kind == "create_conversation"
            )
            expected_conversation = (
                f"chief-chapter-{chapter_id}"
                if expected_agent == "chief-editor"
                else f"final-chapter-{chapter_id}"
            )
            assert conversation.conversation_key == expected_conversation
            assert conversation.agent == expected_agent

        reducer = next(action for action in cohort_plan.actions if action.kind == "invoke_tool")
        assert reducer.tool in {
            "reduce-final-chief-revision-cohort",
            "reduce-final-recheck-cohort",
        }


def test_cross_owner_21_pipeline_declares_initial_reviewer_agent_boundary() -> None:
    _, registry = load_distribution_reporting_capability()
    # The owner specializations are registered by the same loader used by the
    # production Cross compiler; this keeps the characterization on the
    # packaged definition rather than a hand-built test workflow.
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    assert [action.kind for action in plan.actions[:7]] == [
        "invoke_tool",
        "invoke_tool",
        "if",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "goto",
    ]
    assert plan.tool_ids[:3] == [
        "prepare-current-cross-owner-initial",
        "cross-owner-initial-requires-agent",
        "accept-current-cross-owner-initial",
    ]
    assert plan.agent_ids[0] == "cross-module-reviewer"
    assert plan.task_ids[0] == "cross-owner-runtime-initial-review"
    assert "execute-current-cross-owner-pipeline" not in plan.tool_ids

    prepare = plan.actions[0]
    assert prepare.tool == "prepare-current-cross-owner-initial"
    assert prepare.input_variables == {
        "state": "reporting-state",
        "owner_module_id": "owner-module-id",
    }
    route = plan.actions[1]
    assert route.tool == "cross-owner-initial-requires-agent"
    choose = plan.actions[2]
    create = plan.actions[3]
    assert choose.then == create.id
    assert choose.otherwise == "cross-owner-initial-has-findings"
    assert create.agent == "cross-module-reviewer"
    assert create.conversation_key == "cross-owner-2.1"

    invoke = plan.actions[4]
    assert invoke.agent == "cross-module-reviewer"
    assert invoke.task == "cross-owner-runtime-initial-review"
    assert invoke.conversation_variable == "cross-owner-conversation"
    assert invoke.input_variable == "owner-context"
    accept = plan.actions[5]
    assert accept.tool == "accept-current-cross-owner-initial"
    assert accept.input_variables == {
        "context": "owner-context",
        "result": "cross-owner-initial-agent-result",
    }
    assert "continue-current-cross-owner-pipeline" not in {action.id for action in plan.actions}


def test_cross_owner_21_pipeline_declares_owner_finding_revision_boundary() -> None:
    """Characterize the next Cross owner slice before its implementation exists."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    action_ids = [action.id for action in plan.actions]
    assert "cross-owner-initial-has-findings" in action_ids
    assert "prepare-current-cross-owner-revision" in action_ids
    assert "accept-current-cross-owner-revision" in action_ids
    assert "continue-current-cross-owner-pipeline" not in action_ids

    finding_route = next(
        action for action in plan.actions if action.id == "cross-owner-initial-has-findings"
    )
    assert finding_route.kind == "invoke_tool"
    revision_prepare = next(
        action for action in plan.actions if action.id == "prepare-current-cross-owner-revision"
    )
    assert revision_prepare.kind == "invoke_tool"
    revision_accept = next(
        action for action in plan.actions if action.id == "accept-current-cross-owner-revision"
    )
    assert revision_accept.kind == "invoke_tool"

    revision_agents = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent" and action.agent == "module-2.1-specialist"
    ]
    assert len(revision_agents) == 1
    revision_agent = revision_agents[0]
    assert "cross" in revision_agent.task
    assert "2.1" in revision_agent.task
    revision_conversation = next(
        action
        for action in plan.actions
        if action.kind == "create_conversation" and action.agent == "module-2.1-specialist"
    )
    assert revision_conversation.conversation_key == "module-2.1"
    assert revision_agent.conversation_variable == revision_conversation.output_variable


def test_cross_owner_21_pipeline_declares_original_auditor_local_regression() -> None:
    """Characterize the Cross revision-to-original-Auditor boundary."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    action_ids = [action.id for action in plan.actions]
    assert "prepare-current-cross-owner-local-review" in action_ids
    assert "accept-current-cross-owner-local-review" in action_ids
    assert "continue-current-cross-owner-pipeline" not in action_ids

    local_review_agents = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent"
        and action.agent == "evidence-auditor"
        and action.task == "cross-owner-runtime-local-review"
    ]
    assert len(local_review_agents) == 1
    local_review_conversation = next(
        action
        for action in plan.actions
        if action.kind == "create_conversation" and action.agent == "evidence-auditor"
    )
    assert local_review_conversation.conversation_key == "module-auditor-2.1"
    assert local_review_agents[0].conversation_variable == local_review_conversation.output_variable


def test_cross_owner_21_pipeline_declares_original_reviewer_recheck() -> None:
    """Characterize the first Cross owner recheck Agent boundary."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    action_ids = [action.id for action in plan.actions]
    assert "prepare-current-cross-owner-recheck" in action_ids
    assert "accept-current-cross-owner-recheck" in action_ids
    recheck_agents = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent"
        and action.agent == "cross-module-reviewer"
        and action.task == "cross-owner-runtime-recheck"
    ]
    assert len(recheck_agents) == 1
    recheck_conversation = next(
        action for action in plan.actions if action.id == "create-cross-owner-recheck-conversation"
    )
    assert recheck_conversation.conversation_key == "cross-owner-2.1"
    assert recheck_agents[0].conversation_variable == (recheck_conversation.output_variable)
    local_review_route = next(
        action for action in plan.actions if action.id == "choose-cross-owner-local-review-source"
    )
    assert local_review_route.otherwise == "prepare-current-cross-owner-recheck"


def test_cross_owner_21_pipeline_declares_repeated_recheck_round_route() -> None:
    """Characterize a declarative Cross regression-round loop before implementation."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    assert pipeline.gates == []
    assert plan.max_iterations is None
    assert all(action.kind != "gate" for action in plan.actions)

    action_ids = [action.id for action in plan.actions]
    recheck_continue = next(
        action for action in plan.actions if action.id == "continue-after-cross-owner-recheck"
    )
    assert recheck_continue.kind == "goto"
    assert recheck_continue.target == "prepare-current-cross-owner-reviewer-exception"

    round_advance = next(
        action for action in plan.actions if action.id == "advance-current-cross-owner-round"
    )
    assert round_advance.kind == "invoke_tool"
    assert round_advance.tool == "advance-current-cross-owner-round"
    round_needs_revision = next(
        action for action in plan.actions if action.id == "cross-owner-round-needs-revision"
    )
    assert round_needs_revision.kind == "invoke_tool"
    assert round_needs_revision.tool == "cross-owner-round-needs-revision"
    choose_next_step = next(
        action for action in plan.actions if action.id == "choose-cross-owner-next-step"
    )
    assert choose_next_step.kind == "if"
    assert choose_next_step.then == "prepare-current-cross-owner-revision"
    assert choose_next_step.otherwise == "complete-current-cross-owner-pipeline"
    assert "finish-cross-owner-pipeline" not in {
        choose_next_step.then,
        choose_next_step.otherwise,
    }
    assert action_ids.index(round_advance.id) < action_ids.index(choose_next_step.id)
    assert action_ids.index("prepare-current-cross-owner-revision") < action_ids.index(
        choose_next_step.id
    )


def test_cross_owner_21_pipeline_declares_no_finding_completion() -> None:
    """An ordinary no-finding owner must not enter the compatibility closure."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    choose_revision = next(
        action for action in plan.actions if action.id == "choose-cross-owner-revision"
    )
    assert choose_revision.otherwise == "complete-current-cross-owner-without-findings"
    completion = next(
        action
        for action in plan.actions
        if action.id == "complete-current-cross-owner-without-findings"
    )
    assert completion.kind == "invoke_tool"
    assert completion.tool == "complete-current-cross-owner-without-findings"
    assert completion.output_variable == "owner-outcome"
    assert all(action.kind != "gate" for action in plan.actions)


def test_cross_owner_21_pipeline_advances_recovered_recheck_verdict() -> None:
    """A persisted verdict must rejoin typed round advancement."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    choose_recheck = next(
        action for action in plan.actions if action.id == "choose-cross-owner-recheck-source"
    )
    assert choose_recheck.otherwise == "prepare-current-cross-owner-reviewer-exception"
    assert all(action.kind != "gate" for action in plan.actions)


def test_cross_owner_21_pipeline_declares_main_exception_agent_routes() -> None:
    """Author and reviewer exceptions use Main without a compatibility Tool."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    main_conversations = [
        action
        for action in plan.actions
        if action.kind == "create_conversation" and action.agent == "main-agent"
    ]
    main_invocations = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent" and action.agent == "main-agent"
    ]
    assert len(main_conversations) == 2
    assert {action.conversation_key for action in main_conversations} == {"main-cross-exception"}
    assert len(main_invocations) == 2
    assert {action.task for action in main_invocations} == {"cross-owner-runtime-main-exception"}
    assert "continue-current-cross-owner-pipeline" not in plan.tool_ids
    assert pipeline.gates == []
    assert plan.max_iterations is None
