"""Characterize Capability ownership of the existing Run input snapshot."""

from __future__ import annotations

import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path


def test_run_input_snapshot_serializes_project_paths_as_canonical_refs() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
        FrozenProjectFile,
    )

    project_path = Path("Inputs") / "S2-1重点用能单位.xlsx"
    frozen = FrozenProjectFile(
        logical_ref=project_path,
        snapshot_ref=Path("Work/runs/reporting-1/frozen-project") / project_path,
        sha256="a" * 64,
        size=1,
        blob_ref=Path("Work/content/sha256/aa") / ("a" * 64),
        trusted_handle_ref=Path("Work/trusted-handles/handle.json"),
    )

    assert frozen.model_dump(mode="json") == {
        "logical_ref": "Inputs/S2-1重点用能单位.xlsx",
        "snapshot_ref": (
            "Work/runs/reporting-1/frozen-project/Inputs/"
            "S2-1重点用能单位.xlsx"
        ),
        "sha256": "a" * 64,
        "size": 1,
        "blob_ref": f"Work/content/sha256/aa/{'a' * 64}",
        "trusted_handle_ref": "Work/trusted-handles/handle.json",
    }
    assert isinstance(frozen.model_dump(mode="python")["logical_ref"], Path)

    restored = FrozenProjectFile.model_validate(
        {
            **frozen.model_dump(mode="json"),
            "logical_ref": r"Inputs\S2-1重点用能单位.xlsx",
            "snapshot_ref": (
                r"Work\runs\reporting-1\frozen-project\Inputs"
                r"\S2-1重点用能单位.xlsx"
            ),
        }
    )
    assert restored.model_dump(mode="json")["logical_ref"] == (
        "Inputs/S2-1重点用能单位.xlsx"
    )


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
    assert find_spec("manyselves.core.reporting") is None
