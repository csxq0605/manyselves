"""Runtime binding for the parameter-adjustment Capability.

The Capability owns only its definitions and one deterministic Tool; the Run
lifecycle and the Agent invocation are the generic Runtime's.
"""

from pathlib import Path
from typing import Any

from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding

from .. import CAPABILITY_FILE


def _normalize_parameter(arguments: dict[str, Any]) -> int:
    return arguments["value"]


TOOL_IMPLEMENTATIONS = {"normalize-parameter": _normalize_parameter}


def build_runtime_binding(
    *,
    workspace: Path,
    services: Any,
    **options: Any,
) -> GenericCapabilityRuntimeBinding:
    return GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=workspace,
        services=services,
        tool_implementations=TOOL_IMPLEMENTATIONS,
        **options,
    )


__all__ = ["TOOL_IMPLEMENTATIONS", "build_runtime_binding"]
