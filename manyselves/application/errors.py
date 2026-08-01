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


class RuntimeConsistencyFailedError(RuntimeError):
    """An irreversible runtime commit could not be reflected durably."""

    code = "RUNTIME_CONSISTENCY_FAILED"

    def __init__(self) -> None:
        super().__init__("Runtime consistency could not be guaranteed")


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


class MaintenanceQuiescedError(RuntimeError):
    """A mutation was rejected while maintenance owns the application."""

    code = "MAINTENANCE_QUIESCED"

    def __init__(self) -> None:
        super().__init__("Runtime mutations are disabled during maintenance")


class MaintenanceTokenMismatchError(RuntimeError):
    """Only the opaque token returned by quiesce may release it."""

    code = "MAINTENANCE_TOKEN_MISMATCH"

    def __init__(self) -> None:
        super().__init__("Maintenance token does not match the active quiesce")


class AgentNotFoundError(LookupError):
    """A command targeted an agent absent from the active runtime."""

    code = "AGENT_NOT_FOUND"

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"Agent was not found: {agent_id}")


class CheckpointNotFoundError(LookupError):
    """A rollback referenced a checkpoint absent from the active runtime."""

    code = "CHECKPOINT_NOT_FOUND"

    def __init__(self, checkpoint_id: str) -> None:
        super().__init__(f"Checkpoint was not found: {checkpoint_id}")


class RollbackPreflightUnsupportedError(RuntimeError):
    """The active backend cannot prove rollback safety before mutation."""

    code = "ROLLBACK_PREFLIGHT_UNSUPPORTED"

    def __init__(self) -> None:
        super().__init__("Rollback preflight is unsupported by the active runtime")
