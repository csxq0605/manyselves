"""Security and concurrency contracts for project workspace file access."""

import asyncio
import base64
import io
import zipfile
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook

from manyselves.application.preview_service import (
    InvalidPreviewDocument,
    PreviewEncodingError,
    PreviewService,
    PreviewTooLarge,
)
from manyselves.application.workspace_files import (
    DestinationExists,
    FileRevisionConflict,
    UnsafeWorkspacePath,
    UploadTooLarge,
    WorkspaceFiles,
)


@pytest.fixture
def workspace_files(tmp_path: Path) -> WorkspaceFiles:
    project = tmp_path / "p1"
    (project / "Inputs").mkdir(parents=True)
    (project / "Inputs" / "a.txt").write_text("initial", encoding="utf-8")
    return WorkspaceFiles(project, max_text_bytes=1024, max_upload_bytes=8)


@pytest.mark.parametrize(
    "unsafe",
    [
        "../outside.txt",
        "/tmp/outside.txt",
        r"C:\outside.txt",
        "%2e%2e%2foutside.txt",
        "Inputs%2fa.txt",
        r"Inputs%5ca.txt",
    ],
)
def test_resolve_rejects_traversal_and_absolute_paths(
    workspace_files: WorkspaceFiles, unsafe: str
) -> None:
    """Normalizing attacker-controlled paths must never escape or reinterpret separators."""
    with pytest.raises(UnsafeWorkspacePath):
        workspace_files.resolve(unsafe)


def test_resolve_rejects_existing_parent_symlink_escape(
    workspace_files: WorkspaceFiles, tmp_path: Path
) -> None:
    """A symlink parent must not redirect a validated path outside the workspace."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace_files.project_root / "Inputs" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"platform denied symlink creation: {error}")

    with pytest.raises(UnsafeWorkspacePath):
        workspace_files.resolve("Inputs/linked/secret.txt")


def test_write_rejects_stale_revision(workspace_files: WorkspaceFiles) -> None:
    """A client must not overwrite content that changed after its read."""
    current = workspace_files.read_text("Inputs/a.txt")
    workspace_files.write_text("Inputs/a.txt", "server", current.revision)

    with pytest.raises(FileRevisionConflict):
        workspace_files.write_text("Inputs/a.txt", "client", current.revision)

    assert workspace_files.read_text("Inputs/a.txt").content == "server"


def test_write_is_atomic_and_cleans_failed_sibling_temp(
    workspace_files: WorkspaceFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement must preserve the prior file and leave no upload artifact."""
    current = workspace_files.read_text("Inputs/a.txt")

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("injected replace interruption")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace interruption"):
        workspace_files.write_text("Inputs/a.txt", "replacement", current.revision)

    assert (workspace_files.project_root / "Inputs" / "a.txt").read_text() == "initial"
    assert list((workspace_files.project_root / "Inputs").glob(".manyselves-tmp-*")) == []


def test_create_and_rename_reject_duplicate_destinations(workspace_files: WorkspaceFiles) -> None:
    """Create or rename must never silently replace an existing entry."""
    workspace_files.create_file("Inputs/b.txt", "b")

    with pytest.raises(DestinationExists):
        workspace_files.create_file("Inputs/b.txt", "other")
    with pytest.raises(DestinationExists):
        workspace_files.rename(
            "Inputs/a.txt",
            "Inputs/b.txt",
            workspace_files.entry("Inputs/a.txt").revision,
        )

    assert workspace_files.read_text("Inputs/b.txt").content == "b"


def test_rename_and_delete_reject_stale_file_revisions(workspace_files: WorkspaceFiles) -> None:
    """Destructive mutations must not act on a file that changed after the client tree read."""
    stale = workspace_files.entry("Inputs/a.txt").revision
    current = workspace_files.read_text("Inputs/a.txt")
    workspace_files.write_text("Inputs/a.txt", "changed", current.revision)

    with pytest.raises(FileRevisionConflict):
        workspace_files.rename("Inputs/a.txt", "Inputs/renamed.txt", stale)
    with pytest.raises(FileRevisionConflict):
        workspace_files.delete("Inputs/a.txt", stale)

    assert workspace_files.read_text("Inputs/a.txt").content == "changed"
    assert not (workspace_files.project_root / "Inputs" / "renamed.txt").exists()


