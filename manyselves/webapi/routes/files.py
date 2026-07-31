"""Project-scoped file, upload, download, range, and preview routes."""

import asyncio
import mimetypes
from collections.abc import AsyncIterator
from io import BufferedReader
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from starlette.responses import StreamingResponse

from ...application.control import ControlLeaseRequired
from ...application.errors import RuntimeNotReadyError
from ...application.preview_service import (
    InvalidPreviewDocument,
    PreviewEncodingError,
    PreviewService,
    PreviewTooLarge,
    UnsupportedPreview,
)
from ...application.project_registry import InvalidProjectId, ProjectRegistryError
from ...application.workspace_files import (
    DestinationExists,
    FileEntry,
    FileRevisionConflict,
    TextFile,
    TextFileTooLarge,
    UnsafeWorkspacePath,
    UploadTooLarge,
    WorkspaceEntryNotFound,
    WorkspaceEntryTypeError,
    WorkspaceFileError,
    WorkspaceFiles,
)
from ..errors import ApiError
from ..schemas.files import (
    CreateEntryRequest,
    FileContent,
    FileEntryResponse,
    FileTreeResponse,
    RenameEntryRequest,
    SaveFileRequest,
)
from ..security import require_control_lease_header, require_deployment_access

router = APIRouter(prefix="/projects/{project_id}/files")
_STREAM_CHUNK_SIZE = 64 * 1024


def _file_service(request: Request, project_id: str) -> WorkspaceFiles:
    registry = request.app.state.project_registry
    settings = request.app.state.web_settings
    try:
        root = registry.project_root(project_id)
    except ProjectRegistryError as error:
        raise ApiError(
            status_code=(
                status.HTTP_400_BAD_REQUEST
                if isinstance(error, InvalidProjectId)
                else status.HTTP_404_NOT_FOUND
            ),
            code=error.code,
            message=str(error),
            retryable=False,
        ) from error
    return WorkspaceFiles(
        root,
        max_text_bytes=settings.text_file_size_limit_bytes,
        max_upload_bytes=settings.upload_size_limit_bytes,
        max_tree_entries=settings.file_tree_entry_limit,
    )


def _preview_service(request: Request, files: WorkspaceFiles) -> PreviewService:
    settings = request.app.state.web_settings
    return PreviewService(
        files,
        max_preview_bytes=settings.preview_size_limit_bytes,
        max_archive_expanded_bytes=settings.preview_archive_expanded_limit_bytes,
        row_limit=settings.preview_row_limit,
        column_limit=settings.preview_column_limit,
        block_limit=settings.preview_block_limit,
        archive_member_limit=settings.preview_archive_member_limit,
        sheet_limit=settings.preview_sheet_limit,
        cell_character_limit=settings.preview_cell_character_limit,
    )


def _entry_response(entry: FileEntry) -> FileEntryResponse:
    return FileEntryResponse(
        path=entry.path,
        name=entry.name,
        kind=entry.kind,
        size=entry.size,
        modifiedAt=entry.modified_at,
        revision=entry.revision,
    )


def _content_response(content: TextFile) -> FileContent:
    return FileContent(
        path=content.path,
        revision=content.revision,
        size=content.size,
        modifiedAt=content.modified_at,
        content=content.content,
    )


def _file_error(error: Exception) -> ApiError:
    if isinstance(error, ControlLeaseRequired):
        return ApiError(
            status_code=status.HTTP_423_LOCKED,
            code=error.code,
            message=str(error),
            retryable=False,
        )
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=error.code,
            message=str(error),
            retryable=True,
        )
    if isinstance(error, UnsafeWorkspacePath):
        code = status.HTTP_400_BAD_REQUEST
    elif isinstance(error, WorkspaceEntryNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, (DestinationExists, FileRevisionConflict)):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, (UploadTooLarge, TextFileTooLarge, PreviewTooLarge)):
        code = status.HTTP_413_CONTENT_TOO_LARGE
    elif isinstance(error, UnsupportedPreview):
        code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    elif isinstance(error, (PreviewEncodingError, InvalidPreviewDocument)):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif isinstance(error, WorkspaceEntryTypeError):
        code = status.HTTP_400_BAD_REQUEST
    elif isinstance(error, WorkspaceFileError):
        code = status.HTTP_400_BAD_REQUEST
    else:
        raise error
    assert isinstance(error, WorkspaceFileError)
    return ApiError(
        status_code=code,
        code=error.code,
        message=str(error),
        retryable=False,
    )


@router.get("/tree", response_model=FileTreeResponse)
async def file_tree(
    project_id: str,
    request: Request,
    path: str = Query(default=""),
) -> FileTreeResponse:
    files = _file_service(request, project_id)
    try:
        async with request.app.state.runtime_facade.read_transaction():
            return FileTreeResponse(entries=[_entry_response(item) for item in files.list_tree(path)])
    except WorkspaceFileError as error:
        raise _file_error(error) from error


@router.get("/content", response_model=FileContent)
async def read_file(project_id: str, request: Request, path: str = Query()) -> FileContent:
    files = _file_service(request, project_id)
    try:
        async with request.app.state.runtime_facade.read_transaction():
            return _content_response(files.read_text(path))
    except (WorkspaceFileError, UnicodeDecodeError) as error:
        if isinstance(error, UnicodeDecodeError):
            error = PreviewEncodingError()
        raise _file_error(error) from error


