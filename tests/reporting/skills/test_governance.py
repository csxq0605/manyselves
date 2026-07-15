from pathlib import Path

import pytest

from autoreport.core.reporting.skills.governance import (
    RegressionCase,
    SkillGovernance,
    SkillPublishError,
)


def test_skill_candidate_requires_passing_regression_before_publish_and_can_rollback(
    tmp_path: Path,
) -> None:
    governance = SkillGovernance(tmp_path / "Work" / "skills")
    baseline = governance.register_baseline(
        skill_id="pds.module21.architecture",
        version="1.0.0",
        content="负荷率低于100%不得写成当前过载。",
    )
    candidate = governance.create_candidate(
        skill_id=baseline.skill_id,
        base_version=baseline.version,
        candidate_version="1.1.0",
        content="负荷率低于100%不得写成当前过载，并保留计算口径。",
    )
    cases = [
        RegressionCase(
            id="below-100",
            prompt="负荷率96.99%",
            required_phrases=["未达到100%"],
            forbidden_phrases=["已过载"],
        )
    ]

    failed = governance.evaluate(candidate, cases, lambda _candidate, _case: "设备已过载")
    assert failed.passed is False
    with pytest.raises(SkillPublishError, match="regression"):
        governance.publish(candidate, failed)

    candidate = governance.create_candidate(
        skill_id=baseline.skill_id,
        base_version=baseline.version,
        candidate_version="1.1.1",
        content="负荷率低于100%不得写成当前过载，并保留计算口径。",
    )
    passed = governance.evaluate(
        candidate,
        cases,
        lambda _candidate, _case: "本次负荷率未达到100%，不能据此认定超过额定负荷。",
    )
    published = governance.publish(candidate, passed)

    assert published.version == "1.1.1"
    assert governance.active_version(baseline.skill_id) == "1.1.1"
    rollback = governance.rollback(baseline.skill_id, "1.0.0", reason="现场回归异常")
    assert rollback.version == "1.0.0"
    assert governance.active_version(baseline.skill_id) == "1.0.0"
    history = governance.history(baseline.skill_id)
    assert [record.action for record in history] == ["baseline", "publish", "rollback"]


def test_skill_candidate_version_is_immutable(tmp_path: Path) -> None:
    governance = SkillGovernance(tmp_path / "skills")
    governance.register_baseline("pds.test", "1.0.0", "baseline")
    governance.create_candidate("pds.test", "1.0.0", "1.1.0", "candidate")

    with pytest.raises(SkillPublishError, match="already exists"):
        governance.create_candidate("pds.test", "1.0.0", "1.1.0", "changed")
