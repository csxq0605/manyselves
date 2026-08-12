from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from manyselves.core.reporting.module_collaboration import (
    InterfaceCrossClosure,
    InterfaceDisposition,
    InterfaceRequest,
    InterfaceResolution,
    InterfaceResolutionRegistry,
    ModuleDiscoverySubmission,
    ModuleInterfaceCoverage,
    ModuleInterfaceResponseSubmission,
    MODULE_IDS,
    apply_interface_cross_closure,
    build_interface_resolution_registry,
    interface_owner_finding_id,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.workflow import ReportWorkflowRunner


def _discoveries() -> list[ModuleDiscoverySubmission]:
    request = InterfaceRequest(
        request_id="IF-2.1-2.3-001",
        requester_module_id="2.1",
        target_module_id="2.3",
        question="保护配置是否覆盖当前运行边界？",
        needed_for="确定 2.1 风险机理与联合验收边界。",
        evidence_ids=["E-0001"],
    )
    values: list[ModuleDiscoverySubmission] = []
    for module_id in MODULE_IDS:
        requests = [request] if module_id == "2.1" else []
        values.append(
            ModuleDiscoverySubmission(
                module_id=module_id,
                discovery_summary=f"{module_id} discovery",
                evidence_ids=["E-0001"],
                interface_coverage=[
                    ModuleInterfaceCoverage(
                        target_module_id=target,
                        status="request" if target == "2.3" and module_id == "2.1" else "not_applicable",
                        rationale="typed coverage",
                    )
                    for target in MODULE_IDS
                    if target != module_id
                ],
                requests=requests,
            )
        )
    return values


def _registry() -> object:
    response = ModuleInterfaceResponseSubmission(
        module_id="2.3",
        dispositions=[
            InterfaceDisposition(
                request_id="IF-2.1-2.3-001",
                status="unresolved",
                checked_evidence_ids=["E-0001"],
                queries_performed=["search_project_evidence"],
                missing_fields=["异常切换试验记录"],
                reason="当前运行边界缺少异常切换记录。",
                boundary="作者必须保留未评估边界，不得推断异常覆盖。",
            )
        ],
    )
    return build_interface_resolution_registry(
        _discoveries(),
        [response],
        run_id="run-interface",
        known_evidence_ids={"E-0001"},
    )


def test_registry_marks_wave2_answered_terminal_and_unresolved_pending() -> None:
    discoveries = _discoveries()
    # Add an answered second request so the registry proves the two states can
    # coexist while Cross only receives the unresolved member.
    answer_request = InterfaceRequest(
        request_id="IF-2.4-2.3-002",
        requester_module_id="2.4",
        target_module_id="2.3",
        question="控制联锁是否覆盖切换场景？",
        needed_for="完成 2.4 联合验收。",
        evidence_ids=["E-0001"],
    )
    d24 = discoveries[3]
    discoveries[3] = d24.model_copy(
        update={
            "interface_coverage": [
                item.model_copy(
                    update={"status": "request"}
                    if item.target_module_id == "2.3"
                    else {}
                )
                for item in d24.interface_coverage
            ],
            "requests": [answer_request],
        }
    )
    response = ModuleInterfaceResponseSubmission(
        module_id="2.3",
        dispositions=[
            InterfaceDisposition(
                request_id="IF-2.1-2.3-001",
                status="unresolved",
                checked_evidence_ids=["E-0001"],
                queries_performed=["search_project_evidence"],
                missing_fields=["异常切换试验记录"],
                reason="当前运行边界缺少异常切换记录。",
                boundary="作者必须保留未评估边界。",
            ),
            InterfaceDisposition(
                request_id="IF-2.4-2.3-002",
                status="answered",
                answer="已覆盖当前切换场景。",
                conditions=["以当前整定版本为准"],
            ),
        ],
    )
    registry = build_interface_resolution_registry(
        discoveries,
        [response],
        run_id="run-interface",
        known_evidence_ids={"E-0001"},
    )
    assert registry.resolutions["IF-2.4-2.3-002"].status == "answered"
    assert registry.pending_request_ids == ("IF-2.1-2.3-001",)


def test_cross_closure_supports_three_outcomes_and_stable_reroute_id() -> None:
    registry = _registry()
    reroute = InterfaceCrossClosure(
        request_id="IF-2.1-2.3-001",
        outcome="reroute_to_owner",
        reason="请求方必须把透明边界写回正文。",
        owner_module_id="2.1",
        owner_submodule_id="2.1.1",
        owner_finding_id="XMR-IF-2.1-2.3-001",
    )
    assert interface_owner_finding_id(reroute.request_id) == reroute.owner_finding_id
    rerouted = apply_interface_cross_closure(registry, [reroute])
    assert rerouted.pending_request_ids == (reroute.request_id,)
    confirmed = InterfaceCrossClosure(
        request_id=reroute.request_id,
        outcome="confirmed_missing",
        reason="现有证据不足以回答该请求。",
        boundary="保留未评估边界。",
        residual_risk="异常切换风险仍未验证。",
    )
    closed = apply_interface_cross_closure(rerouted, [confirmed], review_round=1)
    assert closed.pending_request_ids == ()
    assert closed.resolutions[reroute.request_id].cross_closure.residual_risk == (
        "异常切换风险仍未验证。"
    )


def test_cross_closure_rejects_answered_or_inexact_sets() -> None:
    registry = _registry()
    with pytest.raises(ValueError, match="exact pending IF set"):
        apply_interface_cross_closure(
            registry,
            [
                InterfaceCrossClosure(
                    request_id="IF-2.2-2.3-001",
                    outcome="resolved_by_cross",
                    reason="不应接受额外请求。",
                )
            ],
        )
    with pytest.raises(ValidationError):
        InterfaceCrossClosure(
            request_id="IF-2.1-2.3-001",
            outcome="confirmed_missing",
            reason="缺失。",
            boundary="边界。",
        )
    request = _discoveries()[0].requests[0]
    answered = InterfaceDisposition(
        request_id=request.request_id,
        status="answered",
        answer="回答。",
        conditions=["当前版本"],
    )
    with pytest.raises(ValidationError, match="closes immediately"):
        InterfaceResolution(
            request=request,
            disposition=answered,
            closure_status="pending_cross",
        )


def test_resume_rejects_foreign_or_mutated_registry_hash(tmp_path) -> None:
    store = ReportingStore(tmp_path)
    run_id = "run-interface-resume"
    registry = InterfaceResolutionRegistry(run_id=run_id, resolutions={})
    ref = f"Work/runs/{run_id}/collaboration/interface-resolution-registry.json"
    store.write_json(ref, registry.model_dump(mode="json"))
    path = tmp_path / ref
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = SimpleNamespace(workspace=tmp_path, store=store)
    state = {"run_id": run_id, "request": SimpleNamespace(target_modules=[])}
    checkpoint = {
        "run_id": run_id,
        "interface_resolution_registry_ref": ref,
        "interface_resolution_registry_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    runner._restore_resume_state(state, checkpoint)
    assert state["interface_resolution_registry_ref"] == ref

    mutated = path.read_bytes() + b"\n"
    path.write_bytes(mutated)
    with pytest.raises(Exception, match="registry hash"):
        runner._restore_resume_state(
            {"run_id": run_id, "request": SimpleNamespace(target_modules=[])},
            checkpoint,
        )

    foreign_checkpoint = dict(checkpoint)
    foreign_checkpoint["interface_resolution_registry_ref"] = (
        "Work/runs/other-run/collaboration/interface-resolution-registry.json"
    )
    with pytest.raises(Exception, match="outside the current run"):
        runner._restore_resume_state(
            {"run_id": run_id, "request": SimpleNamespace(target_modules=[])},
            foreign_checkpoint,
        )