def test_directory_revision_is_deterministic_and_changes_with_descendants(
    workspace_files: WorkspaceFiles,
) -> None:
    """Directory rename/delete revisions must represent the deterministic subtree state."""
    workspace_files.create_directory("Inputs/folder")
    workspace_files.create_file("Inputs/folder/a.txt", "one")
    first = workspace_files.entry("Inputs/folder").revision
    second_reader = WorkspaceFiles(workspace_files.project_root)

    assert second_reader.entry("Inputs/folder").revision == first

    child = workspace_files.read_text("Inputs/folder/a.txt")
    workspace_files.write_text("Inputs/folder/a.txt", "two", child.revision)
    changed = workspace_files.entry("Inputs/folder").revision

    assert changed != first
    with pytest.raises(FileRevisionConflict):
        workspace_files.delete("Inputs/folder", first)
    assert (workspace_files.project_root / "Inputs" / "folder").is_dir()

    workspace_files.delete("Inputs/folder", changed)
    assert not (workspace_files.project_root / "Inputs" / "folder").exists()


def test_list_tree_hashes_each_nested_file_once(
    workspace_files: WorkspaceFiles,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory revisions must reuse child digests instead of rescanning every subtree."""
    tree = workspace_files.project_root / "Inputs" / "tree"
    (tree / "child" / "deep").mkdir(parents=True)
    (tree / "one.txt").write_text("one", encoding="utf-8")
    (tree / "child" / "two.txt").write_text("two", encoding="utf-8")
    (tree / "child" / "deep" / "three.txt").write_text("three", encoding="utf-8")
    original_open = Path.open
    hash_opens: dict[str, int] = {}

    def counted_open(path: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "rb" and path.is_relative_to(tree):
            relative = path.relative_to(tree).as_posix()
            hash_opens[relative] = hash_opens.get(relative, 0) + 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)

    entries = workspace_files.list_tree("Inputs/tree")

    assert {entry.path for entry in entries} == {
        "Inputs/tree/one.txt",
        "Inputs/tree/child",
        "Inputs/tree/child/two.txt",
        "Inputs/tree/child/deep",
        "Inputs/tree/child/deep/three.txt",
    }
    assert hash_opens == {
        "one.txt": 1,
        "child/two.txt": 1,
        "child/deep/three.txt": 1,
    }


@pytest.mark.asyncio
async def test_interrupted_upload_cleans_temp_and_destination(
    workspace_files: WorkspaceFiles,
) -> None:
    """Cancellation must not publish a partial upload or strand a sibling temp file."""

    async def interrupted():
        yield b"abc"
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await workspace_files.upload("Inputs/interrupted.bin", interrupted())

    inputs = workspace_files.project_root / "Inputs"
    assert not (inputs / "interrupted.bin").exists()
    assert list(inputs.glob(".manyselves-tmp-*")) == []


@pytest.mark.asyncio
async def test_upload_limit_is_checked_while_streaming(workspace_files: WorkspaceFiles) -> None:
    """An upload crossing the configured limit must stop without publishing the file."""

    async def chunks():
        yield b"1234"
        yield b"56789"
        raise AssertionError("stream should stop after crossing the limit")

    with pytest.raises(UploadTooLarge):
        await workspace_files.upload("Inputs/large.bin", chunks())

    assert not (workspace_files.project_root / "Inputs" / "large.bin").exists()


def test_preview_dtos_are_bounded_passive_and_path_sanitized(
    workspace_files: WorkspaceFiles,
) -> None:
    """Preview responses must not expose server paths or active SVG/Markdown content."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg" onload="evil()"><script>x</script><a href="javascript:evil()">x</a></svg>'
    workspace_files.create_file("Inputs/unsafe.svg", svg)
    workspace_files.create_file("Inputs/unsafe.md", "# ok\n<script>evil()</script>")
    service = PreviewService(workspace_files, max_preview_bytes=1024, row_limit=2, column_limit=2)

    svg_dto = service.preview(
        "Inputs/unsafe.svg", content_url="/api/v1/projects/p1/files/download?path=Inputs%2Funsafe.svg"
    )
    markdown_dto = service.preview(
        "Inputs/unsafe.md", content_url="/api/v1/projects/p1/files/download?path=Inputs%2Funsafe.md"
    )
    serialized = f"{svg_dto!r}{markdown_dto!r}"

    assert svg_dto["type"] == "image"
    assert "script" not in svg_dto["content"].casefold()
    assert "onload" not in svg_dto["content"].casefold()
    assert "javascript:" not in svg_dto["content"].casefold()
    assert markdown_dto["type"] == "markdown"
    assert "<script" not in markdown_dto["content"].casefold()
    assert str(workspace_files.project_root) not in serialized


def test_svg_preview_uses_strict_passive_allowlist(workspace_files: WorkspaceFiles) -> None:
    """SVG preview must remove styling, animation, embedded media, and external IRIs."""
    malicious = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">
      <style>@import url(https://evil.example/a.css);</style>
      <defs><linearGradient id="safe"><stop offset="0" stop-color="#fff"/></linearGradient></defs>
      <path d="M0 0L1 1" fill="url(#safe)" style="filter:url(https://evil.example/f)"/>
      <animate attributeName="x" from="0" to="1"/>
      <image href="https://evil.example/x.png"/>
      <use href="data:image/svg+xml,evil"/>
      <a href="javascript:evil()"><text>bad</text></a>
      <foreignObject><div>html</div></foreignObject>
    </svg>"""
    workspace_files.create_file("Inputs/malicious.svg", malicious)

    dto = PreviewService(workspace_files, max_preview_bytes=4096).preview(
        "Inputs/malicious.svg", content_url="/raw/original.svg"
    )
    content = dto["content"].casefold()

    assert dto["type"] == "image"
    assert "contentUrl" not in dto
    assert "<style" not in content
    assert "style=" not in content
    assert "animate" not in content
    assert "<image" not in content
    assert "foreignobject" not in content
    assert "https:" not in content
    assert "javascript:" not in content
    assert "data:" not in content
    assert "url(#safe)" in content


def test_svg_preview_requires_svg_root(workspace_files: WorkspaceFiles) -> None:
    """Treating arbitrary XML as image/svg+xml would bypass the SVG safety contract."""
    workspace_files.create_file("Inputs/not-svg.svg", "<html><script>x</script></html>")

    with pytest.raises(InvalidPreviewDocument):
        PreviewService(workspace_files).preview(
            "Inputs/not-svg.svg", content_url="/raw/not-svg.svg"
        )


def test_preview_reports_invalid_text_encoding_explicitly(workspace_files: WorkspaceFiles) -> None:
    """Binary bytes must not be silently replaced and presented as valid text."""
    (workspace_files.project_root / "Inputs" / "bad.md").write_bytes(b"\xff\xfe")

    with pytest.raises(PreviewEncodingError):
        PreviewService(workspace_files).preview(
            "Inputs/bad.md", content_url="/api/v1/projects/p1/files/download?path=Inputs%2Fbad.md"
        )


def test_image_preview_reports_dimensions_without_embedding_server_path(
    workspace_files: WorkspaceFiles,
) -> None:
    """Image preview metadata must be passive and client-addressable."""
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    (workspace_files.project_root / "Inputs" / "pixel.png").write_bytes(png)

    dto = PreviewService(workspace_files).preview(
        "Inputs/pixel.png", content_url="/content/pixel.png"
    )

    assert dto == {
        "type": "image",
        "path": "Inputs/pixel.png",
        "mimeType": "image/png",
        "width": 1,
        "height": 1,
        "contentUrl": "/content/pixel.png",
    }


def test_spreadsheet_preview_limits_rows_columns_and_does_not_evaluate_formulas(
    workspace_files: WorkspaceFiles,
) -> None:
    """Spreadsheet parsing must expose a bounded value window without executing formulas."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["a", "b", "c"])
    sheet.append([1, 2, "=1+1"])
    sheet.append([3, 4, 5])
    workbook.save(workspace_files.project_root / "Inputs" / "data.xlsx")
    workbook.close()

    dto = PreviewService(
        workspace_files,
        max_preview_bytes=64 * 1024,
        row_limit=2,
        column_limit=2,
    ).preview("Inputs/data.xlsx", content_url="/content/data.xlsx")

    assert dto["type"] == "spreadsheet"
    assert dto["sheets"] == [
        {
            "name": "Data",
            "rowCount": 3,
            "columnCount": 3,
            "rows": [["a", "b"], ["1", "2"]],
            "truncated": True,
        }
    ]


def test_csv_preview_streams_counts_and_bounds_cells(workspace_files: WorkspaceFiles) -> None:
    """Delimiter-heavy CSV must retain only the configured window and bounded cell text."""
    csv_text = "x" * 5000 + ",b,c\n" + "1," * 5000 + "end\n" + "z,z,z\n" * 50
    (workspace_files.project_root / "Inputs" / "wide.csv").write_text(csv_text, encoding="utf-8")

    dto = PreviewService(
        workspace_files,
        max_preview_bytes=64 * 1024,
        row_limit=2,
        column_limit=3,
        cell_character_limit=16,
    ).preview("Inputs/wide.csv", content_url="/content/wide.csv")
    sheet = dto["sheets"][0]

    assert sheet["rowCount"] == 52
    assert sheet["columnCount"] == 5001
    assert len(sheet["rows"]) == 2
    assert len(sheet["rows"][0]) == 3
    assert sheet["rows"][0][0] == "x" * 16
    assert all(len(cell) <= 16 for row in sheet["rows"] for cell in row)
    assert sheet["truncated"] is True


def test_csv_preflight_rejects_delimiter_amplification_before_reader_materialization(
    workspace_files: WorkspaceFiles,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large physical row with excessive fields must never reach csv.reader."""
    adversarial = ("x," * 500_000) + "end\n"
    (workspace_files.project_root / "Inputs" / "adversarial.csv").write_text(
        adversarial,
        encoding="utf-8",
    )
    reader_called = False

    def forbidden_reader(*args, **kwargs):
        nonlocal reader_called
        reader_called = True
        raise AssertionError("csv.reader must not receive an oversized record")

    monkeypatch.setattr("manyselves.application.preview_service.csv.reader", forbidden_reader)

    with pytest.raises(PreviewTooLarge):
        PreviewService(
            workspace_files,
            max_preview_bytes=2 * 1024 * 1024,
            csv_record_byte_limit=256 * 1024,
            csv_field_limit=64,
        ).preview("Inputs/adversarial.csv", content_url="/content/adversarial.csv")

    assert reader_called is False


def test_csv_preflight_handles_quoted_multiline_and_malformed_records(
    workspace_files: WorkspaceFiles,
) -> None:
    """Quoted delimiters are fields, multiline records stay bounded, and bad quotes fail."""
    inputs = workspace_files.project_root / "Inputs"
    (inputs / "quoted.csv").write_text('"a,b",c\n"x\ny",z\n', encoding="utf-8")
    (inputs / "oversized-multiline.csv").write_text(
        '"' + ("long-line\n" * 8) + '",z\n',
        encoding="utf-8",
    )
    (inputs / "malformed.csv").write_text('"unterminated\n', encoding="utf-8")
    (inputs / "mid-field-quote.csv").write_text(
        'prefix",x"\n',
        encoding="utf-8",
    )
    service = PreviewService(
        workspace_files,
        max_preview_bytes=4096,
        csv_record_byte_limit=32,
        csv_field_limit=2,
    )

    quoted = service.preview("Inputs/quoted.csv", content_url="/content/quoted.csv")

    assert quoted["sheets"][0]["rowCount"] == 2
    assert quoted["sheets"][0]["columnCount"] == 2
    with pytest.raises(PreviewTooLarge):
        service.preview(
            "Inputs/oversized-multiline.csv",
            content_url="/content/oversized-multiline.csv",
        )
    with pytest.raises(InvalidPreviewDocument):
        service.preview("Inputs/malformed.csv", content_url="/content/malformed.csv")
    with pytest.raises(InvalidPreviewDocument):
        service.preview("Inputs/mid-field-quote.csv", content_url="/content/mid-field-quote.csv")


def test_spreadsheet_preview_caps_sheets_and_archive_members(
    workspace_files: WorkspaceFiles,
) -> None:
    """Workbook and ZIP metadata must not create unbounded preview collections."""
    workbook = Workbook()
    workbook.active.title = "s0"
    for index in range(1, 5):
        workbook.create_sheet(f"s{index}")
    workbook.save(workspace_files.project_root / "Inputs" / "many-sheets.xlsx")
    workbook.close()

    dto = PreviewService(
        workspace_files,
        max_preview_bytes=128 * 1024,
        sheet_limit=2,
        archive_member_limit=100,
    ).preview("Inputs/many-sheets.xlsx", content_url="/content/many-sheets.xlsx")

    assert [sheet["name"] for sheet in dto["sheets"]] == ["s0", "s1"]
    assert dto["sheetsTruncated"] is True

    archive_path = workspace_files.project_root / "Inputs" / "many-members.xlsx"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index in range(6):
            archive.writestr(f"member-{index}.xml", "x")

    with pytest.raises(PreviewTooLarge):
        PreviewService(
            workspace_files,
            max_preview_bytes=128 * 1024,
            archive_member_limit=4,
        ).preview("Inputs/many-members.xlsx", content_url="/content/many-members.xlsx")


def test_docx_preview_preserves_passive_paragraph_table_image_order(
    workspace_files: WorkspaceFiles,
) -> None:
    """DOCX preview must extract passive blocks without exposing or executing package content."""
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    document = Document()
    document.add_paragraph("intro")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "left"
    table.cell(0, 1).text = "right"
    document.add_picture(io.BytesIO(png))
    document.save(workspace_files.project_root / "Inputs" / "report.docx")

    dto = PreviewService(workspace_files, max_preview_bytes=128 * 1024).preview(
        "Inputs/report.docx", content_url="/content/report.docx"
    )

    assert dto["type"] == "docx"
    assert [block["type"] for block in dto["blocks"]] == ["paragraph", "table", "image"]
    assert dto["blocks"][0]["text"] == "intro"
    assert dto["blocks"][1]["rows"] == [["left", "right"]]
    assert dto["blocks"][2]["dataUrl"].startswith("data:image/png;base64,")
    assert str(workspace_files.project_root) not in repr(dto)
