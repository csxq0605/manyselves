import json
from pathlib import Path

import pytest

from autoreport.core.reporting.skills.governance import SkillGovernanceStore


def test_candidate_evaluation_publish_and_rollback_are_project_local(
    tmp_path: Path,
) -> None:
    store = SkillGovernanceStore(tmp_path)
    candidate = store.create_candidate(
        module_id="2.4",
        original="a",
        revised="b",
        reason="对象绑定错误",
    )
    evaluation = store.record_evaluation(
        candidate.id,
        baseline=0.5,
        candidate_score=0.9,
        regressions=[],
    )
    first = store.publish(candidate.id, evaluation.id, confirmed=True)

    second_candidate = store.create_candidate(
        module_id="2.4", original="b", revised="c", reason="图片绑定改进"
    )
    second_evaluation = store.record_evaluation(
        second_candidate.id,
        baseline=0.9,
        candidate_score=0.95,
        regressions=[],
    )
    second = store.publish(second_candidate.id, second_evaluation.id, confirmed=True)
    store.rollback(module_id="2.4", version_id=first.id)

    assert store.active_version("2.4").id == first.id
    assert (tmp_path / "Capabilities/skills/versions/2.4" / f"{first.id}.json").is_file()
    assert (tmp_path / "Capabilities/skills/versions/2.4" / f"{second.id}.json").is_file()
    manifest = json.loads(
        (tmp_path / "Capabilities/skills/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["active_versions"]["2.4"] == first.id


def test_publish_requires_explicit_confirmation(tmp_path: Path) -> None:
    store = SkillGovernanceStore(tmp_path)
    candidate = store.create_candidate(module_id="2.1", original="a", revised="b", reason="改进")
    evaluation = store.record_evaluation(
        candidate.id, baseline=0.5, candidate_score=0.8, regressions=[]
    )

    with pytest.raises(ValueError, match="confirmation"):
        store.publish(candidate.id, evaluation.id, confirmed=False)


@pytest.mark.parametrize(
    ("baseline", "candidate_score", "regressions"),
    [(0.8, 0.7, []), (0.8, 0.9, ["2.4.2 接地样本退化"])],
)
def test_publish_rejects_regressing_evaluation(
    tmp_path: Path,
    baseline: float,
    candidate_score: float,
    regressions: list[str],
) -> None:
    store = SkillGovernanceStore(tmp_path)
    candidate = store.create_candidate(module_id="2.4", original="a", revised="b", reason="改进")
    evaluation = store.record_evaluation(
        candidate.id,
        baseline=baseline,
        candidate_score=candidate_score,
        regressions=regressions,
    )

    with pytest.raises(ValueError, match="regress"):
        store.publish(candidate.id, evaluation.id, confirmed=True)


def test_publish_rejects_evaluation_from_another_candidate(tmp_path: Path) -> None:
    store = SkillGovernanceStore(tmp_path)
    first = store.create_candidate(module_id="2.1", original="a", revised="b", reason="改进架构")
    second = store.create_candidate(module_id="2.4", original="a", revised="b", reason="改进设备")
    evaluation = store.record_evaluation(
        first.id, baseline=0.5, candidate_score=0.8, regressions=[]
    )

    with pytest.raises(ValueError, match="candidate.*evaluation"):
        store.publish(second.id, evaluation.id, confirmed=True)
