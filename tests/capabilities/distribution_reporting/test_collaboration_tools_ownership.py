"""Physical ownership characterization for Reporting collaboration Tools."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.util import find_spec

COLLABORATION_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.collaboration_tools"
)
MESSAGE_ROUTER_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.message_router"
)
SYMBOLS = (
    "SubmitResultTool",
    "WriteResultPartTool",
    "ListResultPartsTool",
    "ReportBlockedTool",
    "QueryPeerTool",
    "ReplyPeerTool",
    "ReportGapTool",
    "PeerMessageRouter",
)


def test_collaboration_tools_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {COLLABORATION_MODULE} as module",
                    f"import {MESSAGE_ROUTER_MODULE} as router",
                    "print(json.dumps({",
                    "    'module': module.__name__,",
                    "    'router': router.__name__,",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "module": COLLABORATION_MODULE,
        "router": MESSAGE_ROUTER_MODULE,
        "core_reporting": [],
    }


def test_collaboration_tools_are_physically_capability_owned() -> None:
    module = __import__(COLLABORATION_MODULE, fromlist=list(SYMBOLS))
    for symbol in SYMBOLS:
        assert getattr(module, symbol).__module__ == COLLABORATION_MODULE

    router = __import__(MESSAGE_ROUTER_MODULE, fromlist=["WorkflowMessageRouter"])
    assert router.WorkflowMessageRouter.__module__ == MESSAGE_ROUTER_MODULE
    assert router.artifact_path_refs.__module__ == MESSAGE_ROUTER_MODULE
    assert router.source_record_ids.__module__ == MESSAGE_ROUTER_MODULE
    assert find_spec("manyselves.core.tools.reporting_collaboration_tools") is None
    assert find_spec("manyselves.core.reporting.message_router") is None
