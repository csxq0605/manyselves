from pathlib import Path

import pytest

from autoreport.core.reporting.research.reference_library import ReferenceLibrary


def test_reference_search_reads_only_01_and_never_falls_back(tmp_path: Path):
    allowed = tmp_path / "Knowledge/01_页面导入知识库"
    forbidden = tmp_path / "Knowledge/02_本地skill提示词资料_禁止导入"
    allowed.mkdir(parents=True)
    forbidden.mkdir(parents=True)
    (allowed / "温升.md").write_text(
        "连接点温升需要结合负荷与环境判断", encoding="utf-8"
    )
    (forbidden / "客户样例.md").write_text(
        "连接点温升 客户A 柜号G01", encoding="utf-8"
    )
    (tmp_path / "Knowledge/loose.txt").write_text("连接点温升", encoding="utf-8")

    hits = ReferenceLibrary(tmp_path).search("连接点 温升")

    assert [hit.relative_path for hit in hits] == [
        "Knowledge/01_页面导入知识库/温升.md"
    ]
    assert "客户A" not in hits[0].snippet


def test_reference_search_rejects_symlink_escape_to_02(tmp_path: Path):
    allowed = tmp_path / "Knowledge/01_页面导入知识库"
    forbidden = tmp_path / "Knowledge/02_本地skill提示词资料_禁止导入"
    allowed.mkdir(parents=True)
    forbidden.mkdir(parents=True)
    secret = forbidden / "secret.md"
    secret.write_text("禁止内容", encoding="utf-8")
    try:
        (allowed / "escape.md").symlink_to(secret)
    except OSError:
        pytest.skip("symlinks are not available")

    assert ReferenceLibrary(tmp_path).search("禁止内容") == []


def test_reference_open_accepts_only_a_path_beneath_01(tmp_path: Path):
    root = tmp_path / "Knowledge/01_页面导入知识库"
    root.mkdir(parents=True)
    path = root / "guide.txt"
    path.write_text("完整参考正文", encoding="utf-8")
    library = ReferenceLibrary(tmp_path)

    document = library.open("Knowledge/01_页面导入知识库/guide.txt")

    assert document.text == "完整参考正文"
    with pytest.raises(ValueError, match="01"):
        library.open("Knowledge/02_本地skill提示词资料_禁止导入/secret.md")
