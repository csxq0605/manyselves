"""Characterization for the shared typed user-supplement projection."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.user_supplements import (
    user_supplement_constraints,
)


def test_user_supplement_projection_preserves_scope_stage_and_supersedes() -> None:
    supplements = [
        UserSupplement(
            id="US-old",
            content="旧约束应被后续补充替代。",
            scope="module",
            target_ids=["2.1"],
            stages=["module_authoring"],
        ),
        UserSupplement(
            id="US-new",
            content="新的模块约束必须保留。",
            scope="module",
            target_ids=["2.1"],
            stages=["module_authoring"],
            supersedes=["US-old"],
        ),
        {
            "id": "US-run",
            "content": "运行级约束在当前阶段生效。",
            "scope": "run",
            "stages": ["module_authoring"],
        },
        UserSupplement(
            id="US-review-only",
            content="仅复审阶段适用。",
            scope="module",
            target_ids=["2.1"],
            stages=["module_review"],
        ),
        UserSupplement(
            id="US-other-module",
            content="其他模块约束不应泄漏。",
            scope="module",
            target_ids=["2.2"],
            stages=["module_authoring"],
        ),
    ]

    constraints = user_supplement_constraints(
        supplements,
        stage="module_authoring",
        target_ids={"2.1", "2.1.1"},
    )

    assert constraints == [
        "用户补充 US-new（scope=module; targets=2.1）是当前 run 的显式输入：新的模块约束必须保留。",
        "用户补充 US-run（scope=run; targets=run）是当前 run 的显式输入：运行级约束在当前阶段生效。",
    ]


def test_user_supplement_projection_accepts_mapping_and_generator() -> None:
    supplements = (
        supplement
        for supplement in [
            {
                "id": "US-submodule",
                "content": "目标子模块需要明确验证边界。",
                "scope": "submodule",
                "target_ids": ["2.1.1"],
                "stages": ["module_review"],
            },
            {
                "id": "US-unmatched",
                "content": "不匹配目标的输入。",
                "scope": "submodule",
                "target_ids": ["2.1.2"],
                "stages": ["module_review"],
            },
        ]
    )

    assert user_supplement_constraints(
        supplements,
        stage="module_review",
        target_ids={"2.1.1"},
    ) == [
        "用户补充 US-submodule（scope=submodule; targets=2.1.1）是当前 run 的显式输入：目标子模块需要明确验证边界。"
    ]
