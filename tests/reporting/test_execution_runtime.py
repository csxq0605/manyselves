from pathlib import Path

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.artifacts import ArtifactGateway, ArtifactGrant
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agentic_models import TaskEnvelope
from manyselves.core.reporting.capabilities import compile_agent_access, scoped_gateway
from manyselves.core.reporting.config import AgentDefinition
from manyselves.core.reporting.execution_runtime import (
    ExecutionProfileCatalog,
    ProviderRouter,
    TaskExecutionProfile,
)


class _Provider(LLMProvider):
    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise AssertionError("routing tests do not call the provider")


def _definition() -> AgentDefinition:
    return AgentDefinition(
        name="module-2.1-specialist",
        description="test",
        model="inherit",
        effort="high",
        maxTurns=8,
        maxTokens=32_768,
        instructions="<test />",
        source_path=Path("agent.md"),
    )


def _envelope() -> TaskEnvelope:
    return TaskEnvelope(
        task_id="module-2.1-author",
        run_id="run-profile",
        agent_id="module-2.1-specialist",
        objective="author complete module",
    )


def test_profile_routing_is_lossless_and_never_reduces_existing_headroom() -> None:
    inherited = _Provider("key", model="full-model")
    configured = TaskExecutionProfile(
        profile_id="author-fast-path",
        task_kind="module_authoring",
        provider_route="inherit",
        model="inherit",
        effort="low",
        working_memory_tokens=4_096,
        max_output_tokens=1_024,
        max_tool_rounds=1,
        max_tool_calls_per_round=1,
        max_tool_result_chars=512,
        expected_duration_ms=30_000,
    )
    router = ProviderRouter(
        inherited,
        profiles={"task_kind:module_authoring": configured},
    )
    base = AgentDefaults(
        working_memory_tokens=64_000,
        max_tokens=32_768,
        max_tool_iterations=20,
        max_tool_calls_per_round=8,
        max_tool_result_chars=32_000,
    )

    resolved, provider, config = router.resolve(
        _definition(),
        _envelope(),
        task_kind="module_authoring",
        base_config=base,
    )

    assert provider is inherited
    assert resolved.profile.context_policy == "lossless_references"
    assert resolved.profile.preserve_all_declared_inputs is True
    assert resolved.profile.effort == "high"
    assert config.working_memory_tokens == 64_000
    assert config.max_tokens == 32_768
    assert config.max_tool_iterations == 20
    assert config.max_tool_calls_per_round == 8
    assert config.max_tool_result_chars == 32_000
    assert len(resolved.profile_sha256) == 64


def test_router_selects_preconfigured_provider_without_mutating_models() -> None:
    inherited = _Provider("key-a", model="full-model")
    alternate = _Provider("key-b", model="audit-model")
    profile = TaskExecutionProfile(
        profile_id="audit-route",
        task_kind="module_review",
        provider_route="audit",
        model="audit-model",
        effort="high",
        working_memory_tokens=64_000,
        max_output_tokens=32_768,
        max_tool_rounds=20,
        max_tool_calls_per_round=8,
        max_tool_result_chars=32_000,
    )
    router = ProviderRouter(
        inherited,
        routes={"audit": alternate},
        profiles={"task_kind:module_review": profile},
    )

    resolved, provider, _config = router.resolve(
        _definition(),
        _envelope(),
        task_kind="module_review",
        base_config=AgentDefaults(),
    )

    assert provider is alternate
    assert resolved.resolved_provider_route == "audit"
    assert resolved.resolved_model == "audit-model"
    assert inherited.model == "full-model"
    assert alternate.model == "audit-model"


def test_scheduler_uses_task_profile_priority_and_critical_path_hints() -> None:
    provider = _Provider("key", model="full-model")
    profile = TaskExecutionProfile(
        profile_id="module-lane-priority",
        task_kind="module_lane",
        effort="high",
        working_memory_tokens=64_000,
        max_output_tokens=32_768,
        max_tool_rounds=20,
        max_tool_calls_per_round=8,
        max_tool_result_chars=32_000,
        priority=91,
        critical_path_weight=1.75,
        expected_duration_ms=12_345,
    )
    router = ProviderRouter(
        provider,
        profiles={"task_kind:module_lane": profile},
    )

    assert router.scheduling_hints(
        task_id="module-lane:2.1",
        task_kind="module_lane",
    ) == (91, 1.75, 12_345)


def test_router_rejects_model_mismatch_instead_of_mutating_shared_provider() -> None:
    inherited = _Provider("key", model="full-model")
    profile = TaskExecutionProfile(
        profile_id="bad-route",
        task_kind="module_authoring",
        model="another-model",
        effort="high",
        working_memory_tokens=64_000,
        max_output_tokens=32_768,
        max_tool_rounds=20,
        max_tool_calls_per_round=8,
        max_tool_result_chars=32_000,
    )
    router = ProviderRouter(
        inherited,
        profiles={"task_kind:module_authoring": profile},
    )

    with pytest.raises(ValueError, match="does not match"):
        router.resolve(
            _definition(),
            _envelope(),
            task_kind="module_authoring",
            base_config=AgentDefaults(),
        )
    assert inherited.model == "full-model"


def test_lossless_continuation_tool_is_always_available(tmp_path) -> None:
    envelope = _envelope().model_copy(update={"allowed_tools": ["submit_result"]})
    definition = _definition().model_copy(update={"tools": ["submit_result"]})
    root = ArtifactGateway(
        tmp_path,
        ArtifactGrant("root", "root", "root", "root"),
    )
    gateway = scoped_gateway(
        root,
        workflow_id="workflow",
        envelope=envelope,
        agent_id=definition.id,
        session_id="session",
    )

    access = compile_agent_access(
        definition,
        envelope,
        [],
        gateway=gateway,
    )

    assert access.tool_names == ("submit_result", "open_tool_result")


def test_deployment_catalog_loads_strict_task_selectors(tmp_path: Path) -> None:
    path = tmp_path / "execution-profiles.json"
    path.write_text(
        TaskExecutionProfile(
            profile_id="review-route",
            task_kind="module_review",
            model="inherit",
            effort="high",
            working_memory_tokens=64_000,
            max_output_tokens=32_768,
            max_tool_rounds=20,
            max_tool_calls_per_round=8,
            max_tool_result_chars=32_000,
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    # A catalog is an explicit selector map, not a bare profile.
    bare = path.read_text(encoding="utf-8")
    path.write_text(
        '{"schema_version":"1","profiles":{"task_kind:module_review":'
        + bare
        + "}}",
        encoding="utf-8",
    )

    catalog = ExecutionProfileCatalog.load(path)

    assert catalog.profiles["task_kind:module_review"].profile_id == "review-route"
    assert len(catalog.digest()) == 64


def test_deployment_catalog_rejects_ambiguous_selector(tmp_path: Path) -> None:
    path = tmp_path / "execution-profiles.json"
    profile = TaskExecutionProfile(
        profile_id="bad-selector",
        task_kind="module_review",
        effort="high",
        working_memory_tokens=64_000,
        max_output_tokens=32_768,
        max_tool_rounds=20,
        max_tool_calls_per_round=8,
        max_tool_result_chars=32_000,
    )
    path.write_text(
        '{"profiles":{"module_review":' + profile.model_dump_json() + "}}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid execution profile selector"):
        ExecutionProfileCatalog.load(path)
