"""Typed application-layer failures shared by runtime clients."""


class RuntimeStartupError(RuntimeError):
    """A startup failure that clients may handle by stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeNotReadyError(RuntimeError):
    """A mutation was requested before the shared runtime was ready."""

    code = "RUNTIME_NOT_READY"

    def __init__(self) -> None:
        super().__init__("Runtime is not ready")


class CommandIdConflictError(RuntimeError):
    """A completed command ID was reused for a different mutation."""

    code = "COMMAND_ID_CONFLICT"

    def __init__(self) -> None:
        super().__init__("Command ID was already used with different command data")


class RuntimeBusyError(RuntimeError):
    """A workspace mutation requires every runtime agent to be idle."""

    code = "RUNTIME_BUSY"

    def __init__(self) -> None:
        super().__init__("Runtime agents must be idle before activating a project")
