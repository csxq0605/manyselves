"""Simple business-state recovery API.

The implementation lives beside the historical parallel runtime contracts so
existing imports remain wire-compatible.  This module is a discoverable shim
for workers that should depend on stage/lane/aggregate state rather than CAS
or content-hash identity.
"""

from .parallel_runtime import (
    AggregateState,
    LaneAttempt,
    LaneAttemptRecord,
    LaneCompletion,
    LaneCompletionRecord,
    LaneRecoveryStore,
    LaneState,
    RecoveryPlan,
    RecoveryStateStore,
    RecoveryStore,
    StageState,
)

__all__ = [
    "AggregateState",
    "LaneAttempt",
    "LaneAttemptRecord",
    "LaneCompletion",
    "LaneCompletionRecord",
    "LaneRecoveryStore",
    "LaneState",
    "RecoveryPlan",
    "RecoveryStateStore",
    "RecoveryStore",
    "StageState",
]
