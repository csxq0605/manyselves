from pathlib import Path

import pytest

from autoreport.core.reporting.research.reference_library import ReferenceLibrary


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
