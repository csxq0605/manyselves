"""Typed contracts and deterministic barriers for three-wave module collaboration."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ModuleId = Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
MODULE_IDS: tuple[ModuleId, ...] = ("2.1", "2.2", "2.3", "2.4", "2.5")
_INTERFACE_ID_PATTERN = r"^IF-2\.[1-5]-2\.[1-5]-[0-9]{3,}$"
_MAX_EVIDENCE_IDS = 24
_MAX_INTERFACE_REQUESTS = 4
_MAX_CONDITIONS = 6

EvidenceId = Annotated[str, Field(min_length=3, max_length=128)]
ConditionText = Annotated[str, Field(min_length=1, max_length=300)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModuleInterfaceCoverage(_StrictModel):
    """One Wave 1 assessment of another fixed responsibility module."""

    target_module_id: ModuleId = Field(
        description="The other fixed module whose interface relevance was assessed."
    )
    status: Literal["not_applicable", "offer", "request", "conflict"] = Field(
        description=(
            "Explicit coverage result: no interface, an unsolicited offer, a question, "
            "or a conflict that requires a response."
        )
    )
    rationale: str = Field(
        min_length=1,
        max_length=600,
        description="Evidence-bounded reason for this exact peer coverage result.",
    )


class InterfaceRequest(_StrictModel):
    """One stable, directed question emitted by a Wave 1 specialist."""

    request_id: str = Field(
        pattern=_INTERFACE_ID_PATTERN,
        max_length=32,
        description="Workflow-stable request id in IF-<requester>-<target>-NNN form.",
    )
    requester_module_id: ModuleId = Field(
        description="Module that needs the interface answer before final authoring."
    )
    target_module_id: ModuleId = Field(
        description="Different module responsible for answering this request."
    )
    question: str = Field(
        min_length=1,
        max_length=800,
        description="Concrete interface question that the target specialist can answer.",
    )
    needed_for: str = Field(
        min_length=1,
        max_length=600,
        description="Why the answer can affect the requester's final module analysis.",
    )
    evidence_ids: list[EvidenceId] = Field(
        default_factory=list,
        max_length=_MAX_EVIDENCE_IDS,
        description="Current-run E-* evidence ids already bounding the question.",
    )
    blocking: bool = Field(
        default=False,
        description=(
            "If unresolved, the boundary must be explicitly escalated in the "
            "collaboration bundle and final authoring; this flag does not "
            "automatically pause or block the current workflow stage."
        ),
    )

    @model_validator(mode="after")
    def directed_identity_is_consistent(self) -> "InterfaceRequest":
        if self.requester_module_id == self.target_module_id:
            raise ValueError("interface request target must differ from requester")
        expected_prefix = (
            f"IF-{self.requester_module_id}-{self.target_module_id}-"
        )
        if not self.request_id.startswith(expected_prefix):
            raise ValueError("request_id must encode its requester and target module ids")
        if (
            any(not value.startswith("E-") for value in self.evidence_ids)
            or len(self.evidence_ids) != len(set(self.evidence_ids))
        ):
            raise ValueError("request evidence_ids must be unique E-* ids")
        return self


class ModuleDiscoverySubmission(_StrictModel):
    """Wave 1 research summary plus exhaustive peer-interface coverage."""

    kind: Literal["module_discovery_submission"] = "module_discovery_submission"
    module_id: ModuleId = Field(description="Specialist module that owns this discovery.")
    discovery_summary: str = Field(
        min_length=1,
        max_length=3000,
        description="Concise evidence-bounded findings available before final authoring.",
    )
    evidence_ids: list[EvidenceId] = Field(
        default_factory=list,
        max_length=_MAX_EVIDENCE_IDS,
        description="Current-run E-* evidence ids used by this discovery.",
    )
    interface_coverage: list[ModuleInterfaceCoverage] = Field(
        min_length=4,
        max_length=4,
        description="Exactly one explicit coverage entry for every other module.",
    )
    requests: list[InterfaceRequest] = Field(
        default_factory=list,
        max_length=_MAX_INTERFACE_REQUESTS,
        description="Directed requests required by request or conflict coverage entries.",
    )

    @model_validator(mode="after")
    def covers_every_peer_exactly_once(self) -> "ModuleDiscoverySubmission":
        expected_targets = set(MODULE_IDS) - {self.module_id}
        coverage_targets = [item.target_module_id for item in self.interface_coverage]
        if len(coverage_targets) != len(set(coverage_targets)):
            raise ValueError("interface coverage target_module_ids must be unique")
        if set(coverage_targets) != expected_targets:
            raise ValueError("discovery must cover every other module exactly once")
        if (
            any(not value.startswith("E-") for value in self.evidence_ids)
            or len(self.evidence_ids) != len(set(self.evidence_ids))
        ):
            raise ValueError("discovery evidence_ids must be unique E-* ids")
        request_ids = [request.request_id for request in self.requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("request_ids must be unique within one discovery")
        if any(request.requester_module_id != self.module_id for request in self.requests):
            raise ValueError("every discovery request must be owned by its module_id")
        request_targets = [
            request.target_module_id for request in self.requests
        ]
        if len(request_targets) != len(set(request_targets)):
            raise ValueError(
                "discovery may emit at most one request per peer target"
            )
        coverage_by_target = {
            item.target_module_id: item.status for item in self.interface_coverage
        }
        requests_by_target = {
            target: [
                request
                for request in self.requests
                if request.target_module_id == target
            ]
            for target in expected_targets
        }
        for target, status in coverage_by_target.items():
            has_requests = bool(requests_by_target[target])
            if status in {"request", "conflict"} and not has_requests:
                raise ValueError(
                    f"{status} coverage for {target} requires at least one request"
                )
            if status in {"not_applicable", "offer"} and has_requests:
                raise ValueError(
                    f"{status} coverage for {target} cannot carry a response request"
                )
        return self


class InterfaceDisposition(_StrictModel):
    """One Wave 2 answer or explicit unresolved boundary for a known request."""

    request_id: str = Field(
        pattern=_INTERFACE_ID_PATTERN,
        max_length=32,
        description="Exact incoming request id being dispositioned.",
    )
    status: Literal["answered", "unresolved"] = Field(
        description="Whether the target answered or explicitly bounded the request."
    )
    answer: str | None = Field(
        default=None,
        max_length=2000,
        description="Concrete response when status is answered.",
    )
    evidence_ids: list[EvidenceId] = Field(
        default_factory=list,
        max_length=_MAX_EVIDENCE_IDS,
        description="Current-run E-* evidence ids supporting the response.",
    )
    conditions: list[ConditionText] = Field(
        default_factory=list,
        max_length=_MAX_CONDITIONS,
        description="Conditions under which the response remains applicable.",
    )
    residual_uncertainty: str | None = Field(
        default=None,
        max_length=800,
        description="Remaining uncertainty that does not invalidate an answered response.",
    )
    unresolved_reason: str | None = Field(
        default=None,
        max_length=800,
        description="Why the target cannot answer when status is unresolved.",
    )
    boundary: str | None = Field(
        default=None,
        max_length=800,
        description="Explicit authoring or escalation boundary for an unresolved request.",
    )

    @model_validator(mode="after")
    def answer_or_boundary_is_explicit(self) -> "InterfaceDisposition":
        if (
            any(not value.startswith("E-") for value in self.evidence_ids)
            or len(self.evidence_ids) != len(set(self.evidence_ids))
        ):
            raise ValueError("response evidence_ids must be unique E-* ids")
        if self.status == "answered":
            if not self.answer or not self.answer.strip():
                raise ValueError("answered interface disposition requires answer")
            if not self.conditions:
                raise ValueError(
                    "answered interface disposition requires at least one condition"
                )
            if self.unresolved_reason is not None or self.boundary is not None:
                raise ValueError(
                    "answered interface disposition cannot declare an unresolved boundary"
                )
        else:
            if not self.unresolved_reason or not self.unresolved_reason.strip():
                raise ValueError(
                    "unresolved interface disposition requires unresolved_reason"
                )
            if not self.boundary or not self.boundary.strip():
                raise ValueError("unresolved interface disposition requires boundary")
            if self.answer is not None:
                raise ValueError("unresolved interface disposition cannot declare answer")
        if any(not condition.strip() for condition in self.conditions):
            raise ValueError("interface disposition conditions cannot contain blank items")
        return self


class ModuleInterfaceResponseSubmission(_StrictModel):
    """Wave 2 batch returned by one module with actual incoming requests."""

    kind: Literal[
        "module_interface_response_submission"
    ] = "module_interface_response_submission"
    module_id: ModuleId = Field(description="Target module answering its Wave 2 inbox.")
    dispositions: list[InterfaceDisposition] = Field(
        min_length=1,
        max_length=_MAX_INTERFACE_REQUESTS,
        description="One answer or unresolved boundary per actual incoming request.",
    )

    @model_validator(mode="after")
    def request_ids_are_unique(self) -> "ModuleInterfaceResponseSubmission":
        request_ids = [item.request_id for item in self.dispositions]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("response request_ids must be unique")
        return self


class ResolvedInterface(_StrictModel):
    """Barrier-owned immutable pairing of a request and its disposition."""

    request: InterfaceRequest
    disposition: InterfaceDisposition

    @model_validator(mode="after")
    def identity_matches(self) -> "ResolvedInterface":
        if self.request.request_id != self.disposition.request_id:
            raise ValueError("request and disposition ids must match")
        return self


class PeerInterfaceSignal(_StrictModel):
    """One non-empty Wave 1 peer signal routed to its target author."""

    source_module_id: ModuleId
    coverage: ModuleInterfaceCoverage

    @model_validator(mode="after")
    def signal_is_directed_to_a_peer(self) -> "PeerInterfaceSignal":
        if self.source_module_id == self.coverage.target_module_id:
            raise ValueError("peer interface signal must target another module")
        if self.coverage.status == "not_applicable":
            raise ValueError(
                "not_applicable coverage is not an incoming peer signal"
            )
        return self


class ModuleCollaborationBundle(_StrictModel):
    """Barrier 2 context that one Wave 3 author must consume."""

    kind: Literal["module_collaboration_bundle"] = "module_collaboration_bundle"
    module_id: ModuleId
    discovery_summary: str = Field(
        min_length=1,
        max_length=3000,
        description=(
            "The module's own Wave 1 evidence-bounded findings, reused by Wave 3 "
            "so the author does not repeat discovery research."
        ),
    )
    discovery_evidence_ids: list[EvidenceId] = Field(
        default_factory=list,
        max_length=_MAX_EVIDENCE_IDS,
        description="Current-run E-* ids already used by the module in Wave 1.",
    )
    peer_coverage: list[ModuleInterfaceCoverage] = Field(
        min_length=4,
        max_length=4,
        description="The module's exhaustive Wave 1 peer coverage.",
    )
    incoming_peer_signals: list[PeerInterfaceSignal] = Field(
        default_factory=list,
        max_length=_MAX_INTERFACE_REQUESTS,
        description=(
            "Offer/request/conflict coverage emitted by peers toward this "
            "module, including offers that do not require a Wave 2 response."
        ),
    )
    requested_interfaces: list[ResolvedInterface] = Field(
        default_factory=list,
        max_length=_MAX_INTERFACE_REQUESTS,
        description="Answers to requests originally emitted by this module.",
    )
    responded_interfaces: list[ResolvedInterface] = Field(
        default_factory=list,
        max_length=_MAX_INTERFACE_REQUESTS,
        description="Requests this module answered or explicitly bounded.",
    )

    @model_validator(mode="after")
    def bundle_is_scoped_to_one_module(self) -> "ModuleCollaborationBundle":
        expected_peers = set(MODULE_IDS) - {self.module_id}
        peer_ids = [item.target_module_id for item in self.peer_coverage]
        if len(peer_ids) != len(set(peer_ids)) or set(peer_ids) != expected_peers:
            raise ValueError("bundle peer_coverage must contain every other module once")
        if any(
            item.request.requester_module_id != self.module_id
            for item in self.requested_interfaces
        ):
            raise ValueError("requested_interfaces must be owned by bundle module_id")
        if any(
            item.request.target_module_id != self.module_id
            for item in self.responded_interfaces
        ):
            raise ValueError("responded_interfaces must target bundle module_id")
        for values in (self.requested_interfaces, self.responded_interfaces):
            request_ids = [item.request.request_id for item in values]
            if len(request_ids) != len(set(request_ids)):
                raise ValueError("bundle interface request_ids must be unique")
        if (
            any(
                not evidence_id.startswith("E-")
                for evidence_id in self.discovery_evidence_ids
            )
            or len(self.discovery_evidence_ids)
            != len(set(self.discovery_evidence_ids))
        ):
            raise ValueError(
                "bundle discovery_evidence_ids must be unique E-* ids"
            )
        if any(
            signal.coverage.target_module_id != self.module_id
            for signal in self.incoming_peer_signals
        ):
            raise ValueError(
                "incoming peer signals must target bundle module_id"
            )
        signal_sources = [
            signal.source_module_id
            for signal in self.incoming_peer_signals
        ]
        if len(signal_sources) != len(set(signal_sources)):
            raise ValueError(
                "incoming peer signal source_module_ids must be unique"
            )
        return self


def _validate_known_evidence(
    evidence_ids: Collection[str],
    known_evidence_ids: Collection[str] | None,
    *,
    label: str,
) -> None:
    if known_evidence_ids is None:
        return
    unknown = sorted(set(evidence_ids) - set(known_evidence_ids))
    if unknown:
        raise ValueError(f"{label} contains stale or unknown evidence ids: {unknown}")


def build_interface_inboxes(
    discoveries: Sequence[ModuleDiscoverySubmission],
    *,
    known_evidence_ids: Collection[str] | None = None,
) -> dict[ModuleId, tuple[InterfaceRequest, ...]]:
    """Barrier 1: validate five discoveries and return only non-empty Wave 2 inboxes."""

    if len(discoveries) != len(MODULE_IDS):
        raise ValueError("Barrier 1 requires exactly five module discoveries")
    by_module = {item.module_id: item for item in discoveries}
    if len(by_module) != len(discoveries) or set(by_module) != set(MODULE_IDS):
        raise ValueError("Barrier 1 requires one discovery for each module 2.1-2.5")

    requests: list[InterfaceRequest] = []
    for module_id in MODULE_IDS:
        discovery = by_module[module_id]
        _validate_known_evidence(
            discovery.evidence_ids,
            known_evidence_ids,
            label=f"discovery {module_id}",
        )
        for request in discovery.requests:
            _validate_known_evidence(
                request.evidence_ids,
                known_evidence_ids,
                label=f"request {request.request_id}",
            )
            requests.append(request)
    request_ids = [request.request_id for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Barrier 1 requires globally unique request_ids")

    return {
        module_id: tuple(
            sorted(
                (
                    request.model_copy(deep=True)
                    for request in requests
                    if request.target_module_id == module_id
                ),
                key=lambda item: item.request_id,
            )
        )
        for module_id in MODULE_IDS
        if any(request.target_module_id == module_id for request in requests)
    }


def build_collaboration_bundles(
    discoveries: Sequence[ModuleDiscoverySubmission],
    responses: Sequence[ModuleInterfaceResponseSubmission],
    *,
    known_evidence_ids: Collection[str] | None = None,
) -> dict[ModuleId, ModuleCollaborationBundle]:
    """Barrier 2: require exact inbox dispositions and build five author bundles."""

    inboxes = build_interface_inboxes(
        discoveries,
        known_evidence_ids=known_evidence_ids,
    )
    by_responder = {item.module_id: item for item in responses}
    if len(by_responder) != len(responses):
        raise ValueError("Barrier 2 response module_ids must be unique")
    if set(by_responder) != set(inboxes):
        raise ValueError(
            "Wave 2 must return exactly the modules with non-empty incoming request inboxes"
        )

    requests_by_id = {
        request.request_id: request
        for inbox in inboxes.values()
        for request in inbox
    }
    dispositions_by_id: dict[str, InterfaceDisposition] = {}
    for module_id, inbox in inboxes.items():
        expected_ids = {request.request_id for request in inbox}
        response = by_responder[module_id]
        actual_ids = {item.request_id for item in response.dispositions}
        if actual_ids != expected_ids:
            raise ValueError(
                f"Wave 2 response {module_id} must disposition only and all incoming requests"
            )
        for disposition in response.dispositions:
            _validate_known_evidence(
                disposition.evidence_ids,
                known_evidence_ids,
                label=f"response {disposition.request_id}",
            )
            dispositions_by_id[disposition.request_id] = disposition
    if set(dispositions_by_id) != set(requests_by_id):
        raise ValueError("Barrier 2 requires every request answered or unresolved")

    resolved = {
        request_id: ResolvedInterface(
            request=request.model_copy(deep=True),
            disposition=dispositions_by_id[request_id].model_copy(deep=True),
        )
        for request_id, request in requests_by_id.items()
    }
    discovery_by_module = {item.module_id: item for item in discoveries}
    return {
        module_id: ModuleCollaborationBundle(
            module_id=module_id,
            discovery_summary=discovery_by_module[
                module_id
            ].discovery_summary,
            discovery_evidence_ids=list(
                discovery_by_module[module_id].evidence_ids
            ),
            peer_coverage=[
                item.model_copy(deep=True)
                for item in discovery_by_module[module_id].interface_coverage
            ],
            incoming_peer_signals=sorted(
                (
                    PeerInterfaceSignal(
                        source_module_id=source_module_id,
                        coverage=coverage.model_copy(deep=True),
                    )
                    for source_module_id, discovery in (
                        discovery_by_module.items()
                    )
                    for coverage in discovery.interface_coverage
                    if (
                        coverage.target_module_id == module_id
                        and coverage.status != "not_applicable"
                    )
                ),
                key=lambda item: item.source_module_id,
            ),
            requested_interfaces=sorted(
                (
                    item.model_copy(deep=True)
                    for item in resolved.values()
                    if item.request.requester_module_id == module_id
                ),
                key=lambda item: item.request.request_id,
            ),
            responded_interfaces=sorted(
                (
                    item.model_copy(deep=True)
                    for item in resolved.values()
                    if item.request.target_module_id == module_id
                ),
                key=lambda item: item.request.request_id,
            ),
        )
        for module_id in MODULE_IDS
    }
