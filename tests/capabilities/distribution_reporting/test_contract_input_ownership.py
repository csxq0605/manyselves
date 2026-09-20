import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import ContractDefinition, DefinitionKind

SUBMISSIONS_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.contracts.submissions"
)
INPUTS_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.inputs"
)

INPUT_CONTRACT_MODELS = {
    "module_review_input": "ModuleReviewInput",
    "module_revision_input": "ModuleRevisionInput",
    "cross_owner_input": "CrossOwnerInput",
    "chief_chapter_lane_input": "ChiefChapterLaneInput",
    "final_chapter_lane_input": "FinalChapterLaneInput",
}


def test_chief_revision_schema_distinguishes_patch_scope_from_body_part_ids() -> None:
    submissions = import_module(SUBMISSIONS_MODULE)
    schema = submissions.submission_schema("chief_chapter_lane_revision_submission")
    assert "changed" in schema["properties"]["section_ids"]["description"]
    assert "unchanged" in schema["properties"]["section_ids"]["description"]
    assert "changed_target_ids" in schema["description"]
    assert "3.2" in schema["description"]
    assert "improvement_action_plan" in schema["description"]
    example = schema["examples"][0]
    assert example["revision_responses"][0]["changed_target_ids"] == ["3.2"]
    submissions.submission_model(example["kind"]).model_validate(example)
    initial = submissions.submission_schema("chief_chapter_lane_submission")
    assert "Complete" in initial["properties"]["section_ids"]["description"]


def test_edited_report_example_satisfies_paired_special_topic_contract() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        EditedReportSubmission,
    )

    submissions = import_module(SUBMISSIONS_MODULE)
    example = submissions.submission_schema("edited_report_submission")["examples"][0]
    restored = EditedReportSubmission.model_validate(example)

    assert restored.special_topic_plan is None
    assert restored.special_topic_analysis is None
    assert set(restored.module_narratives) == {"2.1", "2.2", "2.3", "2.4", "2.5"}


def test_contract_and_input_modules_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {SUBMISSIONS_MODULE} as submissions",
                    f"import {INPUTS_MODULE} as inputs",
                    "print(json.dumps({",
                    "    'submissions': submissions.__name__,",
                    "    'inputs': inputs.__name__,",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "submissions": SUBMISSIONS_MODULE,
        "inputs": INPUTS_MODULE,
        "core_reporting": [],
    }


def test_contract_and_input_modules_are_physically_capability_owned() -> None:
    submissions = import_module(SUBMISSIONS_MODULE)
    inputs = import_module(INPUTS_MODULE)

    for function_name in (
        "submission_model",
        "submission_schema",
        "render_submission_contract",
        "undescribed_property_paths",
    ):
        assert getattr(submissions, function_name).__module__ == SUBMISSIONS_MODULE
    for function_name in (
        "input_contract_schema",
        "render_input_contract",
        "write_contract_manifest",
    ):
        assert getattr(inputs, function_name).__module__ == INPUTS_MODULE
    assert all(
        model_type.__module__ == INPUTS_MODULE
        for model_type in inputs.INPUT_CONTRACT_TYPES.values()
    )

    for module in (submissions, inputs):
        source = Path(module.__file__).read_text(encoding="utf-8")
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

    assert find_spec("manyselves.core.reporting") is None


def test_all_input_and_submission_contract_semantics_remain_complete() -> None:
    submissions = import_module(SUBMISSIONS_MODULE)
    inputs = import_module(INPUTS_MODULE)
    agentic = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.agentic"
    )

    assert set(submissions.KIND_EXAMPLES) == set(agentic.SUBMISSION_INPUT_TYPES)
    for kind, model_type in agentic.SUBMISSION_INPUT_TYPES.items():
        assert submissions.submission_model(kind) is model_type
        schema = submissions.submission_schema(kind)
        assert schema["description"].startswith(submissions.KIND_SUMMARIES[kind])
        assert schema["examples"] == [submissions.KIND_EXAMPLES[kind]]
        assert submissions.undescribed_property_paths(schema) == []

    assert set(inputs.INPUT_CONTRACT_TYPES) == set(inputs.INPUT_CONTRACT_SUMMARIES)
    assert set(inputs.INPUT_CONTRACT_TYPES) == set(inputs.INPUT_CONTRACT_EXAMPLES)
    for kind in inputs.INPUT_CONTRACT_TYPES:
        schema = inputs.input_contract_schema(kind)
        assert schema["description"] == inputs.INPUT_CONTRACT_SUMMARIES[kind]
        assert schema["examples"] == [inputs.INPUT_CONTRACT_EXAMPLES[kind]]
        assert f"input_contract_kind: {kind}" in inputs.render_input_contract(kind)


def test_file_declared_input_contracts_resolve_to_capability_models() -> None:
    _, registry = load_distribution_reporting_capability()
    inputs = import_module(INPUTS_MODULE)
    contracts = build_contract_catalog(registry)

    for contract_id, model_name in INPUT_CONTRACT_MODELS.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{INPUTS_MODULE}:{model_name}"
        assert contracts[contract_id].json_schema() == getattr(
            inputs, model_name
        ).model_json_schema()
