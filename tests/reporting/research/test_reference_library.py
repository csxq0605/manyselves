import hashlib
import json
from pathlib import Path

import pytest

from manyselves.core.reporting.research.reference_library import ReferenceLibrary


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


def test_reference_snapshot_reuses_full_parsed_text_without_rescanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Knowledge"
    root.mkdir()
    source = root / "full.md"
    complete_text = "完整信息" * 10_000
    source.write_text(complete_text, encoding="utf-8")
    first = ReferenceLibrary(tmp_path)

    document = first.open("Knowledge/full.md")
    manifest_ref = first.snapshot_manifest_ref()

    assert document.text == complete_text
    assert (tmp_path / manifest_ref).is_file()
    second = ReferenceLibrary(tmp_path)
    monkeypatch.setattr(
        second,
        "_read_text",
        lambda _path: (_ for _ in ()).throw(AssertionError("source reparsed")),
    )
    assert second.open("Knowledge/full.md").text == complete_text


def test_reference_snapshot_load_rejects_hash_consistent_retired_history_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Knowledge"
    root.mkdir()
    (root / "clean.md").write_text("正常知识正文", encoding="utf-8")
    first = ReferenceLibrary(tmp_path)
    manifest_path = tmp_path / first.snapshot_manifest_ref()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    poisoned = "<persisted_result_part sha256=" + "b" * 64 + ">"
    digest = hashlib.sha256(poisoned.encode("utf-8")).hexdigest()
    text_ref = Path("Work/indexes/knowledge/text") / digest[:2] / f"{digest}.txt"
    text_path = tmp_path / text_ref
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(poisoned, encoding="utf-8")
    manifest["documents"][0].update(
        {
            "text_ref": text_ref.as_posix(),
            "text_sha256": digest,
            "text_chars": len(poisoned),
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retired internal history token") as exc_info:
        ReferenceLibrary(tmp_path).documents()

    assert "persisted_result_part" not in str(exc_info.value)
