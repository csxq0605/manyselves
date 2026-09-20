"""Capability ownership of the expert-template isolation rule."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.template_access import (
    FORBIDDEN_AGENT_ACCESS_ERROR,
    reject_forbidden_agent_document,
)
from manyselves.runtime.tools.document_tool import InspectDocumentTool


@pytest.mark.asyncio
async def test_reporting_injects_template_isolation_into_generic_document_tool(
    tmp_path: Path,
) -> None:
    template = tmp_path / "Templates" / "配电安全专家咨询报告(专家优化版).docx"
    template.parent.mkdir()
    template.write_bytes(b"isolated")
    tool = InspectDocumentTool(
        tmp_path,
        path_validator=reject_forbidden_agent_document,
    )

    with pytest.raises(PermissionError, match=FORBIDDEN_AGENT_ACCESS_ERROR):
        await tool(path="Templates/配电安全专家咨询报告(专家优化版).docx")


def test_reporting_distiller_may_read_its_isolated_snapshot() -> None:
    reject_forbidden_agent_document(
        "Work/runs/run-1/templates/template-for-skill.docx",
        allow_template_distiller=True,
    )
