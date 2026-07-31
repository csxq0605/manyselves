"""Typed application-layer failures shared by runtime clients."""


class RuntimeStartupError(RuntimeError):
    """A startup failure that clients may handle by stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
