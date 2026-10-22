"""Template examples are methods, not independent evidence or workflow rules."""

from manyselves.capabilities.distribution_reporting.runtime import (
    module_authoring_preparation,
)


def test_author_skill_context_distinguishes_method_from_evidence_and_contract():
    skill_id = "author-2.4"
    state = {"template_skill_text": {skill_id: "示例：所有缺陷分三级，必须写第四章。"}}
    context = module_authoring_preparation._template_skill_context(state, skill_id)
    assert 'authority="method_only"' in context
    assert "适用条件" in context and "示例不等于强制要求" in context
    assert "任务合同" in context and "第四章" in context
