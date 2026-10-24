"""Template examples are methods, not independent evidence or workflow rules."""

from types import SimpleNamespace

import pytest

from manyselves.capabilities.distribution_reporting.runtime import (
    final_delivery_binding,
    final_review_tools,
    module_authoring_preparation,
    module_review_preparation,
)
from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
    RunInputSnapshotStore,
)
from manyselves.capabilities.distribution_reporting.runtime.template_skill_paths import (
    template_skill_ref,
)


@pytest.mark.parametrize("role", ["author", "review", "final", "chief"])
def test_all_consumers_distinguish_method_from_evidence_and_contract(tmp_path, role):
    skill_id = {"author": "author-2.4", "review": "auditor-2.4", "final": "final-auditor",
                "chief": "chief-editor-chapter-1"}[role]
    state = {"template_skill_text": {skill_id: "示例：所有缺陷分三级，必须写第四章。"}}
    if role == "author":
        context = module_authoring_preparation._template_skill_context(state, skill_id)
    elif role == "review":
        context = module_review_preparation._template_skill_context(tmp_path, state, "2.4")
    elif role == "final":
        context = final_delivery_binding._template_skill_context(state, tmp_path, skill_id)
    else:
        context = final_review_tools._template_skill_context(state, tmp_path, skill_id)
    assert 'authority="method_only"' in context
    assert "适用条件" in context and "示例不等于强制要求" in context
    assert "任务合同" in context and "第四章" in context
    if role in {"review", "final"}:
        assert "不得仅凭 Skill" in context


def test_reviewer_fallback_uses_frozen_skill_instead_of_modified_live_file(tmp_path, monkeypatch):
    logical = template_skill_ref("auditor-2.4")
    live = tmp_path / logical
    live.parent.mkdir(parents=True)
    live.write_text("修改后的新规则", encoding="utf-8")
    frozen = tmp_path / "frozen-skill.md"
    frozen.write_text("本次运行的原规则", encoding="utf-8")
    manifest = tmp_path / "Work/runs/frozen/input-snapshot.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(RunInputSnapshotStore, "load", lambda *_: SimpleNamespace(resolve=lambda _: frozen.relative_to(tmp_path)))
    context = module_review_preparation._template_skill_context(tmp_path, {"run_id": "frozen"}, "2.4")
    assert "本次运行的原规则" in context and "修改后的新规则" not in context
