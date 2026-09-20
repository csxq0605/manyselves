import ast
import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import ContractDefinition, DefinitionKind

REVIEW_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.review"
)
CROSS_OWNER_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.cross_owner"
)

REVIEW_MODEL_NAMES = (
    "ModuleReviewProgress",
    "ModuleReviewPreflightProgress",
    "ModuleInitialReviewPreparation",
    "ModuleInitialReviewAcceptance",
    "ModuleRevisionPreparation",
    "ModuleRecheckPreparation",
    "ModuleRecheckAcceptance",
    "ModuleLocalRegressionContext",
    "CrossReviewProgress",
    "FinalReviewProgress",
    "_CrossOwnerLaneResult",
    "_CrossOwnerPipelineResult",
    "CrossOwnerInitialReviewPreparation",
    "CrossOwnerInitialReviewAcceptance",
    "CrossOwnerRevisionPreparation",
    "CrossOwnerRevisionAcceptance",
    "MainExceptionDecisionPreparation",
    "MainExceptionDecisionAcceptance",
    "CrossOwnerLocalReviewPreparation",
    "CrossOwnerLocalReviewAcceptance",
    "CrossOwnerRecheckPreparation",
    "CrossOwnerRecheckAcceptance",
    "CrossOwnerRoundProgress",
)

CROSS_OWNER_MODEL_NAMES = (
    "DeclarativeCrossOwnerInitialAgentResult",
    "DeclarativeCrossOwnerRecheckAgentResult",
    "DeclarativeMainExceptionAgentResult",
    "DeclarativeCrossOwnerRuntimeContext",
)

CONTRACT_MODELS = {
    "declarative_cross_owner_initial_agent_result": (
        "DeclarativeCrossOwnerInitialAgentResult"
    ),
    "declarative_cross_owner_recheck_agent_result": (
        "DeclarativeCrossOwnerRecheckAgentResult"
    ),
    "declarative_main_exception_agent_result": "DeclarativeMainExceptionAgentResult",
    "declarative_cross_owner_runtime_context": "DeclarativeCrossOwnerRuntimeContext",
}


def test_review_models_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {REVIEW_MODULE} as review",
                    f"import {CROSS_OWNER_MODULE} as cross_owner",
                    "print(json.dumps({",
                    "    'review': review.__name__,",
                    "    'cross_owner': cross_owner.__name__,",
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
        "review": REVIEW_MODULE,
        "cross_owner": CROSS_OWNER_MODULE,
        "core_reporting": [],
    }


def test_review_models_are_physically_capability_owned() -> None:
    review = import_module(REVIEW_MODULE)
    cross_owner = import_module(CROSS_OWNER_MODULE)

    for model_name in REVIEW_MODEL_NAMES:
        assert getattr(review, model_name).__module__ == REVIEW_MODULE
    for model_name in CROSS_OWNER_MODEL_NAMES:
        assert getattr(cross_owner, model_name).__module__ == CROSS_OWNER_MODULE

    for module in (review, cross_owner):
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


def test_cross_owner_contracts_resolve_to_capability_models() -> None:
    _, registry = load_distribution_reporting_capability()
    cross_owner = import_module(CROSS_OWNER_MODULE)
    contracts = build_contract_catalog(registry)

    for contract_id, model_name in CONTRACT_MODELS.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        assert isinstance(definition, ContractDefinition)
        assert definition.model == f"{CROSS_OWNER_MODULE}:{model_name}"
        assert contracts[contract_id].json_schema() == getattr(
            cross_owner, model_name
        ).model_json_schema()


def test_cross_owner_runtime_context_nested_round_trip_and_extra_forbid() -> None:
    agentic = import_module(
        "manyselves.capabilities.distribution_reporting.runtime.models.agentic"
    )
    review = import_module(REVIEW_MODULE)
    cross_owner = import_module(CROSS_OWNER_MODULE)

    acceptance = review.MainExceptionDecisionAcceptance(
        run_id="run-1",
        workflow_id="workflow-1",
        scope="cross-owner:2.1",
        trigger="author exception",
        decision_ref="runs/run-1/decision.json",
        result=agentic.WorkflowDecisionSubmission(
            decision="return_to_author",
            rationale="The finding remains unresolved.",
            finding_ids=["CF-2.1-001"],
        ),
    )
    context = cross_owner.DeclarativeCrossOwnerRuntimeContext(
        owner_module_id="2.1",
        status="author_exception_accepted",
        main_acceptance=acceptance,
    )
    payload = context.model_dump(mode="json")

    assert cross_owner.DeclarativeCrossOwnerRuntimeContext.model_validate(
        payload
    ) == context
    with pytest.raises(ValidationError):
        cross_owner.DeclarativeCrossOwnerRuntimeContext.model_validate(
            {**payload, "unexpected": True}
        )
