"""Installed production capability bundles."""

from pathlib import Path

from manyselves.kernel.definitions import CapabilityCatalog

CAPABILITY_ROOT = Path(__file__).resolve().parent


def load_builtin_capability_catalog() -> CapabilityCatalog:
    """Discover the capability bundles shipped by the application."""

    return CapabilityCatalog.discover([CAPABILITY_ROOT])


__all__ = ["CAPABILITY_ROOT", "load_builtin_capability_catalog"]
