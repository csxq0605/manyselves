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
