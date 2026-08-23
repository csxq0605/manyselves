"""Lossless task execution profiles, provider routing, and scheduling hints.

This module deliberately does not implement admission control or rate limiting.
Profiles describe how an already-authorized typed task should execute.  The
router never truncates task inputs and never mutates a shared provider instance
to change models while other lanes may be using it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope

from ...config.schema import AgentDefaults
from ..providers.base import LLMProvider
from .config import AgentDefinition


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Effort = Literal["low", "medium", "high"]


class TaskExecutionProfile(_StrictModel):
    """Versioned execution policy for one typed reporting task.

    The two lossless fields are intentionally literals.  A profile cannot opt
    into prompt truncation or omission of declared input refs merely to reduce
    tokens.  Cost reductions must instead come from reuse, stable references,
    caching, delta inputs, and avoiding repeated work.
    """

    profile_id: str = Field(min_length=1)
    version: str = Field(default="1", min_length=1)
    task_kind: str = Field(min_length=1)
    provider_route: str = Field(default="inherit", min_length=1)
    model: str = Field(default="inherit", min_length=1)
    effort: Effort = "medium"
    working_memory_tokens: int = Field(ge=4_096)
    max_output_tokens: int = Field(ge=1_024)
    max_tool_rounds: int = Field(ge=1, le=200)
    max_tool_calls_per_round: int = Field(ge=1)
    max_tool_result_chars: int = Field(ge=512)
    priority: int = Field(default=50, ge=0, le=100)
    critical_path_weight: float = Field(default=1.0, gt=0)
    expected_duration_ms: int = Field(default=60_000, ge=1)
    context_policy: Literal["lossless_references"] = "lossless_references"
    preserve_all_declared_inputs: Literal[True] = True

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ExecutionProfileCatalog(_StrictModel):
    """Deployment-owned, versioned selectors for task execution profiles."""

    schema_version: Literal["1"] = "1"
    profiles: dict[str, TaskExecutionProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def selectors_are_explicit(self) -> "ExecutionProfileCatalog":
        for selector in self.profiles:
            if selector == "default":
                continue
            prefix, separator, value = selector.partition(":")
            if separator != ":" or prefix not in {"task", "task_kind", "agent"} or not value:
                raise ValueError(f"invalid execution profile selector: {selector}")
        return self

    @classmethod
    def load(cls, path: Path) -> "ExecutionProfileCatalog":
        """Load one strict catalog; a missing file means built-in profiles only."""

        path = Path(path)
        if not path.exists():
            return cls()
        if not path.is_file():
            raise ValueError(f"execution profile catalog is not a file: {path}")
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ResolvedTaskExecutionProfile(_StrictModel):
    """A profile bound to the concrete provider object used by a task."""

    profile: TaskExecutionProfile
    resolved_provider_route: str = Field(min_length=1)
    resolved_provider_class: str = Field(min_length=1)
    resolved_model: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def digest_matches_profile(self) -> "ResolvedTaskExecutionProfile":
        if self.profile_sha256 != self.profile.digest():
            raise ValueError("resolved execution profile digest mismatch")
        return self


class ProviderRouter:
    """Resolve task profiles to preconfigured providers without limiting calls.

    Route providers must already be configured with their model.  Mutating
    ``provider.model`` at dispatch time would race between parallel lanes, so a
    requested model mismatch is rejected instead of silently changing it.
    """

    _EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}
    _BUILTIN_OUTPUT_TOKEN_FLOORS = {
        # Final review emits nested finding/tool arguments.  An 8K cap can cut
        # the tool call before its arguments are serialized, turning a
        # correctable type error into an argument-less call.  This is only
        # headroom: providers still stop as soon as the typed result is done.
        "final_review": 32_768,
    }

    def __init__(
        self,
        inherited_provider: LLMProvider,
        *,
        routes: Mapping[str, LLMProvider] | None = None,
        profiles: Mapping[str, TaskExecutionProfile | Mapping[str, Any]] | None = None,
    ) -> None:
        self.inherited_provider = inherited_provider
        self.routes = dict(routes or {})
        if "inherit" in self.routes and self.routes["inherit"] is not inherited_provider:
            raise ValueError("inherit route is reserved for the inherited provider")
        self.routes["inherit"] = inherited_provider
        self.profiles = {
            selector: (
                profile
                if isinstance(profile, TaskExecutionProfile)
                else TaskExecutionProfile.model_validate(profile)
            )
            for selector, profile in (profiles or {}).items()
        }

    @staticmethod
    def _selector_order(
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        task_kind: str,
    ) -> tuple[str, ...]:
        return (
            f"task:{envelope.task_id}",
            f"task_kind:{task_kind}",
            f"agent:{definition.id}",
            "default",
        )

    def _configured_profile(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        task_kind: str,
    ) -> TaskExecutionProfile | None:
        for selector in self._selector_order(definition, envelope, task_kind):
            if selector in self.profiles:
                return self.profiles[selector]
        return None

    def scheduling_hints(
        self,
        *,
        task_id: str,
        task_kind: str,
        agent_id: str | None = None,
        default_expected_duration_ms: int = 60_000,
    ) -> tuple[int, float, int]:
        """Resolve scheduler-only fields without constructing or mutating a Provider."""

        selectors = [f"task:{task_id}", f"task_kind:{task_kind}"]
        if agent_id:
            selectors.append(f"agent:{agent_id}")
        selectors.append("default")
        configured = next(
            (self.profiles[selector] for selector in selectors if selector in self.profiles),
            None,
        )
        if configured is None:
            return 50, 1.0, max(1, int(default_expected_duration_ms))
        return (
            configured.priority,
            configured.critical_path_weight,
            configured.expected_duration_ms,
        )

    def resolve(
        self,
        definition: AgentDefinition,
        envelope: TaskEnvelope,
        *,
        task_kind: str,
        base_config: AgentDefaults,
    ) -> tuple[ResolvedTaskExecutionProfile, LLMProvider, AgentDefaults]:
        configured = self._configured_profile(definition, envelope, task_kind)
        requested_effort = configured.effort if configured else definition.effort
        effort = max(
            (definition.effort, requested_effort),
            key=self._EFFORT_ORDER.__getitem__,
        )
        profile = TaskExecutionProfile(
            profile_id=(
                configured.profile_id
                if configured
                else f"builtin:{definition.id}:{task_kind}"
            ),
            version=configured.version if configured else "1",
            task_kind=task_kind,
            provider_route=configured.provider_route if configured else "inherit",
            # Existing reporting definitions historically treated ``model`` as
            # descriptive while the runner inherited its concrete provider.
            # Preserve that compatibility unless an explicit execution profile
            # selects one preconfigured route/model pair.
            model=configured.model if configured else "inherit",
            effort=effort,
            # A configured profile may increase execution headroom, but cannot
            # reduce the current complete-context settings.
            working_memory_tokens=max(
                base_config.working_memory_tokens,
                configured.working_memory_tokens if configured else 0,
            ),
            max_output_tokens=max(
                base_config.max_tokens,
                configured.max_output_tokens if configured else 0,
                self._BUILTIN_OUTPUT_TOKEN_FLOORS.get(task_kind, 0),
            ),
            max_tool_rounds=max(
                base_config.max_tool_iterations,
                configured.max_tool_rounds if configured else 0,
            ),
            max_tool_calls_per_round=max(
                base_config.max_tool_calls_per_round,
                configured.max_tool_calls_per_round if configured else 0,
            ),
            max_tool_result_chars=max(
                base_config.max_tool_result_chars,
                configured.max_tool_result_chars if configured else 0,
            ),
            priority=configured.priority if configured else 50,
            critical_path_weight=(
                configured.critical_path_weight if configured else 1.0
            ),
            expected_duration_ms=(
                configured.expected_duration_ms if configured else 60_000
            ),
        )
        try:
            provider = self.routes[profile.provider_route]
        except KeyError as exc:
            raise ValueError(
                f"unknown provider route for execution profile: {profile.provider_route}"
            ) from exc
        provider_model = str(provider.model or "unknown")
        requested_model = profile.model
        if requested_model not in {"inherit", provider_model}:
            raise ValueError(
                "execution profile model does not match its preconfigured provider route: "
                f"requested={requested_model}, route_model={provider_model}"
            )
        resolved = ResolvedTaskExecutionProfile(
            profile=profile,
            resolved_provider_route=profile.provider_route,
            resolved_provider_class=provider.__class__.__name__,
            resolved_model=provider_model,
            profile_sha256=profile.digest(),
        )
        config = base_config.model_copy(
            update={
                "working_memory_tokens": profile.working_memory_tokens,
                "max_tokens": profile.max_output_tokens,
                "max_tool_iterations": profile.max_tool_rounds,
                "max_tool_calls_per_round": profile.max_tool_calls_per_round,
                "max_tool_result_chars": profile.max_tool_result_chars,
            }
        )
        return resolved, provider, config
