import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.agentic_models import AgentResult, TaskEnvelope
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.module_collaboration import (
    MODULE_IDS,
    InterfaceDisposition,
    InterfaceRequest,
    ModuleDiscoverySubmission,
    ModuleInterfaceCoverage,
    ModuleInterfaceResponseSubmission,
    build_collaboration_bundles,
    build_interface_inboxes,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.submission_contracts import (
    submission_model,
    submission_schema,
    undescribed_property_paths,
)
from manyselves.core.tools.reporting_collaboration_tools import SubmitResultTool
from manyselves.config.schema import AgentDefaults


def _discovery(
    module_id: str,
    *,
    requests: list[InterfaceRequest] | None = None,
    offer_targets: set[str] | None = None,
) -> ModuleDiscoverySubmission:
    requests = requests or []
    offer_targets = offer_targets or set()
    request_targets = {request.target_module_id for request in requests}
    return ModuleDiscoverySubmission(
        module_id=module_id,
        discovery_summary=f"{module_id} 已完成证据与接口发现。",
        evidence_ids=["E-0001"],
        interface_coverage=[
            ModuleInterfaceCoverage(
                target_module_id=target,
                status=(
                    "request"
                    if target in request_targets
                    else "offer"
                    if target in offer_targets
                    else "not_applicable"
                ),
                rationale=(
                    "需要该模块回答一个明确接口问题。"
                    if target in request_targets
                    else "可向该模块提供已确认的接口边界。"
                    if target in offer_targets
                    else "当前证据不形成对该模块的接口依赖。"
                ),
            )
            for target in MODULE_IDS
            if target != module_id
        ],
        requests=requests,
    )


def _five_discoveries() -> list[ModuleDiscoverySubmission]:
    request_21_23 = InterfaceRequest(
        request_id="IF-2.1-2.3-001",
        requester_module_id="2.1",
        target_module_id="2.3",
        question="保护配置是否覆盖当前运行边界？",
        needed_for="确定 2.1 的风险机理与联合验收边界。",
        evidence_ids=["E-0001"],
        blocking=True,
    )
    request_24_23 = InterfaceRequest(
        request_id="IF-2.4-2.3-001",
        requester_module_id="2.4",
        target_module_id="2.3",
        question="控制联锁是否覆盖异常切换场景？",
        needed_for="确定 2.4 的行动依赖。",
        evidence_ids=["E-0001"],
    )
    return [
        _discovery("2.1", requests=[request_21_23]),
        _discovery("2.2", offer_targets={"2.5"}),
        _discovery("2.3"),
        _discovery("2.4", requests=[request_24_23]),
        _discovery("2.5"),
    ]


def test_discovery_requires_four_unique_peer_targets_and_unique_request_ids() -> None:
    valid = _discovery("2.1")
    duplicated_coverage = [
        *valid.interface_coverage[:3],
        valid.interface_coverage[0],
    ]
    with pytest.raises(ValidationError, match="target_module_ids must be unique"):
        ModuleDiscoverySubmission(
            module_id="2.1",
            discovery_summary="重复覆盖不得通过。",
            interface_coverage=duplicated_coverage,
        )

    request = InterfaceRequest(
        request_id="IF-2.1-2.3-001",
        requester_module_id="2.1",
        target_module_id="2.3",
        question="需要确认什么？",
        needed_for="完成最终判断。",
    )
    with pytest.raises(ValidationError, match="request_ids must be unique"):
        _discovery("2.1", requests=[request, request])


def test_discovery_caps_requests_to_one_per_peer_and_four_total() -> None:
    duplicate_peer_requests = [
        InterfaceRequest(
            request_id=f"IF-2.1-2.3-{index:03d}",
            requester_module_id="2.1",
            target_module_id="2.3",
            question=f"需要确认接口问题 {index}？",
            needed_for="完成最终判断。",
        )
        for index in (1, 2)
    ]
    with pytest.raises(
        ValidationError,
        match="at most one request per peer target",
    ):
        _discovery("2.1", requests=duplicate_peer_requests)

    five_requests = [
        InterfaceRequest(
            request_id=f"IF-2.1-{target}-{index:03d}",
            requester_module_id="2.1",
            target_module_id=target,
            question=f"需要 {target} 确认接口问题？",
            needed_for="完成最终判断。",
        )
        for index, target in enumerate(
            ("2.2", "2.3", "2.4", "2.5", "2.2"),
            start=1,
        )
    ]
    with pytest.raises(ValidationError) as exc_info:
        _discovery("2.1", requests=five_requests)
    assert any(
        error["type"] == "too_long"
        and error["loc"] == ("requests",)
        for error in exc_info.value.errors()
    )


def test_collaboration_text_and_list_budgets_are_schema_enforced() -> None:
    discovery_payload = _discovery("2.1").model_dump(mode="json")
    discovery_payload["discovery_summary"] = "长" * 3001
    with pytest.raises(ValidationError) as exc_info:
        ModuleDiscoverySubmission.model_validate(discovery_payload)
    assert any(
        error["type"] == "string_too_long"
        and error["loc"] == ("discovery_summary",)
        for error in exc_info.value.errors()
    )

    with pytest.raises(ValidationError) as exc_info:
        InterfaceRequest(
            request_id="IF-2.1-2.3-001",
            requester_module_id="2.1",
            target_module_id="2.3",
            question="问" * 801,
            needed_for="完成最终判断。",
        )
    assert any(
        error["type"] == "string_too_long"
        and error["loc"] == ("question",)
        for error in exc_info.value.errors()
    )

    with pytest.raises(ValidationError) as exc_info:
        InterfaceDisposition(
            request_id="IF-2.1-2.3-001",
            status="answered",
            answer="已回答。",
            conditions=[f"条件 {index}" for index in range(7)],
        )
    assert any(
        error["type"] == "too_long"
        and error["loc"] == ("conditions",)
        for error in exc_info.value.errors()
    )


def test_answered_disposition_requires_nonempty_applicability_conditions() -> None:
    with pytest.raises(
        ValidationError,
        match="requires at least one condition",
    ):
        InterfaceDisposition(
            request_id="IF-2.1-2.3-001",
            status="answered",
            answer="该接口问题不依赖 E 证据，也仍需声明适用条件。",
            evidence_ids=[],
        )

    valid_without_evidence = InterfaceDisposition(
        request_id="IF-2.1-2.3-001",
        status="answered",
        answer="该接口问题不依赖 E 证据。",
        evidence_ids=[],
        conditions=["仅适用于当前明确的问题边界"],
    )
    assert valid_without_evidence.evidence_ids == []

    with pytest.raises(
        ValidationError,
        match="conditions cannot contain blank items",
    ):
        InterfaceDisposition(
            request_id="IF-2.1-2.3-001",
            status="answered",
            answer="已回答。",
            conditions=["   "],
        )


def test_barrier_one_requires_five_modules_and_builds_only_nonempty_inboxes() -> None:
    discoveries = _five_discoveries()

    inboxes = build_interface_inboxes(
        discoveries,
        known_evidence_ids={"E-0001"},
    )

    assert list(inboxes) == ["2.3"]
    assert [item.request_id for item in inboxes["2.3"]] == [
        "IF-2.1-2.3-001",
        "IF-2.4-2.3-001",
    ]
    with pytest.raises(ValueError, match="exactly five"):
        build_interface_inboxes(discoveries[:-1])
    with pytest.raises(ValueError, match="stale or unknown"):
        build_interface_inboxes(discoveries, known_evidence_ids=set())


def test_barrier_two_requires_only_inbox_responders_and_every_request() -> None:
    discoveries = _five_discoveries()
    response = ModuleInterfaceResponseSubmission(
        module_id="2.3",
        dispositions=[
            InterfaceDisposition(
                request_id="IF-2.1-2.3-001",
                status="answered",
                answer="正常边界已覆盖，异常场景需联合验证。",
                evidence_ids=["E-0002"],
                conditions=["以当前整定版本为准"],
            ),
            InterfaceDisposition(
                request_id="IF-2.4-2.3-001",
                status="unresolved",
                unresolved_reason="缺少异常切换联动试验记录。",
                boundary="Wave 3 必须标记为证据缺口并升级联合试验。",
            ),
        ],
    )

    bundles = build_collaboration_bundles(
        discoveries,
        [response],
        known_evidence_ids={"E-0001", "E-0002"},
    )

    assert list(bundles) == list(MODULE_IDS)
    assert bundles["2.1"].discovery_summary.startswith("2.1 ")
    assert bundles["2.1"].discovery_evidence_ids == ["E-0001"]
    assert [
        item.source_module_id
        for item in bundles["2.5"].incoming_peer_signals
    ] == ["2.2"]
    assert [
        item.request.request_id for item in bundles["2.1"].requested_interfaces
    ] == ["IF-2.1-2.3-001"]
    assert [
        item.request.request_id for item in bundles["2.3"].responded_interfaces
    ] == ["IF-2.1-2.3-001", "IF-2.4-2.3-001"]
    assert (
        bundles["2.4"].requested_interfaces[0].disposition.status
        == "unresolved"
    )

    incomplete = response.model_copy(
        update={"dispositions": response.dispositions[:1]}
    )
    with pytest.raises(ValueError, match="only and all incoming requests"):
        build_collaboration_bundles(discoveries, [incomplete])
    unexpected = ModuleInterfaceResponseSubmission(
        module_id="2.2",
        dispositions=[
            InterfaceDisposition(
                request_id="IF-2.1-2.3-001",
                status="answered",
                answer="不应由 2.2 回答。",
                conditions=["仅用于验证错误 responder"],
            )
        ],
    )
    with pytest.raises(ValueError, match="exactly the modules"):
        build_collaboration_bundles(discoveries, [response, unexpected])


def test_new_submission_kinds_have_enriched_schemas_and_runner_identity_const(
    tmp_path: Path,
) -> None:
    for kind in (
        "module_discovery_submission",
        "module_interface_response_submission",
    ):
        assert submission_model(kind) in {
            ModuleDiscoverySubmission,
            ModuleInterfaceResponseSubmission,
        }
        schema = submission_schema(kind)
        assert schema["properties"]["kind"]["const"] == kind
        assert undescribed_property_paths(schema) == []
        if kind == "module_discovery_submission":
            assert schema["properties"]["requests"]["maxItems"] == 4
            blocking_description = schema["$defs"]["InterfaceRequest"][
                "properties"
            ]["blocking"]["description"]
            assert "does not automatically pause or block" in blocking_description

    runner = ReportingAgentRunner(
        tmp_path,
        MessageBus(),
        object(),
        AgentDefaults(),
    )
    envelope = TaskEnvelope(
        task_id="module-2.4-discovery",
        run_id="run-collaboration",
        agent_id="module-2.4-specialist",
        objective="完成 Wave 1 discovery",
        allowed_outputs=["module_discovery_submission"],
    )
    registry = runner._tools(
        load_packaged_agents()["module-2.4-specialist"],
        envelope,
        "session-collaboration",
        "workflow-collaboration",
    )
    payload_schema = registry._schema_cache["submit_result"]["properties"]["payload"]
    assert payload_schema["properties"]["module_id"]["const"] == "2.4"
    assert "examples" not in payload_schema


@pytest.mark.asyncio
async def test_submit_result_persists_new_kind_through_unified_agent_result(
    tmp_path: Path,
) -> None:
    tool = SubmitResultTool(
        "module-2.1-specialist",
        "session-1",
        "run-1",
        "module-2.1-discovery",
        ReportingStore(tmp_path),
        MessageBus(),
        "workflow-1",
        allowed_outputs=["module_discovery_submission"],
    )

    result = await tool(_discovery("2.1").model_dump(mode="json"))

    assert result["status"] == "completed"
    persisted = json.loads(
        (tmp_path / result["result_path"]).read_text(encoding="utf-8")
    )
    parsed = AgentResult.model_validate(persisted)
    assert isinstance(parsed.payload, ModuleDiscoverySubmission)
    assert parsed.payload.module_id == "2.1"
