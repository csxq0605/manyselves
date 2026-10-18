"""Prepare a separate project copy with the reviewed reporting references.

No model calls, report generation, source overwrite or frozen-run modification.
The amendments target the uploaded reference edition, not arbitrary libraries.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCES = ROOT / "manyselves/templates/reporting"


def prepare(project: Path, output: Path) -> dict:
    project, output = project.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(project) or project.is_relative_to(output):
        raise ValueError("output must be a new directory outside the source project")
    for name in ("Inputs", "Knowledge"):
        directory = project / name
        if not directory.is_dir():
            raise ValueError(f"missing source directory: {name}")
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError(f"source symlinks are not copied: {name}")

    reviewed = REFERENCES / "reviewed-knowledge"
    amendments = json.loads((reviewed / "amendments.json").read_text(encoding="utf-8"))
    relative = amendments["file"]
    original = (project / "Knowledge" / relative).read_text(encoding="utf-8-sig")
    # A unit ends at the next heading, including the appendix after the last KU.
    parts = re.split(r"(?m)(?=^#{1,3} )", original)
    changed_units = set()
    for index, part in enumerate(parts):
        match = re.match(r"### (KU-[\d.]+-\d+)\s", part)
        if not match or match[1] not in amendments["unit_fields"]:
            continue
        unit = match[1]
        if unit in changed_units:
            raise ValueError(f"ambiguous knowledge unit: {unit}")
        for field, value in amendments["unit_fields"][unit].items():
            part, count = re.subn(
                rf"(?m)^{re.escape(field)}：[^\n]*$",
                lambda _: f"{field}：{value}", part,
            )
            if count != 1:
                raise ValueError(f"expected exactly one {unit}/{field}, got {count}")
        part = re.sub(r"(?m)^风险等级：[^\n]*$", "风险等级：待按项目证据及适用依据确定", part)
        parts[index] = part.rstrip() + (
            "\n依据状态：相关标准仅为候选检索入口，原条款支持、现行性及项目适用条件尚未核实；"
            "本次修订不宣称已完成专业标准认证。\n\n"
        )
        if topic := amendments.get("unit_topics", {}).get(unit):
            parts[index] += f"检索关联主题：{topic}。仅辅助检索，不改变原KU身份或证明适用性。\n\n"
        changed_units.add(unit)
    if changed_units != set(amendments["unit_fields"]):
        raise ValueError("source edition is missing expected knowledge units")
    replacements = {relative: "".join(parts)}
    for change in amendments["replacements"]:
        relative = change["file"]
        content = replacements.get(relative)
        if content is None:
            content = (project / "Knowledge" / relative).read_text(encoding="utf-8-sig")
        if content.count(change["old"]) != 1:
            raise ValueError(f"source edition does not match reviewed amendment: {relative}")
        replacements[relative] = content.replace(change["old"], change["new"])
    for file in reviewed.rglob("*.md"):
        relative = file.relative_to(reviewed).as_posix()
        if not (project / "Knowledge" / relative).is_file():
            raise ValueError(f"missing reviewed source: {relative}")
        replacements[relative] = file.read_text(encoding="utf-8")

    skills = REFERENCES / "reviewed-role-skills"
    source_skills = project / "Inputs/report-template-role-skills"
    # Validate the existing boundary before making a project copy.
    boundary = json.loads((source_skills / "boundary.json").read_text(encoding="utf-8"))
    role_files = sorted(skills.glob("*/SKILL.md"))
    for file in role_files:
        if not (source_skills / file.relative_to(skills)).is_file():
            raise ValueError(f"missing original role: {file.parent.name}")

    for name in ("Inputs", "Knowledge"):
        shutil.copytree(project / name, output / name)
    lineage = output / "review-lineage"
    shutil.copytree(source_skills, lineage / "original-role-skills")
    target_skills = output / "Inputs/report-template-role-skills"
    for file in role_files:
        shutil.copyfile(file, target_skills / file.relative_to(skills))
    boundary["boundary_statement"] = (
        "人工审订的方法资料；不迁移原模板客户事实、阈值或操作方案。"
        "任务合同决定范围；现场判断依赖当前证据及经核实适用规则。"
        "原模板未重新核验，不声称本版是重新蒸馏的结果。"
        "当前项目可追溯的设备、数值与定位须保留；示例不增加章节、数量、时限或工程操作要求。"
    )
    (target_skills / "boundary.json").write_text(
        json.dumps(boundary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    # Old hashes and template-distiller producer describe the original only.
    source = {
        "source": "manual-reference-review", "revision": "pr6-2026-10-18",
        "producer": "manual-editorial-revision",
        "skill_root": "Inputs/report-template-role-skills",
        "boundary_ref": "Inputs/report-template-role-skills/boundary.json",
        "boundary_policy_version": boundary["policy_version"],
        "original_source_ref": "review-lineage/original-role-skills/source.json",
        "original_template_reinspected": False,
    }
    (target_skills / "source.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    for relative, content in replacements.items():
        old = lineage / "original-knowledge" / relative
        old.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / "Knowledge" / relative, old)
        (output / "Knowledge" / relative).write_text(content, encoding="utf-8")
    summary = {
        "revision": source["revision"], "roles_revised": len(role_files),
        "knowledge_units_revised": sorted(changed_units),
        "knowledge_files_revised": sorted(replacements),
        "provider_calls": 0, "report_runs": 0,
        "limits": ["Editorial review does not certify professional conclusions.",
                   "No official standard status or applicability was certified.",
                   "Existing frozen runs are unchanged; this is a separate project copy."],
    }
    (lineage / "revision.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.project, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
