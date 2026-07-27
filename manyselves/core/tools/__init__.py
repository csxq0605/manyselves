"""Agent tools for Manyselves."""

from .exec_tools import ExecTool
from .document_tool import InspectDocumentTool
from .file_state import FileStateManager
from .file_tools import ApplyPatchTool, DeleteFileTool, ReadTool
from .manifest_tool import ManifestManager, ManifestTool
from .pdf_tool import PDFParseTool
from .registry import Tool, ToolRegistry
from .task_board import TaskBoard
from .task_tools import ManageTasksTool

__all__ = [
    "Tool",
    "ToolRegistry",
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
    "RunReportingWorkflowTool",
    "CancelReportingWorkflowTool",
    "GetReportingWorkflowStatusTool",
    "ResumeReportingWorkflowTool",
    "ReviseReportingWorkflowTool",
    "FileStateManager",
]


def __getattr__(name: str):
    """Keep reporting workflow import lazy to avoid AgentLoop/tool cycles."""
    if name in {
        "RunReportingWorkflowTool",
        "CancelReportingWorkflowTool",
        "GetReportingWorkflowStatusTool",
        "ResumeReportingWorkflowTool",
        "ReviseReportingWorkflowTool",
    }:
        from .reporting_tool import (
            CancelReportingWorkflowTool,
            GetReportingWorkflowStatusTool,
            ResumeReportingWorkflowTool,
            ReviseReportingWorkflowTool,
            RunReportingWorkflowTool,
        )

        return {
            "RunReportingWorkflowTool": RunReportingWorkflowTool,
            "CancelReportingWorkflowTool": CancelReportingWorkflowTool,
            "GetReportingWorkflowStatusTool": GetReportingWorkflowStatusTool,
            "ResumeReportingWorkflowTool": ResumeReportingWorkflowTool,
            "ReviseReportingWorkflowTool": ReviseReportingWorkflowTool,
        }[name]
    raise AttributeError(name)
