"""Fixed-root global knowledge file, preview, upload, and download routes."""

import asyncio
import mimetypes
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from starlette.responses import StreamingResponse

from ...application.control import ControlLeaseRequired
from ...application.errors import RuntimeNotReadyError
from ...application.global_knowledge_service import GlobalKnowledgeService
from ...application.preview_service import PreviewEncodingError
from ...application.workspace_files import (
    FileEntry,
    TextFile,
    UploadTooLarge,
    WorkspaceFileError,
)
from ..dependencies import get_global_knowledge_service
from ..errors import ApiError
from ..schemas.files import UploadConflict
from ..schemas.global_knowledge import (
    GlobalKnowledgeFileContent,
    GlobalKnowledgeFileEntryResponse,
    GlobalKnowledgeFileTreeResponse,
    GlobalKnowledgePreviewResponse,
    GlobalKnowledgeSaveRequest,
)
from ..security import require_authenticated_session, require_control_lease_header
from .files import (
    _content_disposition,
    _file_error,
    _if_match_revision,
    _parse_range,
    _preview_service,
    _stream_range,
    _to_thread_non_abandoning,
)

router = APIRouter(
    prefix="/global-knowledge/files",
    dependencies=[Depends(require_authenticated_session)],
)


def _service(
    service: GlobalKnowledgeService | None = Depends(get_global_knowledge_service),
) -> GlobalKnowledgeService:
    if service is None:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="GLOBAL_KNOWLEDGE_NOT_READY",
            message="Global knowledge service is not ready",
            retryable=True,
        )
    return service


def _entry_response(entry: FileEntry) -> GlobalKnowledgeFileEntryResponse:
    return GlobalKnowledgeFileEntryResponse(
        path=entry.path,
        name=entry.name,
        kind=entry.kind,
        size=entry.size,
        modifiedAt=entry.modified_at,
        revision=entry.revision,
    )


def _content_response(content: TextFile) -> GlobalKnowledgeFileContent:
    return GlobalKnowledgeFileContent(
        path=content.path,
        revision=content.revision,
        size=content.size,
        modifiedAt=content.modified_at,
        content=content.content,
    )


def _require_regular_file(service: GlobalKnowledgeService, path: str) -> None:
    if service.entry(path).kind != "file":
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="GLOBAL_KNOWLEDGE_MUTATION_FORBIDDEN",
            message="Global knowledge directories cannot be changed through this API",
            retryable=False,
        )


@router.get("/tree", response_model=GlobalKnowledgeFileTreeResponse)
async def global_knowledge_tree(
    request: Request,
    path: str = Query(default=""),
    service: GlobalKnowledgeService = Depends(_service),
) -> GlobalKnowledgeFileTreeResponse:
    try:
        async with request.app.state.runtime_facade.read_transaction():
            entries = await _to_thread_non_abandoning(service.list_tree, path)
        return GlobalKnowledgeFileTreeResponse(
            entries=[_entry_response(entry) for entry in entries]
        )
    except WorkspaceFileError as error:
        raise _file_error(error) from error


@router.get("/content", response_model=GlobalKnowledgeFileContent)
async def read_global_knowledge_file(
    request: Request,
    path: str = Query(),
    service: GlobalKnowledgeService = Depends(_service),
) -> GlobalKnowledgeFileContent:
    try:
        async with request.app.state.runtime_facade.read_transaction():
            return _content_response(service.read_text(path))
    except (UnicodeDecodeError, WorkspaceFileError) as error:
        if isinstance(error, UnicodeDecodeError):
            error = PreviewEncodingError()
        raise _file_error(error) from error


@router.put("/content", response_model=GlobalKnowledgeFileContent)
async def save_global_knowledge_file(
    body: GlobalKnowledgeSaveRequest,
    request: Request,
    path: str = Query(),
    lease_token: str = Depends(require_control_lease_header),
    service: GlobalKnowledgeService = Depends(_service),
) -> GlobalKnowledgeFileContent:
    try:
        _require_regular_file(service, path)
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _content_response(
                service.write_text(path, body.content, body.base_revision)
            )
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error


@router.delete("/entries", status_code=status.HTTP_204_NO_CONTENT)
async def delete_global_knowledge_file(
    request: Request,
    path: str = Query(),
    lease_token: str = Depends(require_control_lease_header),
    if_match: str = Header(alias="If-Match"),
    service: GlobalKnowledgeService = Depends(_service),
) -> Response:
    try:
        _require_regular_file(service, path)
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            service.delete(path, _if_match_revision(if_match))
    except (ControlLeaseRequired, RuntimeNotReadyError, WorkspaceFileError) as error:
        raise _file_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/upload",
    response_model=GlobalKnowledgeFileEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_global_knowledge_file(
    request: Request,
    path: str = Query(),
    conflict: UploadConflict = Query(default="reject"),
    base_revision: str | None = Query(
        default=None,
        alias="baseRevision",
        pattern=r"^[0-9a-f]{64}$",
    ),
    lease_token: str = Depends(require_control_lease_header),
    service: GlobalKnowledgeService = Depends(_service),
) -> GlobalKnowledgeFileEntryResponse:
    try:
        service.resolve(path)
        content_length = request.headers.get("Content-Length")
        if content_length is not None and int(content_length) > service.max_upload_bytes:
            raise UploadTooLarge()
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            entry = await service.upload(
                path,
                request.stream(),
                conflict=conflict,
                base_revision=base_revision,
            )
        return _entry_response(entry)
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
async def download_global_knowledge_file(
    request: Request,
    path: str = Query(),
    service: GlobalKnowledgeService = Depends(_service),
) -> StreamingResponse:
    opened = None
    try:
        async with request.app.state.runtime_facade.read_transaction():
            opened = service.open_download(path)
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


@router.get("/preview", response_model=GlobalKnowledgePreviewResponse)
async def preview_global_knowledge_file(
    request: Request,
    path: str = Query(),
    service: GlobalKnowledgeService = Depends(_service),
) -> GlobalKnowledgePreviewResponse:
    try:
        content_url = f"/api/v1/global-knowledge/files/download?{urlencode({'path': path})}"
        preview_service = _preview_service(request, service.workspace_files)
        async with request.app.state.runtime_facade.read_transaction():
            capture = preview_service.capture(path)
        internal = await asyncio.to_thread(
            preview_service.preview_capture,
            capture,
            content_url=content_url,
        )
        external = dict(internal)
        external["kind"] = external.pop("type")
        if external["kind"] == "docx":
            external["blocks"] = [
                {
                    "kind": block["type"],
                    **{key: value for key, value in block.items() if key != "type"},
                }
                for block in external["blocks"]
            ]
        return GlobalKnowledgePreviewResponse.model_validate(external)
    except WorkspaceFileError as error:
        raise _file_error(error) from error
