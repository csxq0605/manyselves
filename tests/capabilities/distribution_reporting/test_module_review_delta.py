from __future__ import annotations

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_delta import (
    build_module_recheck_delta,
)


def _module(*, claim_text: str, revision: int) -> ModuleSubmission:
    return ModuleSubmission(
        module_id="2.4",
        submodule_narratives={
            submodule_id: f"### {submodule_id}\n\n保持不变的模块正文。"
            for submodule_id in REPORT_TAXONOMY["2.4"].submodules
        },
        claims=[
            ClaimRecord(
                id="C-module-delta",
                module_id="2.4",
                submodule_id="2.4.1.2",
                text=claim_text,
                claim_type="technical_interpretation",
                source_ids=[],
            )
        ],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
    )


def test_module_recheck_delta_rejects_changed_claim_outside_reviewer_scope() -> None:
    baseline = _module(claim_text="原始技术判断。", revision=0)
    revised = _module(claim_text="未经分配却被修改的技术判断。", revision=1)

    with pytest.raises(ValueError, match="Claims outside reviewer finding scope"):
        build_module_recheck_delta(baseline, revised, {"2.4.1.1"})


def test_recheck_keeps_unchanged_disputed_text_without_previous_conversation() -> None:
    baseline = _module(claim_text="数值 16.58 超过 10，但结论正常。", revision=0)
    current = baseline.model_copy(update={"revision": 1})
    delta = build_module_recheck_delta(baseline, current, {"2.4.1.2"})

    assert delta["subject"].submodule_narratives == {
        "2.4.1.2": baseline.submodule_narratives["2.4.1.2"],
    }
    assert [item.text for item in delta["claim_statements"]] == [baseline.claims[0].text]
    assert delta["revision_diff"].changed_statement_refs == []
    assert delta["prior_claim_statements"] == []
