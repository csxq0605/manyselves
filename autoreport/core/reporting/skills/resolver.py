"""Load approved, versioned module skills from packaged Markdown resources."""

from pathlib import Path

import yaml

from ..models import ReportingModel
from ..taxonomy import resolve_submodule


class SkillDefinition(ReportingModel):
    id: str
    version: str
    title: str
    submodule_ids: list[str]
    guidance: str
    source_path: Path

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


def _parse_skill(path: Path) -> SkillDefinition:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"skill frontmatter is missing: {path}")
    try:
        closing = lines[1:].index("---") + 1
    except ValueError as exc:
        raise ValueError(f"skill frontmatter is not closed: {path}") from exc
    metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    submodule_ids = [str(value) for value in metadata.get("submodules", [])]
    for submodule_id in submodule_ids:
        resolve_submodule(submodule_id)
    return SkillDefinition(
        id=str(metadata["id"]),
        version=str(metadata["version"]),
        title=str(metadata["title"]),
        submodule_ids=submodule_ids,
        guidance="\n".join(lines[closing + 1 :]).strip(),
        source_path=path,
    )


class SkillResolver:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._by_submodule: dict[str, SkillDefinition] = {}
        for path in sorted(self.root.rglob("*.md")):
            skill = _parse_skill(path)
            for submodule_id in skill.submodule_ids:
                if submodule_id in self._by_submodule:
                    raise ValueError(f"duplicate skill for submodule {submodule_id}")
                self._by_submodule[submodule_id] = skill

    @classmethod
    def packaged(cls) -> "SkillResolver":
        root = Path(__file__).resolve().parents[3] / "templates" / "reporting" / "skills"
        return cls(root)

    def resolve(self, submodule_id: str) -> SkillDefinition:
        resolve_submodule(submodule_id)
        try:
            return self._by_submodule[submodule_id]
        except KeyError as exc:
            raise ValueError(f"no approved skill for submodule {submodule_id}") from exc
