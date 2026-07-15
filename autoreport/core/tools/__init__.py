"""Agent tools for AutoReport."""

from .agent_tools import RespondTool, SendToAgentTool
from .exec_tools import ExecTool
from .file_state import FileStateManager
from .file_tools import ApplyPatchTool, DeleteFileTool, ReadTool
from .manifest_tool import ManifestManager, ManifestTool
from .pdf_tool import PDFParseTool
from .registry import Tool, ToolRegistry
from .skill_tool import LoadSkillTool, SkillLoader
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
    "PDFParseTool",
    "SendToAgentTool",
    "RespondTool",
    "RunReportingWorkflowTool",
    "FileStateManager",
    "SkillLoader",
    "LoadSkillTool",
]


def __getattr__(name: str):
    """Keep reporting workflow import lazy to avoid AgentLoop/tool cycles."""
    if name == "RunReportingWorkflowTool":
        from .reporting_tool import RunReportingWorkflowTool

        return RunReportingWorkflowTool
    raise AttributeError(name)
