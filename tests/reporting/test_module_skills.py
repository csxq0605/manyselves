from pathlib import Path

import pytest

from manyselves.core.reporting.config import ConfigurationError
from manyselves.core.reporting.module_skills import ModuleSkillLibrary


def test_specialist_receives_only_owned_module_skills() -> None:
    library = ModuleSkillLibrary.packaged()

    skills = library.for_agent("module-2.4-specialist")

    assert len(skills) == 4
    assert {skill.module_id for skill in skills} == {"2.4"}
    assert {skill.id for skill in skills} == {
        "pds.module24.configuration",
        "pds.module24.field-inspection",
        "pds.module24.installation",
        "pds.module24.operating-condition",
    }


def test_auditor_receives_only_module_under_review() -> None:
    library = ModuleSkillLibrary.packaged()

    skills = library.for_agent("evidence-auditor", module_id="2.2")

    assert skills
    assert {skill.module_id for skill in skills} == {"2.2"}


def test_auditor_receives_only_skills_for_required_submodules() -> None:
    library = ModuleSkillLibrary.packaged()

    skills = library.for_agent(
        "evidence-auditor",
        module_id="2.4",
        submodule_ids={"2.4.2.2"},
    )

    assert {skill.id for skill in skills} == {"pds.module24.installation"}


def test_routing_index_contains_metadata_without_skill_bodies() -> None:
    library = ModuleSkillLibrary.packaged()

    index = library.index_text()

    assert "2.1: pds.module21.architecture" in index
    assert "2.4: pds.module24.configuration" in index
    assert "负荷率必须保留计算口径" not in index
    assert library.for_agent("main-agent") == []


def test_loader_rejects_skill_assigned_to_wrong_module(tmp_path: Path) -> None:
    skill_dir = tmp_path / "2.1"
    skill_dir.mkdir()
    (skill_dir / "bad.md").write_text(
        "---\nid: bad\nversion: '1'\ntitle: bad\nsubmodules: ['2.4.1.1']\n---\nbody",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="does not belong to module 2.1"):
        ModuleSkillLibrary.load(tmp_path)
