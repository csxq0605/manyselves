"""Application-layer adapters for Manyselves frontends."""

__all__ = ["ControlLeaseService", "RuntimeFacade", "RuntimeHost"]


def __getattr__(name: str):
    """Preserve convenience exports without loading the legacy host eagerly."""

    if name == "ControlLeaseService":
        from .control import ControlLeaseService

        return ControlLeaseService
    if name == "RuntimeFacade":
        from .runtime_facade import RuntimeFacade

        return RuntimeFacade
    if name == "RuntimeHost":
        from .runtime_host import RuntimeHost

        return RuntimeHost
    raise AttributeError(name)
