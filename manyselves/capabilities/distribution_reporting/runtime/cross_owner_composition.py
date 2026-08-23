"""Neutral composition boundary for the file-defined Cross owner workflows.

Cross has a deliberately wide lifecycle: five owner inputs, the original
module-author revision, an owner-local Auditor regression, the original Cross
reviewer recheck, and the existing Main exception interactions.  The lifecycle
state machine is still supplied by its owning runtime.  This module only maps
that runtime's already-created ports to the names declared in the Cross YAML
workflows.  It therefore does not import the legacy Reporting runner or copy
any persistence/recovery implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class CrossOwnerRuntimePort(Protocol):
    """Structural port consumed by the generic Cross workflow composition.

    Implementations own the typed lifecycle and may use the existing
    capability persistence/Agent services.  The composition intentionally
    does not prescribe how those services are obtained.
    """

    agent_invokers: Mapping[str, Any]


_CROSS_TOOL_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("prepare-cross-owner-cohort", "prepare"),
    ("reduce-cross-owner-cohort", "reduce"),
    ("prepare-current-cross-owner-initial", "prepare_initial"),
    ("cross-owner-initial-requires-agent", "initial_requires_agent"),
    ("accept-current-cross-owner-initial", "accept_initial"),
    ("cross-owner-initial-has-findings", "initial_has_findings"),
    ("prepare-current-cross-owner-revision", "prepare_revision"),
    ("cross-owner-revision-requires-agent", "revision_requires_agent"),
    ("accept-current-cross-owner-revision", "accept_revision"),
    ("prepare-current-cross-owner-author-exception", "prepare_author_exception"),
    ("prepare-current-cross-owner-reviewer-exception", "prepare_reviewer_exception"),
    ("cross-owner-main-exception-requires-agent", "main_exception_requires_agent"),
    ("accept-current-cross-owner-main-exception", "accept_main_exception"),
    ("cross-owner-main-exception-requests-user", "main_exception_requests_user"),
    (
        "apply-current-cross-owner-main-exception-user-input",
        "apply_main_exception_user_input",
    ),
    (
        "cross-owner-author-exception-returns-to-author",
        "author_exception_returns_to_author",
    ),
    ("prepare-current-cross-owner-local-review", "prepare_local_review"),
    (
        "cross-owner-local-review-preflight-needs-revision",
        "local_review_preflight_needs_revision",
    ),
    (
        "prepare-current-cross-owner-local-preflight-revision",
        "prepare_local_preflight_revision",
    ),
    (
        "accept-current-cross-owner-local-preflight-revision",
        "accept_local_preflight_revision",
    ),
    ("cross-owner-local-review-requires-agent", "local_review_requires_agent"),
    ("accept-current-cross-owner-local-review", "accept_local_review"),
    ("cross-owner-local-review-needs-revision", "local_review_needs_revision"),
    (
        "prepare-current-cross-owner-local-module-revision",
        "prepare_local_module_revision",
    ),
    (
        "cross-owner-local-revision-requires-agent",
        "local_revision_requires_agent",
    ),
    (
        "accept-current-cross-owner-local-module-revision",
        "accept_local_module_revision",
    ),
    (
        "prepare-current-cross-owner-local-module-recheck",
        "prepare_local_module_recheck",
    ),
    (
        "cross-owner-local-recheck-requires-agent",
        "local_recheck_requires_agent",
    ),
    (
        "accept-current-cross-owner-local-module-recheck",
        "accept_local_module_recheck",
    ),
    ("prepare-current-cross-owner-recheck", "prepare_recheck"),
    ("cross-owner-recheck-requires-agent", "recheck_requires_agent"),
    ("accept-current-cross-owner-recheck", "accept_recheck"),
    ("advance-current-cross-owner-round", "advance_round"),
    ("cross-owner-round-needs-revision", "round_needs_revision"),
    ("complete-current-cross-owner-pipeline", "complete_owner_round"),
    (
        "complete-current-cross-owner-without-findings",
        "complete_owner_without_findings",
    ),
    (
        "cross-owner-reviewer-exception-requires-agent",
        "main_exception_requires_agent",
    ),
    (
        "cross-owner-reviewer-exception-requests-user",
        "main_exception_requests_user",
    ),
)


def build_cross_owner_tool_implementations(
    runtime: CrossOwnerRuntimePort | Any | None,
) -> dict[str, Any]:
    """Expose every declared Cross Tool implemented by ``runtime``.

    Missing attributes are left out intentionally.  The generic Host then
    persists its completed prefix and reports the exact unbound action; this
    composition does not silently turn an absent lifecycle phase into a
    successful no-op.
    """

    if runtime is None:
        return {}
    implementations: dict[str, Any] = {}
    for tool_id, attribute in _CROSS_TOOL_ATTRIBUTES:
        implementation = getattr(runtime, attribute, None)
        if callable(implementation):
            implementations[tool_id] = implementation
    return implementations


def cross_owner_agent_invokers(
    runtime: CrossOwnerRuntimePort | Any | None,
) -> Mapping[str, Any]:
    """Return the already-composed Cross Agent invokers without adaptation."""

    if runtime is None:
        return {}
    invokers = getattr(runtime, "agent_invokers", {})
    return invokers if isinstance(invokers, Mapping) else {}


__all__ = [
    "CrossOwnerRuntimePort",
    "build_cross_owner_tool_implementations",
    "cross_owner_agent_invokers",
]
