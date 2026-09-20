"""Agent tools for Manyselves."""

from .contracts import (
    NormalizedToolCall,
    mechanical_tool_correction,
    normalize_tool_call,
)
from .document_tool import InspectDocumentTool
from .exec_tools import ExecTool
from .file_state import FileStateManager
from .file_tools import ApplyPatchTool, DeleteFileTool, ReadTool
from .manifest_tool import ManifestManager, ManifestTool
from .pdf_tool import PDFParseTool
from .registry import Tool, ToolRegistry
from .result_memory import ResultMemory, RunToolResultIndex, ToolResultMemory
from .task_board import TaskBoard
from .task_tools import ManageTasksTool

__all__ = [
    "Tool",
    "ToolRegistry",
    "NormalizedToolCall",
    "normalize_tool_call",
    "mechanical_tool_correction",
    "RunToolResultIndex",
    "ResultMemory",
    "ToolResultMemory",
    "TaskBoard",
    "ManageTasksTool",
    "ReadTool",
    "ApplyPatchTool",
    "DeleteFileTool",
    "ManifestManager",
    "ManifestTool",
    "ExecTool",
    "InspectDocumentTool",
    "PDFParseTool",
    "FileStateManager",
]
