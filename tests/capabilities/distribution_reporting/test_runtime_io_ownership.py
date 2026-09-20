"""Characterization for the Capability-owned deterministic I/O runtime."""

import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec

import pytest

RUNTIME = "manyselves.capabilities.distribution_reporting.runtime"
MODULES = {
    "adapters": f"{RUNTIME}.intake.adapters",
    "manifest": f"{RUNTIME}.intake.manifest",
    "workbook": f"{RUNTIME}.intake.workbook",
    "wps_images": f"{RUNTIME}.intake.wps_images",
    "mappers": f"{RUNTIME}.mappers",
    "preparation": f"{RUNTIME}.preparation",
}


def test_runtime_io_modules_are_capability_owned_and_import_independently() -> None:
    module_names = ", ".join(
        f"{key!r}: {value!r}" for key, value in MODULES.items()
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"modules = {{{module_names}}}",
                    "loaded = {key: __import__(name, fromlist=['*']) for key, name in modules.items()}",
                    "print(json.dumps({'modules': {key: module.__name__ for key, module in loaded.items()}, 'core_reporting': sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting'))}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["modules"] == MODULES
    assert payload["core_reporting"] == []

    def has_spec(name: str) -> bool:
        try:
            return find_spec(name) is not None
        except ModuleNotFoundError:
            return False

    assert not has_spec("manyselves.core.reporting.intake.adapters")
    assert not has_spec("manyselves.core.reporting.mappers.s2_1")
    assert not has_spec("manyselves.core.reporting.preparation")

    intake = import_module(f"{RUNTIME}.intake")
    mappers = import_module(f"{RUNTIME}.mappers")
    assert not hasattr(intake, "IntakeAdapterRegistry")
    assert mappers.__all__ == ["map_s2_1", "map_s4_4", "map_s4_6"]
    assert not any(
        name in vars(mappers)
        for name in ("ManifestFile", "ProjectManifest", "MappingGap", "MappingResult")
    )


@pytest.mark.parametrize(
    ("module_name", "symbol"),
    [
        (MODULES["adapters"], "IntakeAdapterRegistry"),
        (MODULES["manifest"], "build_manifest"),
        (MODULES["workbook"], "WorkbookArtifact"),
        (MODULES["workbook"], "WorkbookSheetArtifact"),
        (MODULES["wps_images"], "extract_wps_images"),
        (MODULES["wps_images"], "canonicalize_photo_bindings"),
        (f'{MODULES["mappers"]}.s2_1', "map_s2_1"),
        (f'{MODULES["mappers"]}.s4_4', "map_s4_4"),
        (f'{MODULES["mappers"]}.s4_6', "map_s4_6"),
        (MODULES["preparation"], "prepare_manifest_file"),
    ],
)
def test_runtime_io_symbols_have_capability_owned_modules(
    module_name: str,
    symbol: str,
) -> None:
    module = __import__(module_name, fromlist=[symbol])
    assert getattr(module, symbol).__module__ == module_name
