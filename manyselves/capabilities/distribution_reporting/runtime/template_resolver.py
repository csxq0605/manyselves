"""Capability-owned report template selection for Delivery preparation."""

from __future__ import annotations

from pathlib import Path

_PROJECT_TEMPLATE_PATH = Path("Templates/report_template.docx")


def resolve_report_template(
    workspace: Path,
    run_id: str | None = None,
) -> tuple[Path, str]:
    """Resolve the current project template, falling back to the packaged one."""

    workspace = Path(workspace)
    project_template = (
        workspace
        / "Work"
        / "runs"
        / run_id
        / "frozen-project"
        / _PROJECT_TEMPLATE_PATH
        if run_id is not None
        else workspace / _PROJECT_TEMPLATE_PATH
    )
    if project_template.exists():
        if not project_template.is_file():
            raise ValueError(
                f"project report template must be a DOCX file: {_PROJECT_TEMPLATE_PATH}"
            )
        if not project_template.resolve().is_relative_to(workspace):
            raise ValueError("project report template must stay inside the project workspace")
        return project_template, "project"
    packaged_report_template_path = (
        Path(__file__).resolve().parents[3]
        / "templates"
        / "reporting"
        / "report_template.docx"
    )
    if not packaged_report_template_path.is_file():
        raise FileNotFoundError(
            f"packaged report template is missing: {packaged_report_template_path}"
        )
    return packaged_report_template_path, "packaged"


__all__ = ["resolve_report_template"]
