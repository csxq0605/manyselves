from pathlib import Path

import pytest

from manyselves.core.reporting.research.reference_library import ReferenceLibrary


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_reference_search_reads_every_supported_file_beneath_knowledge(tmp_path: Path):
    standards = tmp_path / "Knowledge/标准/低压"
    cases = tmp_path / "Knowledge/案例"
    standards.mkdir(parents=True)
    cases.mkdir(parents=True)
    (standards / "温升.md").write_text("连接点温升需要结合负荷与环境判断", encoding="utf-8")
    (cases / "客户样例.md").write_text("连接点温升 客户A 柜号G01", encoding="utf-8")
    (tmp_path / "Knowledge/loose.txt").write_text("连接点温升", encoding="utf-8")

    hits = ReferenceLibrary(tmp_path).search("连接点 温升")

    assert {hit.relative_path for hit in hits} == {
        "Knowledge/标准/低压/温升.md",
        "Knowledge/案例/客户样例.md",
        "Knowledge/loose.txt",
    }


def test_reference_search_rejects_symlink_escape_outside_knowledge(tmp_path: Path):
    allowed = tmp_path / "Knowledge/allowed"
    outside = tmp_path / "Inputs"
    allowed.mkdir(parents=True)
    outside.mkdir(parents=True)
    secret = outside / "secret.md"
    secret.write_text("禁止内容", encoding="utf-8")
    try:
        (allowed / "escape.md").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not available")

    assert ReferenceLibrary(tmp_path).search("禁止内容") == []


def test_reference_open_accepts_only_a_path_beneath_knowledge(tmp_path: Path):
    root = tmp_path / "Knowledge/任意目录"
    root.mkdir(parents=True)
    path = root / "guide.txt"
    path.write_text("完整参考正文", encoding="utf-8")
    library = ReferenceLibrary(tmp_path)

    document = library.open("Knowledge/任意目录/guide.txt")

    assert document.text == "完整参考正文"
    with pytest.raises(ValueError, match="Knowledge"):
        library.open("Inputs/secret.md")


def test_project_document_wins_same_relative_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    _write(project / "Knowledge" / "rules.md", "project priority")
    _write(global_root / "rules.md", "global priority")

    hits = ReferenceLibrary(project, global_root=global_root).search("priority", limit=10)

    assert [(hit.namespace, hit.relative_path) for hit in hits] == [
        ("project", "Knowledge/rules.md"),
    ]


def test_global_document_has_namespaced_reference(tmp_path: Path) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    _write(global_root / "shared.md", "shared standard")

    document = ReferenceLibrary(project, global_root=global_root).open(
        "GlobalKnowledge/shared.md"
    )

    assert document.namespace == "global"
    assert document.relative_path == "GlobalKnowledge/shared.md"
    assert str(global_root) not in document.relative_path


def test_composite_search_deduplicates_equal_content_with_project_priority(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    _write(project / "Knowledge" / "project-copy.md", "same transformer rule")
    _write(global_root / "global-copy.md", "same transformer rule")
    _write(global_root / "unique.md", "unique transformer rule")

    hits = ReferenceLibrary(project, global_root=global_root).search("transformer rule", limit=10)

    assert [(hit.namespace, hit.relative_path) for hit in hits] == [
        ("project", "Knowledge/project-copy.md"),
        ("global", "GlobalKnowledge/unique.md"),
    ]


def test_global_reference_rejects_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    global_root = tmp_path / "global"
    outside = tmp_path / "outside.md"
    global_root.mkdir()
    outside.write_text("outside secret", encoding="utf-8")
    try:
        (global_root / "escape.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available")

    library = ReferenceLibrary(project, global_root=global_root)

    assert library.search("outside secret") == []
    with pytest.raises(ValueError, match="GlobalKnowledge"):
        library.open("GlobalKnowledge/escape.md")
