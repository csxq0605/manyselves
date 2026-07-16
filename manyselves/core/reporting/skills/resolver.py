"""Resolve active Skills with packaged, product, then project precedence."""

from pathlib import Path

from ..module_skills import ModuleSkill, ModuleSkillLibrary
from .governance import SkillGovernanceStore, SkillScope


class RuntimeSkillResolver:
    @staticmethod
    def resolve(
        packaged: ModuleSkillLibrary,
        *,
        product_root: Path,
        project_root: Path,
    ) -> ModuleSkillLibrary:
        resolved = {skill.id: skill for skill in packaged.skills}
        RuntimeSkillResolver._overlay(resolved, product_root, "product")
        RuntimeSkillResolver._overlay(resolved, project_root, "project")
        return ModuleSkillLibrary(tuple(resolved.values()))

    @staticmethod
    def _overlay(resolved: dict[str, ModuleSkill], root: Path, scope: SkillScope) -> None:
        root = Path(root)
        if not (root / "manifest.json").is_file():
            return
        store = SkillGovernanceStore(root, scope=scope)
        for version in store.active_versions():
            resolved[version.skill_id] = ModuleSkill(
                id=version.skill_id,
                version=version.id,
                title=version.title,
                module_id=version.module_id,
                submodules=tuple(version.submodules),
                content=version.content,
                source_path=(root / "versions" / version.skill_id / f"{version.id}.json"),
                scope=version.scope,
                sha256=version.content_sha256,
            )
