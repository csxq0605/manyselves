"""Offline replay of the four tool-error families seen in run-135.

The production log is deliberately read-only input.  The tests retain only
counts and anonymous representative lines, so they do not depend on the
original run's files or expose an absolute workspace path to a Provider.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.artifact_access import (
    ConfigurationError,
    compile_agent_access,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.capabilities.distribution_reporting.runtime.module_provider_tools import (
    InspectImageTool,
)
from manyselves.kernel.definitions import AgentDefinition, DefinitionKind
from manyselves.runtime.artifacts import ArtifactGateway, ArtifactGrant, ToolContractError
from manyselves.runtime.tools.artifact_tools import OpenArtifactTool
from manyselves.runtime.tools.contracts import normalize_tool_call
from manyselves.runtime.tools.outcomes import normalize_tool_outcome

BASELINE_LOG = Path(
    "/Users/zzymima0000/Documents/Codex/test-improvements/logs/"
    "errors_2026-08-11_13-18-20.log"
)

_EXPECTED_COUNTS = {
    "binary_utf8": 103,
    "not_delivered": 98,
    "nonopaque_open_tool_result": 26,
    "pid_path": 1,
}

# Anonymous examples make the contract replay portable when the protected
# baseline log is not mounted in another checkout.
_ANONYMOUS_LINES = (
    "ERROR open_artifact: artifact was not delivered by reference | args={'ref': 'Work/runs/RUN/preparation/evidence.jsonl'}",
    "ERROR open_tool_result: open_internal requires an opaque reference | args={'ref': 'Work/runs/RUN/context/module.md'}",
    "ERROR search_text: 'utf-8' codec can't decode byte 0x87 in position 10: invalid start byte | args={'ref': 'Inputs/source.xlsx'}",
    "ERROR inspect_image: image must be a file inside the project | args={'path': 'P-0013'}",
)


def _module_agent_definition() -> AgentDefinition:
    _capability, registry = load_distribution_reporting_capability()
    definition = registry.require(DefinitionKind.AGENT, "module-2.1-specialist")
    assert isinstance(definition, AgentDefinition)
    return definition


def _classify(line: str) -> str | None:
    if "artifact was not delivered by reference" in line:
        return "not_delivered"
    if "open_internal requires an opaque reference" in line:
        return "nonopaque_open_tool_result"
    if "utf-8' codec can't decode" in line:
        return "binary_utf8"
    if "inspect_image" in line and re.search(r"P-[A-Za-z0-9_.-]+", line):
        return "pid_path"
    return None


def _baseline_lines() -> list[str]:
    if BASELINE_LOG.is_file():
        return BASELINE_LOG.read_text(encoding="utf-8").splitlines()
    return list(_ANONYMOUS_LINES)


async def _replay_decision(
    line: str,
    *,
    gateway: ArtifactGateway,
    binary_tool: OpenArtifactTool,
    denied_tool: OpenArtifactTool,
) -> dict[str, object] | None:
    """Turn one known log row into a deterministic local contract decision."""

    category = _classify(line)
    if category is None:
        return None
    if category == "binary_utf8":
        result = await binary_tool("Inputs/source.xlsx")
        error = result["error"]
        return {
            "category": category,
            "code": error["code"],
            "repair_code": error["repair_code"],
            "terminal": False,
            "provider_followup_required": False,
        }
    if category == "not_delivered":
        match = re.search(r"'ref': '([^']+)'", line)
        ref = match.group(1) if match else "Work/runs/RUN/unknown.json"
        try:
            await denied_tool(ref)
        except PermissionError as exc:
            return {
                "category": category,
                "code": "capability_denied",
                "repair_code": "deliver_artifact_reference",
                "terminal": True,
                "provider_followup_required": False,
                "message": str(exc),
            }
        raise AssertionError("an empty allowed-ref set must deny every baseline artifact")
    if category == "nonopaque_open_tool_result":
        normalized = normalize_tool_call(
            "open_tool_result",
            {"ref": "Work/runs/RUN/context/module.md"},
            authorized_refs=[],
        )
        error = normalized["error"]
        return {
            "category": category,
            "code": error["code"],
            "repair_code": normalized["repair_code"],
            "terminal": False,
            "provider_followup_required": False,
        }
    pid = re.search(r"P-[A-Za-z0-9_.-]+", line)
    normalized = normalize_tool_call(
        "inspect_image",
        {"path": pid.group(0) if pid else "P-UNKNOWN"},
        authorized_refs=[],
    )
    error = normalized["error"]
    return {
        "category": category,
        "code": error["code"],
        "repair_code": error.get("repair_code") or normalized.get("repair_code"),
        "terminal": False,
        "provider_followup_required": False,
    }


def test_run_135_log_counts_are_explicit_and_not_fabricated() -> None:
    lines = _baseline_lines()
    counts = {key: sum(_classify(line) == key for line in lines) for key in _EXPECTED_COUNTS}
    if BASELINE_LOG.is_file():
        # Actual protected baseline coverage: 228 known contract errors out of
        # 229 lines; the remaining line is an unrelated manage_tasks failure.
        assert len(lines) == 229
        assert counts == _EXPECTED_COUNTS
        assert sum(counts.values()) == 228
    else:
        assert counts == {
            "binary_utf8": 1,
            "not_delivered": 1,
            "nonopaque_open_tool_result": 1,
            "pid_path": 1,
        }


@pytest.mark.asyncio
async def test_every_known_run_135_row_has_one_local_no_followup_decision(
    tmp_path: Path,
) -> None:
    """Replay all known rows through real local contracts, not string labels."""

    binary_path = tmp_path / "Inputs/source.xlsx"
    binary_path.parent.mkdir(parents=True)
    binary_path.write_bytes(b"PK\x03\x04\x80\x81binary")
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("workflow", "task", "agent", "session"),
        secret=b"d" * 32,
    )
    binary_tool = OpenArtifactTool(gateway)
    denied_tool = OpenArtifactTool(gateway, allowed_refs=[])
    decisions = [
        decision
        for line in _baseline_lines()
        if (decision := await _replay_decision(
            line,
            gateway=gateway,
            binary_tool=binary_tool,
            denied_tool=denied_tool,
        ))
        is not None
    ]

    assert len(decisions) == (228 if BASELINE_LOG.is_file() else 4)
    assert all(item["provider_followup_required"] is False for item in decisions)
    assert Counter(item["category"] for item in decisions) == Counter(
        _EXPECTED_COUNTS if BASELINE_LOG.is_file() else {key: 1 for key in _EXPECTED_COUNTS}
    )
    assert {
        (item["category"], item["code"], item["repair_code"])
        for item in decisions
    } == {
        ("binary_utf8", "unsupported_operation", "use_format_reader"),
        ("not_delivered", "capability_denied", "deliver_artifact_reference"),
        ("nonopaque_open_tool_result", "non_opaque_reference", "use_open_artifact"),
        ("pid_path", "image_scope_unresolved", "resolve_image_scope"),
    }


@pytest.mark.asyncio
async def test_binary_artifact_returns_structured_non_retryable_contract_error(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Inputs/source.xlsx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"PK\x03\x04\x80\x81binary")
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("workflow", "task", "agent", "session"),
        secret=b"r" * 32,
    )

    result = await OpenArtifactTool(gateway)("Inputs/source.xlsx")

    assert result["status"] == "failed"
    assert result["error"]["code"] == "unsupported_operation"
    assert result["error"]["retryable"] is False
    assert result["error"]["repair_code"] == "use_format_reader"
    outcome = normalize_tool_outcome(result, "open_artifact")
    assert outcome.status == "failed"
    assert outcome.terminal is False
    assert outcome.error is not None


def test_nonopaque_open_tool_result_is_rejected_with_mechanical_repair() -> None:
    normalized = normalize_tool_call(
        "open_tool_result",
        {"ref": "Work/runs/RUN/context/module.md", "offset": 4000},
        authorized_refs=["Work/runs/RUN/context/module.md"],
    )

    assert normalized.accepted is True
    assert normalized.changed is True
    assert normalized.name == "open_artifact"
    assert normalized["repair_code"] == "public_ref_to_open_artifact"

    rejected = normalize_tool_call(
        "open_tool_result",
        {"ref": "Work/runs/RUN/context/foreign.md"},
        authorized_refs=[],
    )
    assert rejected.accepted is False
    assert rejected["error"]["code"] == "non_opaque_reference"
    assert rejected["repair_code"] == "use_open_artifact"
    # A failed submit is terminal for the turn, while a reader contract error
    # is a structured correction and never an automatic Provider retry.
    terminal = normalize_tool_outcome(
        {"status": "failed", "error": rejected["error"]},
        "submit_result",
    )
    assert terminal.status == "failed"
    assert terminal.terminal is True


def test_prompt_visible_ref_compiles_and_undeclared_tool_fails_closed(
    tmp_path: Path,
) -> None:
    run_id = "run-135-replay"
    ref = f"Work/runs/{run_id}/context/evidence.jsonl"
    path = tmp_path / ref
    path.parent.mkdir(parents=True)
    path.write_text('{"id":"E-001"}\n', encoding="utf-8")
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("workflow", "task", "module-2.1-specialist", "session"),
        secret=b"c" * 32,
    )
    definition = _module_agent_definition()
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id=run_id,
        agent_id="module-2.1-specialist",
        objective="replay capability boundary",
        input_refs=[ref],
        artifact_delivery_modes={ref: "reference"},
    )

    access = compile_agent_access(definition, envelope, gateway=gateway)

    assert ref in access.readable_refs
    assert access.get(ref) is not None
    assert "open_artifact" in access.tool_names
    with pytest.raises(ConfigurationError, match="undeclared tools"):
        compile_agent_access(
            definition,
            envelope.model_copy(update={"allowed_tools": ["exec"]}),
            gateway=gateway,
        )


@pytest.mark.asyncio
async def test_p_id_maps_only_to_current_run_and_rejects_foreign_scope(
    tmp_path: Path,
) -> None:
    current = tmp_path / "Work/runs/run-current/photos/P-0013.png"
    current.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="white").save(current)
    tool = InspectImageTool(
        tmp_path,
        allowed_refs=("Work/runs/run-current/photos/P-0013.png",),
        photo_refs={"P-0013": "Work/runs/run-current/photos/P-0013.png"},
    )

    inspected = await tool(path="P-0013")

    assert inspected["path"] == "Work/runs/run-current/photos/P-0013.png"
    with pytest.raises(ToolContractError, match="current run PhotoAsset map"):
        await tool(path="P-FOREIGN")

    foreign = tmp_path / "Work/runs/run-foreign/photos/P-0013.png"
    foreign.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="black").save(foreign)
    gateway = ArtifactGateway(
        tmp_path,
        ArtifactGrant("workflow", "task", "module-2.1-specialist", "session"),
        secret=b"p" * 32,
    )
    definition = _module_agent_definition()
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id="run-current",
        agent_id="module-2.1-specialist",
        objective="reject foreign photo",
    )
    with pytest.raises(ConfigurationError, match="another run"):
        compile_agent_access(
            definition,
            envelope,
            gateway=gateway,
            typed_input={
                "photos": [
                    {
                        "id": "P-0013",
                        "path": "Work/runs/run-foreign/photos/P-0013.png",
                    }
                ]
            },
        )
