"""Shared typed projection of active, scoped user supplements."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)


def request_user_supplements(
    request: Any,
) -> Iterable[UserSupplement | Mapping[str, Any]]:
    """Project supplements from a typed request or its serialized mapping."""

    if isinstance(request, Mapping):
        return request.get("user_supplements") or ()
    return getattr(request, "user_supplements", ())


def user_supplement_constraints(
    supplements: Iterable[UserSupplement | Mapping[str, Any]],
    *,
    stage: str,
    target_ids: Iterable[str] | None = None,
) -> list[str]:
    """Render the existing active stage/target supplement projection.

    Mapping inputs are converted to the same typed ``UserSupplement`` model
    used by ``ReportRequest``.  Supersession, stage, scope, target matching,
    and the provider-visible sentence are intentionally unchanged from the
    prior Author, Review, and Reporting implementations.
    """

    typed_supplements = [
        supplement
        if isinstance(supplement, UserSupplement)
        else UserSupplement.model_validate(supplement)
        for supplement in supplements
    ]
    superseded = {
        superseded_id
        for supplement in typed_supplements
        for superseded_id in supplement.supersedes
    }
    selected_target_ids = set(target_ids or ())
    applicable = [
        supplement
        for supplement in typed_supplements
        if supplement.id not in superseded
        and stage in supplement.stages
        and (
            supplement.scope == "run"
            or bool(set(supplement.target_ids).intersection(selected_target_ids))
        )
    ]
    return [
        (
            f"用户补充 {item.id}（scope={item.scope}; "
            f"targets={','.join(item.target_ids) or 'run'}）是当前 run 的显式输入："
            f"{item.content}"
        )
        for item in applicable
    ]


__all__ = ["request_user_supplements", "user_supplement_constraints"]
