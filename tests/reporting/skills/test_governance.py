import json
from pathlib import Path

import pytest

from manyselves.core.reporting.skills.governance import SkillGovernanceStore


def _publish(
    store: SkillGovernanceStore,
    *,
    skill_id: str,
    content: str,
    baseline: float = 0.7,
    candidate_score: float = 0.9,
):
    feedback = store.record_feedback(
        skill_id=skill_id,
        module_id="2.4",
        feedback="需要修正对象绑定逻辑",
        explicit_promotion_requested=True,
        report_version_id="report-v1",
    )
    candidate = store.create_candidate(
        feedback.id,
        title="设备状态与风险",
        submodules=["2.4.1.1"],
        proposed_content=content,
        reason="将可复用规则与单次报告措辞分离",
    )
    evaluation = store.record_evaluation(
        candidate.id,
        baseline=baseline,
        candidate_score=candidate_score,
        regressions=[],
        model="test-model",
    )
    return store.publish(candidate.id, evaluation.id, confirmed=True)


def test_feedback_requires_explicit_skill_promotion_intent(tmp_path: Path) -> None:
    store = SkillGovernanceStore(tmp_path / "Capabilities/skills", scope="project")

    with pytest.raises(ValueError, match="explicit"):
        store.record_feedback(
            skill_id="pds.module24.device-risk",
            module_id="2.4",
            feedback="只修改本次报告",
            explicit_promotion_requested=False,
            report_version_id="report-v1",
        )


def test_versions_and_active_manifest_are_per_skill_id_not_per_module(tmp_path: Path) -> None:
    root = tmp_path / "Capabilities/skills"
    store = SkillGovernanceStore(root, scope="project")
    first = _publish(store, skill_id="pds.module24.device-risk", content="规则 A")
    second = _publish(store, skill_id="pds.module24.photo-binding", content="规则 B")

    assert store.active_version("pds.module24.device-risk").id == first.id
    assert store.active_version("pds.module24.photo-binding").id == second.id
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["active_versions"]) == {
        "pds.module24.device-risk",
        "pds.module24.photo-binding",
    }
    assert "2.4" not in manifest["active_versions"]


def test_candidate_is_immutable_and_publish_requires_non_regression_and_confirmation(
    tmp_path: Path,
) -> None:
    store = SkillGovernanceStore(tmp_path / "Capabilities/skills", scope="project")
    feedback = store.record_feedback(
        skill_id="pds.module24.device-risk",
        module_id="2.4",
        feedback="改进",
        explicit_promotion_requested=True,
        report_version_id="report-v1",
    )
    candidate = store.create_candidate(
        feedback.id,
        title="设备状态与风险",
        submodules=["2.4.1.1"],
        proposed_content="候选规则",
        reason="改进",
    )
    with pytest.raises(ValueError, match="already exists"):
        store.save_candidate(candidate)
    evaluation = store.record_evaluation(
        candidate.id,
        baseline=0.8,
        candidate_score=0.7,
        regressions=[],
        model="test",
    )
    with pytest.raises(ValueError, match="regress"):
        store.publish(candidate.id, evaluation.id, confirmed=True)
    with pytest.raises(ValueError, match="confirmation"):
        store.publish(candidate.id, evaluation.id, confirmed=False)


def test_rollback_changes_active_version_without_mutating_versions(tmp_path: Path) -> None:
    store = SkillGovernanceStore(tmp_path / "Capabilities/skills", scope="project")
    first = _publish(store, skill_id="pds.module24.device-risk", content="规则 A")
    second = _publish(store, skill_id="pds.module24.device-risk", content="规则 B")

    store.rollback(skill_id="pds.module24.device-risk", version_id=first.id)

    assert store.active_version("pds.module24.device-risk").id == first.id
    assert store.load_version(second.id).content == "规则 B"
