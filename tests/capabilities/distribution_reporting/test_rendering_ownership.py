"""Characterization for Capability-owned deterministic report rendering."""

import io
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

from docx import Document

RENDERING_ROOT = "manyselves.capabilities.distribution_reporting.runtime.rendering"
MODULES = {
    "contracts": f"{RENDERING_ROOT}.contracts",
    "handoff": f"{RENDERING_ROOT}.handoff_docx",
    "packaged": f"{RENDERING_ROOT}.packaged_docx",
    "pds": f"{RENDERING_ROOT}.pds_docx_renderer",
    "source_index": f"{RENDERING_ROOT}.source_index_docx_renderer",
    "v2": f"{RENDERING_ROOT}.v2_docx_renderer",
}
OLD_MODULES = {
    "contracts": "manyselves.core.reporting.rendering.contracts",
    "handoff": "manyselves.core.reporting.rendering.handoff_docx",
    "packaged": "manyselves.core.reporting.rendering.packaged_docx",
    "pds": "manyselves.core.reporting.rendering.pds_docx_renderer",
    "source_index": "manyselves.core.reporting.rendering.source_index_docx_renderer",
    "v2": "manyselves.core.reporting.rendering.v2_docx_renderer",
}


def test_rendering_modules_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    *(
                        f"import {module_name} as {alias}"
                        for alias, module_name in MODULES.items()
                    ),
                    "print(json.dumps({",
                    "    'modules': sorted((",
                    *(f"        {alias}.__name__," for alias in MODULES),
                    "    )),",
                    "    'core_reporting': sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "modules": sorted(MODULES.values()),
        "core_reporting": [],
    }


def test_rendering_symbols_are_physically_capability_owned() -> None:
    expected_symbols = {
        "contracts": ("RenderRequest", "RenderResult"),
        "handoff": ("HandoffDocxCore", "PackagedV2DocxCore"),
        "packaged": ("PackagedDocxCore", "verify_rendered_markdown"),
        "pds": ("ApprovedReport", "PdsDocxRenderer"),
        "source_index": ("SourceIndexDocxRenderer",),
        "v2": ("render_report_docx",),
    }
    for alias, module_name in MODULES.items():
        module = import_module(module_name)
        for symbol_name in expected_symbols[alias]:
            assert getattr(module, symbol_name).__module__ == module_name

    for alias in MODULES:
        try:
            old_spec = find_spec(OLD_MODULES[alias])
        except ModuleNotFoundError:
            old_spec = None
        assert old_spec is None


def test_capability_render_report_docx_preserves_minimal_approved_markdown() -> None:
    renderer = import_module(MODULES["v2"])
    assert Path(renderer.TEMPLATE_PATH).is_file()

    filename, data = renderer.render_report_docx(
        report_text="# 配电安全专家咨询报告\n\n## 1. 配电评估概述\n\n关键正文保留。",
        filename="minimal-approved.docx",
    )

    document = Document(io.BytesIO(data))
    visible_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert filename == "minimal-approved.docx"
    assert "1. 配电评估概述" in visible_text
    assert "关键正文保留。" in visible_text
