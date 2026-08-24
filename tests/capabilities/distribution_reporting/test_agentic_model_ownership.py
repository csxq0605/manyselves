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

AGENTIC_MODEL_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.agentic"
)

CONTRACT_MODEL_NAMES = {
    "module_submission": "ModuleSubmission",
    "module_revision_submission": "ModuleRevisionSubmission",
    "module_review_finding_submission": "ModuleReviewFindingSubmission",
    "module_review_verdict_submission": "ModuleReviewVerdictSubmission",
    "cross_owner_finding_submission": "CrossOwnerFindingSubmission",
    "cross_owner_verdict_submission": "CrossOwnerVerdictSubmission",
    "chief_chapter_lane_submission": "ChiefChapterLaneSubmission",
    "chief_chapter_lane_revision_submission": "ChiefChapterLaneRevisionSubmission",
    "final_chapter_lane_finding_submission": "FinalChapterLaneFindingSubmission",
    "final_chapter_lane_verdict_submission": "FinalChapterLaneVerdictSubmission",
}


def test_agentic_models_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {AGENTIC_MODEL_MODULE} as agentic",
                    "print(json.dumps({",
                    "    'module': agentic.__name__,",
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
        "module": AGENTIC_MODEL_MODULE,
        "core_reporting": [],
    }


def test_agentic_models_are_physically_owned_by_the_capability() -> None:
    agentic = import_module(AGENTIC_MODEL_MODULE)

    ownership_roots = (
        "StrictModel",
        "TaskEnvelope",
        "ModuleSubmission",
        "ModuleRevisionSubmission",
        "CrossOwnerFindingSubmission",
        "CrossOwnerVerdictSubmission",
        "ChiefChapterLaneSubmission",
        "ChiefChapterLaneRevisionSubmission",
        "FinalChapterLaneFindingSubmission",
        "FinalChapterLaneVerdictSubmission",
        "EditedReportSubmission",
        "AgentResult",
    )
    for model_name in ownership_roots:
        assert getattr(agentic, model_name).__module__ == AGENTIC_MODEL_MODULE
    assert all(
        model_type.__module__ == AGENTIC_MODEL_MODULE
        for model_type in agentic.SUBMISSION_INPUT_TYPES.values()
    )

    source = Path(agentic.__file__).read_text(encoding="utf-8")
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


def test_agentic_contracts_resolve_to_capability_owned_models() -> None:
    _, registry = load_distribution_reporting_capability()
    agentic = import_module(AGENTIC_MODEL_MODULE)
    contracts = build_contract_catalog(registry)

    for contract_id, model_name in CONTRACT_MODEL_NAMES.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{AGENTIC_MODEL_MODULE}:{model_name}"
        assert contracts[contract_id].json_schema() == getattr(
            agentic, model_name
        ).model_json_schema()