@router.put("/content", response_model=FileContent)
async def save_file(
    project_id: str,
    body: SaveFileRequest,
    request: Request,
    path: str = Query(),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> FileContent:
    files = _file_service(request, project_id)
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _content_response(files.write_text(path, body.content, body.base_revision))
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error


@router.post("/entries", response_model=FileEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_entry(
    project_id: str,
    body: CreateEntryRequest,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> FileEntryResponse:
    files = _file_service(request, project_id)
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            if body.kind == "file":
                entry = files.entry(files.create_file(body.path, body.content).path)
            else:
                if body.content:
                    raise WorkspaceEntryTypeError()
                entry = files.create_directory(body.path)
            return _entry_response(entry)
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error


@router.post("/rename", response_model=FileEntryResponse)
async def rename_entry(
    project_id: str,
    body: RenameEntryRequest,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> FileEntryResponse:
    files = _file_service(request, project_id)
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _entry_response(
                files.rename(body.source, body.destination, body.base_revision)
            )
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error


@router.delete("/entries", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    project_id: str,
    request: Request,
    path: str = Query(),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
    if_match: str = Header(alias="If-Match"),
) -> Response:
    files = _file_service(request, project_id)
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            files.delete(path, _if_match_revision(if_match))
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/upload", response_model=FileEntryResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    project_id: str,
    request: Request,
    path: str = Query(),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> FileEntryResponse:
    files = _file_service(request, project_id)
    try:
        content_length = request.headers.get("Content-Length")
        if content_length is not None and int(content_length) > files.max_upload_bytes:
            raise UploadTooLarge()
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _entry_response(await files.upload(path, request.stream()))
    except ValueError as error:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="INVALID_CONTENT_LENGTH",
            message="Content-Length is invalid",
            retryable=False,
        ) from error
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error


@router.get("/download")
async def download_file(
    project_id: str,
    request: Request,
    path: str = Query(),
) -> StreamingResponse:
    files = _file_service(request, project_id)
    opened = None
    try:
        async with request.app.state.runtime_facade.read_transaction():
            opened = files.open_download(path)
            byte_range = _parse_range(request.headers.get("Range"), opened.size)
    except WorkspaceFileError as error:
        raise _file_error(error) from error
    except BaseException:
        if opened is not None:
            opened.stream.close()
        raise
    assert opened is not None
    if byte_range is None:
        start, end, response_status = 0, opened.size - 1, status.HTTP_200_OK
    else:
        start, end = byte_range
        response_status = status.HTTP_206_PARTIAL_CONTENT
    length = max(0, end - start + 1)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": _content_disposition(opened.name),
        "ETag": f'"{opened.revision}"',
    }
    if response_status == status.HTTP_206_PARTIAL_CONTENT:
        headers["Content-Range"] = f"bytes {start}-{end}/{opened.size}"
    return StreamingResponse(
        _stream_range(opened.stream, start, length),
        status_code=response_status,
        media_type=mimetypes.guess_type(opened.name)[0] or "application/octet-stream",
        headers=headers,
    )


@router.get("/preview", response_model=dict)
async def preview_file(project_id: str, request: Request, path: str = Query()) -> dict:
    files = _file_service(request, project_id)
    content_url = f"/api/v1/projects/{project_id}/files/download?{urlencode({'path': path})}"
    try:
        service = _preview_service(request, files)
        async with request.app.state.runtime_facade.read_transaction():
            capture = service.capture(path)
        return await asyncio.to_thread(
            service.preview_capture,
            capture,
            content_url=content_url,
        )
    except WorkspaceFileError as error:
        raise _file_error(error) from error


def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value or size <= 0:
        raise _range_error(size)
    spec = value[6:]
    start_text, separator, end_text = spec.partition("-")
    if not separator:
        raise _range_error(size)
    try:
        if not start_text:
            if not _ascii_digits(end_text):
                raise ValueError
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            return max(0, size - suffix), size - 1
        if not _ascii_digits(start_text) or (end_text and not _ascii_digits(end_text)):
            raise ValueError
        start = int(start_text)
        end = size - 1 if not end_text else int(end_text)
        if start < 0 or end < start or start >= size:
            raise ValueError
        return start, min(end, size - 1)
    except ValueError as error:
        raise _range_error(size) from error


async def _stream_range(
    stream: BufferedReader,
    start: int,
    length: int,
) -> AsyncIterator[bytes]:
    remaining = length
    try:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(_STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()


def _range_error(size: int) -> ApiError:
    return ApiError(
        status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
        code="INVALID_BYTE_RANGE",
        message="Requested byte range is invalid",
        retryable=False,
        headers={"Content-Range": f"bytes */{size}"},
    )


def _ascii_digits(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit()


def _if_match_revision(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2:
        candidate = candidate[1:-1]
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="INVALID_FILE_REVISION",
            message="If-Match must contain one SHA-256 file revision",
            retryable=False,
        )
    return candidate


def _content_disposition(name: str) -> str:
    fallback = "".join(
        character if 32 <= ord(character) < 127 and character not in {'"', "\\"} else "_"
        for character in name
    ).replace("\r", "_").replace("\n", "_")
    fallback = fallback or "download"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'
