"""Final artifact renderers."""

from .handoff_docx import HandoffDocxCore, PackagedV2DocxCore
from .packaged_docx import PackagedDocxCore
from .contracts import RenderRequest, RenderResult
from .pds_docx_renderer import (
    ApprovedReport,
    PdsDocxRenderer,
    PdsRenderResult,
    ReportPhoto,
    ReportTable,
)
from .source_index_docx_renderer import (
    SourceIndexDocxRenderer,
    SourceIndexRenderResult,
)

__all__ = [
    "ApprovedReport",
    "HandoffDocxCore",
    "PackagedV2DocxCore",
    "PackagedDocxCore",
    "PdsDocxRenderer",
    "PdsRenderResult",
    "ReportPhoto",
    "ReportTable",
    "RenderRequest",
    "RenderResult",
    "SourceIndexDocxRenderer",
    "SourceIndexRenderResult",
]
