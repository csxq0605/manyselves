"""Narrow adapter for the V2 handoff package's deterministic DOCX core."""

from __future__ import annotations

import hashlib
import importlib.util
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

_CORE_LOCK = threading.RLock()


class HandoffDocxCore:
    """Load and invoke the handoff renderer without its Dify wrapper.

    The handoff core can synthesize prose from ``report_model``. Manyselves'
    agentic path intentionally disables that API so approved Agent/Chief Editor
    prose is the sole content input.
    """

    def __init__(self, source_path: Path, *, template_path: Path | None = None):
        self.source_path = Path(source_path)
        self.template_path = Path(template_path) if template_path is not None else None
        if not self.source_path.is_file():
            raise FileNotFoundError(f"handoff DOCX core not found: {self.source_path}")
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is not None:
            return self._module
        digest = hashlib.sha256(str(self.source_path).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(f"manyselves_handoff_docx_{digest}", self.source_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load handoff DOCX core: {self.source_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "render_report_docx", None)):
            raise ImportError("handoff DOCX core has no render_report_docx entry point")
        self._module = module
        return module

    def render_approved_prose(
        self,
        report_text: str,
        *,
        filename: str | None = None,
        report_model: Any = None,
    ) -> tuple[str, bytes]:
        if report_model is not None:
            raise ValueError(
                "structured-model prose generation is disabled; pass approved report prose only"
            )
        with _CORE_LOCK:
            module = self._load()
            original_template = getattr(module, "TEMPLATE_PATH", None)
            try:
                if self.template_path is not None:
                    module.TEMPLATE_PATH = self.template_path
                return module.render_report_docx(
                    report_text=report_text,
                    filename=filename,
                    report_model=None,
                )
            finally:
                if original_template is not None:
                    module.TEMPLATE_PATH = original_template
