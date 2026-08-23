"""Characterize Capability ownership of the existing Run input snapshot."""

from __future__ import annotations

import subprocess
import sys
from importlib.util import find_spec


def test_run_input_snapshot_is_physically_owned_by_the_capability() -> None:
    module_name = (
        "manyselves.capabilities.distribution_reporting.runtime.input_snapshot"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    f"from {module_name} import RunInputSnapshotStore",
                    "assert RunInputSnapshotStore.__module__ == "
                    f"{module_name!r}",
                    "assert not any(name.startswith('manyselves.core.reporting') "
                    "for name in sys.modules)",
                )
            ),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert find_spec("manyselves.core.reporting.input_snapshot") is None
