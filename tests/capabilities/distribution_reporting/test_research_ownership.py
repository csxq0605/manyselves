"""Characterization for Capability-owned reporting research runtime modules."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec

RESEARCH_MODULES = {
    "tools": "manyselves.capabilities.distribution_reporting.runtime.research_tools",
    "reference": (
        "manyselves.capabilities.distribution_reporting.runtime.research.reference_library"
    ),
    "web": "manyselves.capabilities.distribution_reporting.runtime.research.web",
    "memory": (
        "manyselves.capabilities.distribution_reporting.runtime.research.evidence_memory"
    ),
    "context": (
        "manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context"
    ),
}


def test_research_runtime_imports_without_core_reporting() -> None:
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
                        for alias, module_name in RESEARCH_MODULES.items()
                    ),
                    "print(json.dumps({",
                    "    'modules': sorted((",
                    *(f"        {alias}.__name__," for alias in RESEARCH_MODULES),
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

    payload = json.loads(completed.stdout)
    assert payload == {
        "modules": sorted(RESEARCH_MODULES.values()),
        "core_reporting": [],
    }


def test_research_symbols_are_physically_capability_owned() -> None:
    expected_symbols = {
        "tools": (
            "SearchProjectEvidenceTool",
            "OpenProjectSourceTool",
            "SearchReferenceLibraryTool",
            "OpenReferenceTool",
            "WebSearchTool",
            "OpenWebSourceTool",
            "PublishResearchNoteTool",
        ),
        "reference": ("ReferenceLibrary", "ReferenceDocument", "ReferenceHit"),
        "web": (
            "BraveWebResearchBackend",
            "DisabledWebResearchBackend",
            "WebSearchHit",
            "OpenedWebSource",
        ),
        "memory": ("EvidenceResearchMemory",),
        "context": ("KnowledgeContext", "KnowledgeContextBuilder"),
    }
    for alias, module_name in RESEARCH_MODULES.items():
        module = import_module(module_name)
        for symbol_name in expected_symbols[alias]:
            assert getattr(module, symbol_name).__module__ == module_name

    assert find_spec("manyselves.core.tools.reporting_research_tools") is None
    assert find_spec("manyselves.core.reporting.research.reference_library") is None
    assert find_spec("manyselves.core.reporting.research.web") is None
    assert find_spec("manyselves.core.reporting.research.evidence_memory") is None
    assert find_spec("manyselves.core.reporting.research.knowledge_context") is None
