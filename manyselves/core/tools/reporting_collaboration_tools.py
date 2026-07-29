"""Typed, artifact-oriented collaboration and completion tools."""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from html import escape
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ...interfaces.types import (
    AgentResultMessage,
    BlockedNoticeMessage,
    PeerQueryMessage,
    PeerReplyMessage,
    ProgressNoteMessage,
    UserMessage,
)
from ..loops.bus import MessageBus
from ..reporting.agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    AgentResult,
    AgentRunStatus,
    ChiefRevisionSubmission,
    ChiefRevisionSubmissionInput,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    EditedReportSubmission,
    EditedReportSubmissionInput,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleRevisionSubmissionInput,
    ModuleSubmission,
    ModuleSubmissionInput,
    TableSubmission,
    TemplateSkillSubmission,
    WorkflowDecisionSubmission,
)
from ..reporting.claim_ledger import ClaimLedger
from ..reporting.input_contracts import (
    INPUT_CONTRACT_TYPES,
    AggregateEditorInput,
    ChiefEditorInput,
    ChiefRevisionInput,
    CrossReviewInput,
    FinalReviewInput,
    ModuleAuthoringInput,
    ModuleReviewInput,
    ModuleRevisionInput,
    TemplateDistillationInput,
    WorkflowExceptionInput,
)
from ..reporting.message_router import artifact_path_refs, source_record_ids
from ..reporting.models import CHIEF_SECTION_RESULT_PART_IDS
from ..reporting.source_ledger import SourceLedger
from ..reporting.store import ReportingStore
from ..reporting.submission_contracts import submission_schema
from ..reporting.taxonomy import REPORT_TAXONOMY
from .document_tool import InspectDocumentTool
from .registry import Tool


class _ResultTool(Tool):
    def __init__(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        task_id: str,
        store: ReportingStore,
        bus: MessageBus,
        workflow_id: str = "",
    ):
        if Path(task_id).name != task_id or not task_id:
            raise ValueError("task_id must be a single safe path component")
        self.agent_id = agent_id
        self.session_id = session_id
        self.run_id = run_id
        self.task_id = task_id
        self.store = store
        self.bus = bus
        self.workflow_id = workflow_id

    async def _persist_and_publish(self, result: AgentResult) -> str:
        path = self.store.write_run_model(self.run_id, f"results/{self.task_id}.json", result)
        relative = path.relative_to(self.store.workspace).as_posix()
        await self.bus.publish(
            AgentResultMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                run_id=self.run_id,
                sender=self.agent_id,
                recipient="workflow",
                artifact_refs=[relative],
                result_path=relative,
                status=result.status.value,
                content=result.reason or "",
            )
        )
        return relative


class SubmitResultTool(_ResultTool):
    name = "submit_result"
    description = (
        "Persist the current task's typed final result. Call when the assigned work is "
        "complete; research is optional and is not a prerequisite."
    )
    max_validation_failures = 8

    def __init__(
        self,
        *args,
        allowed_outputs: list[str] | None = None,
        revision: int = 0,
        input_contract_kind: str | None = None,
        input_contract_ref: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.allowed_outputs = frozenset(allowed_outputs or ())
        self.revision = revision
        self.input_contract_kind = input_contract_kind
        self.input_contract_ref = input_contract_ref
        submissions_root = (
            self.store.workspace / "Work/runs" / self.run_id / "submissions" / self.task_id
        )
        attempt_pattern = re.compile(r"^attempt-([1-9]\d*)-raw\.json$")
        persisted_attempts = [
            int(match.group(1))
            for path in submissions_root.glob("attempt-*-raw.json")
            if (match := attempt_pattern.fullmatch(path.name)) is not None
        ]
        self._submission_attempt = max(persisted_attempts, default=0)
        self._validation_failures = 0
        self._validation_fingerprints: dict[tuple, int] = {}

    @property
    def _draft_root(self) -> Path:
        return (
            self.store.workspace
            / "Work/runs"
            / self.run_id
            / "drafts"
            / self.task_id
            / f"r{self.revision}"
        ).resolve()

    @staticmethod
    def _generated_part_claim_id(module_id: str, part_id: str) -> str:
        """Return the runtime-owned stable Claim id for one fixed report part."""

        return f"C-{module_id}-{part_id.replace('.', '-')}"

    def _bound_module_part(self, part_id: str) -> tuple[str, list[str], dict]:
        """Load prose and its author-declared E-* binding without accepting paths."""

        prose_path = self._draft_root / f"{part_id}.md"
        binding_path = self._draft_root / "_evidence" / f"{part_id}.json"
        if not prose_path.is_file() or not binding_path.is_file():
            raise SubmissionValidationError(
                "module part is not ready for commit",
                field=f"result_parts.{part_id}",
                expected="saved prose plus an explicit evidence_ids binding",
                example={
                    "part_id": part_id,
                    "content": "完整小节正文，不含任何 [[CLAIM:...]] 标记。",
                    "evidence_ids": ["E-0001"],
                },
                received={
                    "prose_saved": prose_path.is_file(),
                    "evidence_binding_saved": binding_path.is_file(),
                },
                repair_instruction=(
                    f"Call write_result_part for part_id={part_id!r} with the complete "
                    "prose and its registered E-* evidence_ids. Do not submit a file "
                    "path, artifact_ref, Claim id, or inline Claim marker."
                ),
            )
        prose = prose_path.read_text(encoding="utf-8")
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SubmissionValidationError(
                "module part evidence binding is unreadable",
                field=f"result_parts.{part_id}.evidence_ids",
                expected="a write_result_part-managed evidence binding",
                received=None,
                repair_instruction=(
                    f"Call write_result_part again for part_id={part_id!r}; do not edit "
                    "the binding artifact directly."
                ),
            ) from exc
        evidence_ids = binding.get("evidence_ids") if isinstance(binding, dict) else None
        if (
            not isinstance(evidence_ids, list)
            or not all(
                isinstance(source_id, str) and source_id.startswith("E-")
                for source_id in evidence_ids
            )
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise SubmissionValidationError(
                "module part evidence_ids must be a unique E-* list",
                field=f"result_parts.{part_id}.evidence_ids",
                expected="unique current-run project evidence ids, or [] for an explicit gap",
                example=["E-0001"],
                received=evidence_ids,
                repair_instruction=(
                    f"Call write_result_part again for part_id={part_id!r} using only "
                    "current-run E-* ids returned by project evidence tools."
                ),
            )
        if "[[CLAIM:" in prose:
            raise SubmissionValidationError(
                "module prose contains a retired model-authored Claim marker",
                field=f"result_parts.{part_id}.content",
                expected="plain report prose with no [[CLAIM:...]] tokens",
                example="完整小节正文；引用位置由运行时根据 evidence_ids 生成。",
                received="contains [[CLAIM:...]]",
                repair_instruction=(
                    f"Call write_result_part for part_id={part_id!r} with the same prose "
                    "after removing every [[CLAIM:...]] token. Preserve all real wording "
                    "and pass the E-* ids separately in evidence_ids."
                ),
            )
        known_evidence = {
            source.id
            for source in SourceLedger(self.store.workspace, self.run_id).records
            if source.id.startswith("E-")
        }
        unknown = sorted(set(evidence_ids) - known_evidence)
        if unknown:
            raise SubmissionValidationError(
                f"module part references unregistered project evidence: {unknown}",
                field=f"result_parts.{part_id}.evidence_ids",
                expected="only E-* ids registered in the current run",
                example=sorted(set(evidence_ids) & known_evidence),
                received=evidence_ids,
                repair_instruction=(
                    f"Call write_result_part again for part_id={part_id!r} after removing "
                    "invented or stale ids. Do not replace them with Claim or artifact ids."
                ),
            )
        return prose, evidence_ids, binding

    def _bound_text_part(self, part_id: str) -> tuple[str, str]:
        """Load one exact current-task prose part without accepting a model path."""

        prose_path = self._draft_root / f"{part_id}.md"
        if not prose_path.is_file():
            raise SubmissionValidationError(
                "chief revision part is not ready for commit",
                field=f"result_parts.{part_id}",
                expected="a non-empty result part saved by write_result_part",
                example={"part_id": part_id, "content": "修订后的完整目标小节正文。"},
                received={"prose_saved": False},
                repair_instruction=(
                    f"Call write_result_part for part_id={part_id!r} with the complete "
                    "revised section, then resubmit the same compact chief revision."
                ),
            )
        prose = prose_path.read_text(encoding="utf-8")
        if not prose.strip():
            raise SubmissionValidationError(
                "chief revision part is empty",
                field=f"result_parts.{part_id}",
                expected="a non-empty complete target-section body",
                received="",
            )
        relative = prose_path.relative_to(self.store.workspace).as_posix()
        return prose, relative

    def _runtime_claim_for_part(
        self,
        *,
        module_id: str,
        part_id: str,
        prose: str,
        evidence_ids: list[str],
    ) -> dict[str, object]:
        claim_id = self._generated_part_claim_id(module_id, part_id)
        return {
            "id": claim_id,
            "module_id": module_id,
            "submodule_id": part_id,
            "text": prose.strip(),
            "claim_type": "technical_interpretation",
            "source_ids": evidence_ids,
            "confidence": 1.0,
            "footnote_required": bool(evidence_ids),
            "unresolved": not evidence_ids,
        }

    def _assemble_module_commit(
        self,
        commit: ModuleSubmissionInput,
    ) -> ModuleSubmission:
        contract = self._feedback_input_contract()
        if isinstance(contract, ModuleAuthoringInput) and (
            commit.module_id != contract.module_id or commit.revision != contract.revision
        ):
            raise SubmissionValidationError(
                "module commit identity differs from the active authoring input",
                field="$identity",
                expected={
                    "module_id": contract.module_id,
                    "revision": contract.revision,
                },
                example={
                    "module_id": contract.module_id,
                    "revision": contract.revision,
                },
                received={
                    "module_id": commit.module_id,
                    "revision": commit.revision,
                },
                repair_instruction=(
                    "Copy module_id and revision exactly from the active input contract; "
                    "do not alter saved prose or evidence bindings."
                ),
            )
        required_part_ids = (
            list(contract.required_submodule_ids)
            if isinstance(contract, ModuleAuthoringInput)
            else list(REPORT_TAXONOMY[commit.module_id].submodules)
        )
        narratives: dict[str, str] = {}
        claims: list[dict[str, object]] = []
        for part_id in required_part_ids:
            prose, evidence_ids, _ = self._bound_module_part(part_id)
            claim = self._runtime_claim_for_part(
                module_id=commit.module_id,
                part_id=part_id,
                prose=prose,
                evidence_ids=evidence_ids,
            )
            claim_id = str(claim["id"])
            narratives[part_id] = (
                prose.rstrip() + f"\n\n[[CLAIM:{claim_id}]]" if evidence_ids else prose
            )
            claims.append(claim)
        return ModuleSubmission.model_validate(
            {
                "kind": "module_submission",
                "module_id": commit.module_id,
                "submodule_narratives": narratives,
                "claims": claims,
                "source_ids": sorted(
                    {source_id for claim in claims for source_id in claim["source_ids"]}
                ),
                "unresolved_questions": commit.unresolved_questions,
                "revision": commit.revision,
                "revision_responses": [
                    response.model_dump(mode="python") for response in commit.revision_responses
                ],
            }
        )

    def _assemble_module_revision_commit(
        self,
        commit: ModuleRevisionSubmissionInput,
    ) -> ModuleRevisionSubmission:
        contract = self._feedback_input_contract()
        if not isinstance(contract, ModuleRevisionInput):
            raise SubmissionValidationError(
                "module revision commit requires its active revision input",
                field="$contract",
                expected="a readable module_revision_input for this task",
                received=self.input_contract_ref,
            )
        subject = self._module_revision_subject(contract)
        if (
            commit.module_id != contract.module_id
            or commit.base_revision != subject.revision
            or commit.revision != subject.revision + 1
        ):
            raise SubmissionValidationError(
                "module revision commit identity differs from the active revision input",
                field="$identity",
                expected={
                    "module_id": contract.module_id,
                    "base_revision": subject.revision,
                    "revision": subject.revision + 1,
                },
                example={
                    "module_id": contract.module_id,
                    "base_revision": subject.revision,
                    "revision": subject.revision + 1,
                },
                received={
                    "module_id": commit.module_id,
                    "base_revision": commit.base_revision,
                    "revision": commit.revision,
                },
                repair_instruction=(
                    "Copy module_id, base_revision, and revision exactly from the active "
                    "revision input; do not alter saved prose or evidence bindings."
                ),
            )
        narratives: dict[str, str] = {}
        claims_upsert: list[dict[str, object]] = []
        for part_id in contract.target_submodule_ids:
            prose, evidence_ids, _ = self._bound_module_part(part_id)
            claim = self._runtime_claim_for_part(
                module_id=commit.module_id,
                part_id=part_id,
                prose=prose,
                evidence_ids=evidence_ids,
            )
            claim_id = str(claim["id"])
            narratives[part_id] = (
                prose.rstrip() + f"\n\n[[CLAIM:{claim_id}]]" if evidence_ids else prose
            )
            claims_upsert.append(claim)
        target_ids = set(contract.target_submodule_ids)
        removed_claim_ids = [
            claim.id
            for claim in subject.claims
            if claim.submodule_id in target_ids
            and claim.id not in {str(claim["id"]) for claim in claims_upsert}
        ]
        resulting_claims = [
            claim for claim in subject.claims if claim.submodule_id not in target_ids
        ]
        resulting_source_ids = {
            source_id for claim in resulting_claims for source_id in claim.source_ids
        }
        resulting_source_ids.update(
            source_id for claim in claims_upsert for source_id in claim["source_ids"]
        )
        return ModuleRevisionSubmission.model_validate(
            {
                "kind": "module_revision_submission",
                "module_id": commit.module_id,
                "base_revision": commit.base_revision,
                "revision": commit.revision,
                "submodule_narratives": narratives,
                "claims_upsert": claims_upsert,
                "claim_ids_remove": removed_claim_ids,
                "source_ids": sorted(resulting_source_ids),
                "unresolved_questions": commit.unresolved_questions,
                "revision_responses": [
                    response.model_dump(mode="python") for response in commit.revision_responses
                ],
            }
        )

    def _assemble_chief_revision_commit(
        self,
        commit: ChiefRevisionSubmissionInput,
    ) -> ChiefRevisionSubmission:
        contract = self._feedback_input_contract()
        if not isinstance(contract, ChiefRevisionInput):
            raise SubmissionValidationError(
                "chief revision commit requires its active revision input",
                field="$contract",
                expected="a readable chief_revision_input for this task",
                received=self.input_contract_ref,
            )
        if (
            commit.base_subject_ref != contract.subject_ref
            or commit.revision != contract.revision
            or commit.revision != self.revision
        ):
            raise SubmissionValidationError(
                "chief revision identity differs from the active revision input",
                field="$identity",
                expected={
                    "base_subject_ref": contract.subject_ref,
                    "revision": contract.revision,
                },
                example={
                    "base_subject_ref": contract.subject_ref,
                    "revision": contract.revision,
                },
                received={
                    "base_subject_ref": commit.base_subject_ref,
                    "revision": commit.revision,
                },
            )
        section_bodies: dict[str, str] = {}
        section_part_refs: dict[str, str] = {}
        for section_id in contract.target_section_ids:
            part_id = CHIEF_SECTION_RESULT_PART_IDS[section_id]
            prose, relative = self._bound_text_part(part_id)
            section_bodies[section_id] = prose
            section_part_refs[section_id] = relative
        return ChiefRevisionSubmission(
            base_subject_ref=commit.base_subject_ref,
            revision=commit.revision,
            section_bodies=section_bodies,
            section_part_refs=section_part_refs,
            revision_responses=commit.revision_responses,
        )

    def _assemble_edited_report(
        self,
        materialized: dict[str, object],
    ) -> EditedReportSubmission:
        """Derive internal C bindings from model-authored E evidence selections."""

        contract = self._feedback_input_contract()
        claims = []
        ledger_path = self.store.workspace / f"Work/runs/{self.run_id}/ledgers/claims.json"
        if ledger_path.is_file():
            claims = ClaimLedger.model_validate_json(ledger_path.read_text(encoding="utf-8")).claims

        known_evidence = {
            source.id
            for source in SourceLedger(self.store.workspace, self.run_id).records
            if source.id.startswith("E-")
        }
        tables: list[TableSubmission] = []
        raw_tables = materialized.pop("tables", [])
        if not isinstance(raw_tables, list):
            raise SubmissionValidationError(
                "tables must be a list",
                field="tables",
                expected="a list of tables using evidence_ids",
                received=raw_tables,
            )
        for index, raw_table in enumerate(raw_tables):
            if not isinstance(raw_table, dict):
                raise SubmissionValidationError(
                    "table must be an object",
                    field=f"tables.{index}",
                    expected="title, headers, rows, and evidence_ids",
                    received=raw_table,
                )
            table = dict(raw_table)
            evidence_ids = list(table.pop("evidence_ids", []))
            unknown = sorted(set(evidence_ids) - known_evidence)
            if unknown:
                raise SubmissionValidationError(
                    f"table uses unregistered evidence ids: {unknown}",
                    field=f"tables.{index}.evidence_ids",
                    expected="registered current-run E-* ids only",
                    example=sorted(set(evidence_ids) & known_evidence),
                    received=evidence_ids,
                )
            claim_ids = sorted(
                claim.id for claim in claims if set(claim.source_ids) & set(evidence_ids)
            )
            if not claim_ids:
                raise SubmissionValidationError(
                    "table evidence is not linked to any approved module content",
                    field=f"tables.{index}.evidence_ids",
                    expected="E-* ids already bound to an approved module part",
                    received=evidence_ids,
                )
            tables.append(
                TableSubmission.model_validate(
                    {
                        **table,
                        "source_ids": evidence_ids,
                        "claim_ids": claim_ids,
                    }
                )
            )

        protected_claim_ids = sorted(claim.id for claim in claims)
        if not isinstance(contract, (ChiefEditorInput, AggregateEditorInput)):
            raise SubmissionValidationError(
                "edited report requires a chief-editor input contract",
                field="$contract",
                received=self.input_contract_ref,
            )

        return EditedReportSubmission.model_validate(
            {
                **materialized,
                "special_topic_plan": (
                    contract.special_topic_plan.model_dump(mode="python")
                    if contract.special_topic_plan is not None
                    else None
                ),
                "protected_claim_ids": protected_claim_ids,
                "tables": [table.model_dump(mode="python") for table in tables],
            }
        )

    @staticmethod
    def _looks_like_text_artifact_ref(value: str) -> bool:
        return (
            "\n" not in value
            and value.startswith("Work/runs/")
            and "/drafts/" in value
            and value.endswith(".md")
        )

    def _read_text_artifact_refs(
        self,
        refs: list[str],
        *,
        separator: str,
        path: tuple[str | int, ...],
    ) -> str:
        parts: list[str] = []
        for ref in refs:
            target = (self.store.workspace / ref).resolve()
            if (
                not target.is_relative_to(self._draft_root)
                or target.suffix != ".md"
                or not target.is_file()
            ):
                raise SubmissionValidationError(
                    "text artifact must belong to the active run, task, and revision",
                    field=".".join(map(str, path)),
                    expected=(
                        f"Markdown files under Work/runs/{self.run_id}/drafts/"
                        f"{self.task_id}/r{self.revision}/"
                    ),
                    example=(
                        f"Work/runs/{self.run_id}/drafts/{self.task_id}/"
                        f"r{self.revision}/<part-id>.md"
                    ),
                    received=ref,
                    repair_instruction=(
                        "Use exactly the artifact_ref returned by write_result_part for "
                        "this active task; do not copy or synthesize another path."
                    ),
                )
            parts.append(target.read_text(encoding="utf-8"))
        return separator.join(parts)

    def _materialize_text_artifacts(
        self,
        value,
        path: tuple[str | int, ...] = (),
    ):
        if isinstance(value, str):
            if "result_part_refs" in path:
                return value
            if self._looks_like_text_artifact_ref(value):
                return self._read_text_artifact_refs([value], separator="\n\n", path=path)
            return value
        if isinstance(value, list):
            return [
                self._materialize_text_artifacts(item, (*path, index))
                for index, item in enumerate(value)
            ]
        if not isinstance(value, dict):
            return value
        if "artifact_refs" in value and set(value).issubset({"artifact_refs", "separator"}):
            refs = value["artifact_refs"]
            separator = value.get("separator", "\n\n")
            if (
                not isinstance(refs, list)
                or not refs
                or not all(isinstance(ref, str) for ref in refs)
            ):
                raise SubmissionValidationError(
                    "artifact_refs must be a non-empty string list",
                    field=".".join(map(str, (*path, "artifact_refs"))),
                    expected=("a non-empty array of current-task Markdown artifact paths"),
                    example=[
                        f"Work/runs/{self.run_id}/drafts/{self.task_id}/"
                        f"r{self.revision}/<part-id>.md"
                    ],
                    received=refs,
                    repair_instruction=(
                        "Replace artifact_refs with paths returned by write_result_part "
                        "for this run, task, and revision, then resubmit the complete "
                        "native JSON object."
                    ),
                )
            if not isinstance(separator, str) or len(separator) > 20:
                raise SubmissionValidationError(
                    "artifact separator must be a short string",
                    field=".".join(map(str, (*path, "separator"))),
                    expected="a string no longer than 20 characters",
                    example="\n\n",
                    received=separator,
                )
            return self._read_text_artifact_refs(
                refs,
                separator=separator,
                path=(*path, "artifact_refs"),
            )
        return {
            key: self._materialize_text_artifacts(item, (*path, key)) for key, item in value.items()
        }

    @staticmethod
    def _validation_fingerprint(error: Exception) -> tuple:
        if isinstance(error, ValidationError):
            return tuple(
                sorted(
                    (
                        tuple(str(part) for part in item["loc"]),
                        str(item["type"]),
                    )
                    for item in error.errors(include_url=False)
                )
            )
        return ((type(error).__name__, str(error)),)

    @classmethod
    def _correction_fingerprint(
        cls,
        error: Exception,
        issues: list[dict[str, object]],
    ) -> tuple:
        """Identify the concrete correction, not Pydantic's shared root location."""

        if not issues:
            return cls._validation_fingerprint(error)
        return tuple(
            sorted(
                (
                    str(issue.get("field", "$")),
                    str(issue.get("problem", "")),
                    json.dumps(
                        issue.get("expected"),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                )
                for issue in issues
            )
        )

    def _feedback_input_contract(self):
        """Load context only for correction examples without masking the real error."""

        if not self.input_contract_kind or not self.input_contract_ref:
            return None
        model = INPUT_CONTRACT_TYPES.get(self.input_contract_kind)
        if model is None:
            return None
        target = (self.store.workspace / self.input_contract_ref).resolve()
        if not target.is_relative_to(self.store.workspace) or not target.is_file():
            return None
        try:
            return model.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return None

    def _module_revision_subject(
        self,
        contract: ModuleRevisionInput,
    ) -> ModuleSubmission:
        target = (self.store.workspace / contract.subject_ref).resolve()
        run_root = (self.store.workspace / f"Work/runs/{self.run_id}").resolve()
        if not target.is_relative_to(run_root) or not target.is_file():
            raise SubmissionValidationError(
                "module revision baseline is not a readable current-run artifact",
                field="$contract.subject_ref",
                expected="the exact current module artifact assigned by the workflow",
                received=contract.subject_ref,
            )
        try:
            return ModuleSubmission.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise SubmissionValidationError(
                "module revision baseline is invalid",
                field="$contract.subject_ref",
                expected="a valid internal module subject",
                received=contract.subject_ref,
            ) from exc

    def _contextual_submission_example(
        self,
        kind: str,
        schema: dict,
        contract,
    ) -> object:
        example = deepcopy(schema.get("examples", [{}])[0])
        if kind == "module_submission" and isinstance(contract, ModuleAuthoringInput):
            example["module_id"] = contract.module_id
            example["revision"] = contract.revision
        elif kind == "module_revision_submission" and isinstance(contract, ModuleRevisionInput):
            example["module_id"] = contract.module_id
            example["base_revision"] = contract.subject.revision
            example["revision"] = contract.subject.revision + 1
            example["unresolved_questions"] = list(contract.subject.unresolved_questions)
            finding_ids = [
                *(finding.id for finding in contract.module_findings),
                *(finding.id for finding in contract.cross_findings),
                *(change.id for change in contract.requested_changes),
            ]
            first_target = contract.target_submodule_ids[0]
            example["revision_responses"] = [
                {
                    "finding_id": finding_id,
                    "action": "implemented",
                    "summary": "已按 finding 修订目标小节并保留其余证据边界。",
                    "changed_target_ids": [first_target],
                }
                for finding_id in finding_ids
            ]
        elif kind == "chief_revision_submission" and isinstance(contract, ChiefRevisionInput):
            example["base_subject_ref"] = contract.subject_ref
            example["revision"] = contract.revision
            first_target = contract.target_section_ids[0]
            example["revision_responses"] = [
                {
                    "finding_id": finding.id,
                    "action": "implemented",
                    "summary": "已在指定小节完成所需修改，其余报告内容由运行时继承。",
                    "changed_target_ids": [
                        change.target_section_id for change in finding.target_changes
                    ]
                    or [first_target],
                }
                for finding in contract.findings
            ]
        return example

    def _module_revision_candidate(
        self,
        contract: ModuleRevisionInput,
        patch: ModuleRevisionSubmission,
    ) -> dict[str, object]:
        subject = self._module_revision_subject(contract)
        claims = {claim.id: claim for claim in subject.claims}
        for claim_id in patch.claim_ids_remove:
            claims.pop(claim_id, None)
        for claim in patch.claims_upsert:
            claims[claim.id] = claim
        narratives = dict(subject.submodule_narratives)
        narratives.update(patch.submodule_narratives)
        return {
            "kind": "module_submission",
            "module_id": contract.module_id,
            "submodule_narratives": narratives,
            "claims": [claim.model_dump(mode="python") for claim in claims.values()],
            "source_ids": list(patch.source_ids),
            "unresolved_questions": list(patch.unresolved_questions),
            "revision": patch.revision,
            "revision_responses": [
                response.model_dump(mode="python") for response in patch.revision_responses
            ],
        }

    def _module_semantic_feedback(self, payload: dict | str) -> list[dict[str, object]]:
        """Report every safely discoverable module semantic defect without editing output."""

        if not isinstance(payload, dict):
            return []
        kind = payload.get("kind")
        if kind not in {
            None,
            "",
            "module_submission",
            "module_revision_submission",
        }:
            return []
        narratives_raw = payload.get("submodule_narratives")
        try:
            materialized = self._materialize_text_artifacts(payload)
        except SubmissionValidationError:
            return []
        if kind == "module_revision_submission":
            contract = self._feedback_input_contract()
            if not isinstance(contract, ModuleRevisionInput):
                return []
            try:
                patch = ModuleRevisionSubmission.model_validate(materialized)
            except ValidationError:
                return []
            candidate = self._module_revision_candidate(contract, patch)
            claims = candidate["claims"]
            narratives = candidate["submodule_narratives"]
            declared_sources = candidate["source_ids"]
        else:
            claims = materialized.get("claims")
            narratives = materialized.get("submodule_narratives")
            declared_sources = materialized.get("source_ids")
        if not isinstance(claims, list) or not isinstance(narratives_raw, dict):
            return []
        if not isinstance(narratives, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in narratives.items()
        ):
            return []

        marker_pattern = re.compile(r"\[\[CLAIM:(C-[^\]\s]+)\]\]")
        marker_locations: dict[str, list[str]] = {}
        for submodule_id, narrative in narratives.items():
            for claim_id in marker_pattern.findall(narrative):
                marker_locations.setdefault(claim_id, []).append(submodule_id)

        declared = set(declared_sources) if isinstance(declared_sources, list) else set()
        known_claim_ids = {
            claim.get("id")
            for claim in claims
            if isinstance(claim, dict) and isinstance(claim.get("id"), str)
        }
        feedback: list[dict[str, object]] = []
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            claim_id = claim.get("id")
            submodule_id = claim.get("submodule_id")
            source_ids = claim.get("source_ids", [])
            if not isinstance(claim_id, str) or not isinstance(submodule_id, str):
                continue
            if not isinstance(source_ids, list):
                continue
            if claim.get("claim_type") == "project_fact" and not any(
                isinstance(source_id, str) and source_id.startswith("E-")
                for source_id in source_ids
            ):
                feedback.append(
                    {
                        "field": f"claims.{index}.source_ids",
                        "problem": "project_fact requires at least one E-* source",
                        "expected": "at least one registered current-run E-* source id",
                        "example": ["E-0001"],
                        "received": source_ids,
                        "repair_instruction": (
                            f"Add a real current-run E-* source to Claim {claim_id}, or change "
                            "claim_type only if the wording is genuinely not a project fact. "
                            "Keep module-level source_ids consistent and resubmit the complete "
                            "native JSON object."
                        ),
                    }
                )
            undeclared = sorted(
                source_id
                for source_id in source_ids
                if isinstance(source_id, str) and source_id not in declared
            )
            if undeclared:
                feedback.append(
                    {
                        "field": "source_ids",
                        "problem": f"module source_ids omit Claim {claim_id} sources: {undeclared}",
                        "expected": "every Claim source id declared once at module level",
                        "example": sorted(declared | set(undeclared)),
                        "received": declared_sources,
                        "repair_instruction": (
                            "Add the reported registered Claim source ids to module-level "
                            "source_ids and resubmit the complete native JSON object."
                        ),
                    }
                )
            locations = marker_locations.get(claim_id, [])
            requires_marker = bool(claim.get("footnote_required", True)) and bool(source_ids)
            marker = f"[[CLAIM:{claim_id}]]"
            raw_narrative = narratives_raw.get(submodule_id)
            uses_artifact = (
                isinstance(raw_narrative, dict) and "artifact_refs" in raw_narrative
            ) or (
                isinstance(raw_narrative, str) and self._looks_like_text_artifact_ref(raw_narrative)
            )
            if requires_marker and locations != [submodule_id]:
                action = (
                    f"Call write_result_part with part_id={submodule_id!r} and the complete "
                    "existing narrative, edited to place"
                    if uses_artifact
                    else f"Edit submodule_narratives.{submodule_id} to place"
                )
                feedback.append(
                    {
                        "field": f"submodule_narratives.{submodule_id}",
                        "problem": (
                            f"Claim {claim_id} marker must occur exactly once in submodule "
                            f"{submodule_id}; got locations={locations}"
                        ),
                        "expected": f"exactly one {marker} in submodule {submodule_id}",
                        "example": f"受证据支持的完整陈述 {marker}，随后继续分析。",
                        "received": {
                            "claim_id": claim_id,
                            "marker_locations": locations,
                            "occurrences": len(locations),
                        },
                        "repair_instruction": (
                            f"{action} {marker} exactly once beside the supported statement. "
                            "Remove duplicate or misplaced copies, preserve all unrelated prose, "
                            "then resubmit the complete native JSON object with the returned "
                            "artifact_ref."
                        ),
                    }
                )
            if not requires_marker and locations:
                feedback.append(
                    {
                        "field": f"submodule_narratives.{submodule_id}",
                        "problem": f"Claim {claim_id} does not require a citation marker",
                        "expected": f"no {marker} marker",
                        "example": "保留正文陈述，但不添加 Claim marker。",
                        "received": {"marker_locations": locations},
                        "repair_instruction": (
                            f"Remove every {marker} marker while preserving the prose, then "
                            "resubmit the complete native JSON object."
                        ),
                    }
                )
        for claim_id, locations in sorted(marker_locations.items()):
            if claim_id in known_claim_ids:
                continue
            for submodule_id in sorted(set(locations)):
                feedback.append(
                    {
                        "field": f"submodule_narratives.{submodule_id}",
                        "problem": f"narrative contains unknown Claim marker {claim_id}",
                        "expected": "markers only for Claims declared in claims",
                        "example": "Remove the unknown marker or declare a fully supported Claim.",
                        "received": {"claim_id": claim_id, "marker_locations": locations},
                        "repair_instruction": (
                            f"Remove [[CLAIM:{claim_id}]] unless a real structured Claim with "
                            "that exact id is added. Do not invent a Claim only to satisfy the gate."
                        ),
                    }
                )
        return feedback

    def _validation_issues(
        self,
        error: Exception,
        payload: dict | str,
    ) -> list[dict[str, object]]:
        submitted_kind = str(payload.get("kind", "")) if isinstance(payload, dict) else ""
        allowed_kinds = sorted(self.allowed_outputs)
        active_kind = (
            submitted_kind
            if submitted_kind in self.allowed_outputs
            else allowed_kinds[0]
            if len(allowed_kinds) == 1
            else ""
        )
        schema = submission_schema(active_kind) if active_kind else {}
        contract = self._feedback_input_contract()
        if schema:
            payload_example: object = self._contextual_submission_example(
                active_kind,
                schema,
                contract,
            )
        else:
            payload_example = [
                self._contextual_submission_example(
                    kind,
                    submission_schema(kind),
                    contract,
                )
                for kind in allowed_kinds
            ]

        def resolve(node: dict) -> dict:
            ref = node.get("$ref")
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                return node
            return schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], node)

        def at_path(root: object, loc: tuple[object, ...]) -> object:
            current = root
            for part in loc:
                if isinstance(part, int):
                    if isinstance(current, list) and current:
                        current = current[min(part, len(current) - 1)]
                    continue
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None
            return current

        def feedback_value(value: object) -> object:
            if isinstance(value, str):
                if len(value) <= 320:
                    return value
                return value[:320] + f"... <{len(value) - 320} chars omitted>"
            if isinstance(value, list):
                if len(value) <= 12:
                    return value
                return {
                    "type": "array",
                    "length": len(value),
                    "first_items": value[:12],
                }
            if isinstance(value, dict):
                try:
                    encoded = json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError):
                    encoded = ""
                if encoded and len(encoded) <= 1_000:
                    return value
                return {
                    "type": "object",
                    "keys": list(value)[:24],
                }
            return value

        def json_type(value: object) -> str:
            if value is None:
                return "null"
            if isinstance(value, bool):
                return "boolean"
            if isinstance(value, str):
                return "string"
            if isinstance(value, (int, float)):
                return "number"
            if isinstance(value, list):
                return "array"
            if isinstance(value, dict):
                return "object"
            return type(value).__name__

        def schema_at(loc: tuple[object, ...]) -> dict:
            current = schema
            for part in loc:
                current = resolve(current)
                if isinstance(part, int):
                    current = current.get("items", {})
                else:
                    current = current.get("properties", {}).get(str(part), {})
                if not isinstance(current, dict):
                    return {}
            return resolve(current)

        def expected_text(node: dict) -> str:
            values = node.get("enum")
            if values:
                return "one of " + ", ".join(map(str, values))
            if "const" in node:
                return repr(node["const"])
            kind_name = node.get("type")
            if kind_name:
                return str(kind_name)
            if node.get("anyOf"):
                return "value matching one declared schema branch"
            return "value matching the active submission contract"

        def issue(
            *,
            field: str,
            problem: str,
            expected: str,
            example: object,
            received: object,
            repair_instruction: str,
        ) -> dict[str, object]:
            return {
                "field": field,
                "problem": problem,
                "received_type": json_type(received),
                "received": feedback_value(received),
                "expected": expected,
                "example": example,
                "repair_instruction": repair_instruction,
            }

        semantic_feedback = [
            issue(
                field=str(item["field"]),
                problem=str(item["problem"]),
                expected=str(item["expected"]),
                example=item["example"],
                received=item["received"],
                repair_instruction=str(item["repair_instruction"]),
            )
            for item in self._module_semantic_feedback(payload)
        ]

        if isinstance(error, ValidationError):
            issues = []
            for item in error.errors(include_url=False):
                loc = tuple(item["loc"])
                if loc and loc[0] == "payload":
                    loc = loc[1:]
                if loc and str(loc[0]) in self.allowed_outputs:
                    loc = loc[1:]
                field = ".".join(str(part) for part in loc) or "$"
                field_example = at_path(payload_example, loc)
                if field_example is None:
                    field_example = payload_example
                issues.append(
                    issue(
                        field=field,
                        problem=str(item["msg"]),
                        expected=expected_text(schema_at(loc)),
                        example=field_example,
                        received=at_path(payload, loc),
                        repair_instruction=(
                            f"Correct {field} to the declared type or value and resubmit "
                            "the complete payload as a native JSON object. Preserve all "
                            "unrelated valid content; do not stringify the payload."
                        ),
                    )
                )
            generic_messages = (
                "project_fact requires at least one E-* source",
                "marker must occur exactly once",
                "does not require a citation marker",
                "unknown Claim markers",
            )
            issues = [
                item
                for item in issues
                if not any(message in str(item["problem"]) for message in generic_messages)
            ]
            return [*issues, *semantic_feedback]
        if isinstance(error, SubmissionValidationError):
            loc = tuple(
                int(part) if part.isdigit() else part
                for part in error.field.split(".")
                if part and part != "$"
            )
            field_example = error.example
            if field_example is None:
                field_example = at_path(payload_example, loc) if loc else payload_example
            if field_example is None:
                field_example = payload_example
            received = (
                error.received
                if error.received is not None
                else at_path(payload, loc)
                if loc
                else payload
            )
            return [
                issue(
                    field=error.field,
                    problem=str(error),
                    expected=error.expected,
                    example=field_example,
                    received=received,
                    repair_instruction=error.repair_instruction,
                ),
                *semantic_feedback,
            ]
        return [
            issue(
                field="$",
                problem=str(error),
                expected="value matching the active submission contract",
                example=payload_example,
                received=payload,
                repair_instruction=(
                    "Correct the reported contract violation and resubmit the complete "
                    "payload as a native JSON object without changing unrelated content."
                ),
            )
        ]

    def _load_input_contract(self):
        if not self.input_contract_kind or not self.input_contract_ref:
            return None
        model = INPUT_CONTRACT_TYPES.get(self.input_contract_kind)
        if model is None:
            raise SubmissionValidationError(
                f"unknown input contract kind: {self.input_contract_kind}",
                field="$runtime.input_contract_kind",
                expected=f"one of {sorted(INPUT_CONTRACT_TYPES)}",
                example=sorted(INPUT_CONTRACT_TYPES)[0],
                received=self.input_contract_kind,
                repair_instruction=(
                    "Do not change the submitted payload. The workflow runtime must "
                    "supply a registered input contract kind before retrying this task."
                ),
            )
        target = (self.store.workspace / self.input_contract_ref).resolve()
        if not target.is_relative_to(self.store.workspace) or not target.is_file():
            raise SubmissionValidationError(
                "input contract ref is not a readable workspace artifact",
                field="$runtime.input_contract_ref",
                expected="a readable current-workspace input contract artifact",
                example="Work/runs/<run-id>/context/<task>-input.json",
                received=self.input_contract_ref,
                repair_instruction=(
                    "Do not invent or replace the input contract. The workflow runtime "
                    "must restore the assigned contract artifact before retrying."
                ),
            )
        return model.model_validate_json(target.read_text(encoding="utf-8"))

    @staticmethod
    def _require_exact_ids(
        actual: set[str],
        expected: set[str],
        label: str,
        *,
        field: str,
    ) -> None:
        if actual != expected:
            raise SubmissionValidationError(
                f"{label} must cover exactly the input contract ids; "
                f"missing={sorted(expected - actual)}; "
                f"unexpected={sorted(actual - expected)}",
                field=field,
                expected=f"exactly these ids: {sorted(expected)}",
                example=sorted(expected),
                received=sorted(actual),
                repair_instruction=(
                    f"Set {field} to exactly the ids assigned by the input contract. "
                    "Do not invent, rename, omit, or retain unrelated ids; then resubmit "
                    "the complete native JSON object."
                ),
            )

    def _validate_against_input_contract(self, payload) -> None:
        contract = self._load_input_contract()
        if contract is None:
            return
        if isinstance(contract, TemplateDistillationInput):
            if not isinstance(payload, TemplateSkillSubmission):
                return
            if contract.run_id != self.run_id:
                raise SubmissionValidationError(
                    "template distillation input belongs to another run",
                    field="$runtime.run_id",
                    expected=contract.run_id,
                    example=contract.run_id,
                    received=self.run_id,
                    repair_instruction=(
                        "Do not change the Skill payload. Resume the task under the exact "
                        "run_id named by its input contract."
                    ),
                )
            cache_ref = f"Work/runs/{contract.run_id}/context/template-inspection.json"
            cached = InspectDocumentTool(
                self.store.workspace,
                required_path=contract.template_ref,
                required_max_chars=contract.inspect_max_chars,
                cache_ref=cache_ref,
            ).load_cached_result(
                contract.template_ref,
                max_chars=contract.inspect_max_chars,
            )
            if cached is None or not str(cached.get("text", "")).strip():
                raise SubmissionValidationError(
                    "template Skill submission requires the durable inspection "
                    f"for {contract.template_ref}",
                    field="$runtime.template_inspection",
                    expected=(
                        f"a completed inspection of {contract.template_ref} using "
                        f"max_chars={contract.inspect_max_chars}"
                    ),
                    example=cache_ref,
                    received=None,
                    repair_instruction=(
                        "Inspect the assigned template with inspect_document using the "
                        "required path and max_chars, then resubmit the unchanged complete "
                        "Skill payload."
                    ),
                )
            return
        if isinstance(contract, ModuleAuthoringInput):
            if not isinstance(payload, ModuleSubmission):
                return
            if payload.module_id != contract.module_id or payload.revision != contract.revision:
                raise SubmissionValidationError(
                    "module submission identity or revision differs from its input contract",
                    field="$identity",
                    expected=(f"module_id={contract.module_id!r} and revision={contract.revision}"),
                    example={
                        "module_id": contract.module_id,
                        "revision": contract.revision,
                    },
                    received={
                        "module_id": payload.module_id,
                        "revision": payload.revision,
                    },
                    repair_instruction=(
                        "Copy module_id and revision exactly from the input contract; "
                        "preserve the authored narratives and Claims, then resubmit."
                    ),
                )
            if payload.revision_responses:
                raise SubmissionValidationError(
                    "initial module authoring cannot contain review responses",
                    field="revision_responses",
                    expected="an empty array during initial module authoring",
                    example=[],
                    received=[
                        response.model_dump(mode="json") for response in payload.revision_responses
                    ],
                    repair_instruction=(
                        "Remove revision_responses because this is initial authoring, "
                        "not a review-driven revision; preserve all authored content."
                    ),
                )
            return
        if isinstance(contract, ModuleRevisionInput):
            if not isinstance(payload, ModuleRevisionSubmission):
                return
            subject = self._module_revision_subject(contract)
            expected_ids = {
                *(finding.id for finding in contract.module_findings),
                *(finding.id for finding in contract.cross_findings),
                *(change.id for change in contract.requested_changes),
            }
            self._require_exact_ids(
                {response.finding_id for response in payload.revision_responses},
                expected_ids,
                "module revision responses",
                field="revision_responses.finding_id",
            )
            allowed = set(contract.target_submodule_ids)
            if (
                payload.module_id != contract.module_id
                or payload.base_revision != subject.revision
                or payload.revision != subject.revision + 1
            ):
                raise SubmissionValidationError(
                    "module patch identity or revision differs from its input contract",
                    field="$identity",
                    expected=(
                        f"module_id={contract.module_id!r}, "
                        f"base_revision={subject.revision}, and "
                        f"revision={subject.revision + 1}"
                    ),
                    example={
                        "module_id": contract.module_id,
                        "base_revision": subject.revision,
                        "revision": subject.revision + 1,
                    },
                    received={
                        "module_id": payload.module_id,
                        "base_revision": payload.base_revision,
                        "revision": payload.revision,
                    },
                )
            if set(payload.submodule_narratives) - allowed:
                raise SubmissionValidationError(
                    "module patch contains out-of-scope narratives",
                    field="submodule_narratives",
                    expected=(f"a patch containing only assigned submodule ids: {sorted(allowed)}"),
                    example={
                        submodule_id: "<revised narrative>" for submodule_id in sorted(allowed)
                    },
                    received=sorted(payload.submodule_narratives),
                    repair_instruction=(
                        "Remove narratives outside target_submodule_ids. Do not copy "
                        "unchanged or unassigned submodules into the patch."
                    ),
                )
            baseline_claims = {claim.id: claim for claim in subject.claims}
            for claim_id in payload.claim_ids_remove:
                claim = baseline_claims.get(claim_id)
                if claim is None or claim.submodule_id not in allowed:
                    raise SubmissionValidationError(
                        f"module patch removes an unknown or out-of-scope Claim: {claim_id}",
                        field="claim_ids_remove",
                        expected=(
                            "only existing Claim ids whose submodule is assigned: "
                            f"{sorted(baseline_claims)}"
                        ),
                        example=[
                            claim.id
                            for claim in baseline_claims.values()
                            if claim.submodule_id in allowed
                        ],
                        received=payload.claim_ids_remove,
                    )
            if any(claim.submodule_id not in allowed for claim in payload.claims_upsert):
                raise SubmissionValidationError(
                    "module patch upserts an out-of-scope Claim",
                    field="claims_upsert",
                    expected=(f"Claims whose submodule_id is one of {sorted(allowed)}"),
                    example=[],
                    received=[
                        {"id": claim.id, "submodule_id": claim.submodule_id}
                        for claim in payload.claims_upsert
                    ],
                )
            ModuleSubmission.model_validate(self._module_revision_candidate(contract, payload))
            return
        if isinstance(contract, ModuleReviewInput):
            allowed = set(contract.required_submodule_ids)
            if isinstance(payload, ModuleReviewFindingSubmission):
                if not allowed.issubset(set(payload.coverage.submodule_ids)):
                    raise SubmissionValidationError(
                        "module review coverage omits required submodules",
                        field="coverage.submodule_ids",
                        expected=f"all required submodule ids: {sorted(allowed)}",
                        example=sorted(allowed),
                        received=payload.coverage.submodule_ids,
                    )
                findings = payload.findings
            elif isinstance(payload, ModuleReviewVerdictSubmission):
                self._require_exact_ids(
                    {verdict.finding_id for verdict in payload.verdicts},
                    {finding.id for finding in contract.required_findings},
                    "module review verdicts",
                    field="verdicts.finding_id",
                )
                findings = payload.new_findings
            else:
                return
            if any(finding.target_submodule_id not in allowed for finding in findings):
                raise SubmissionValidationError(
                    "module review finding target lies outside the input contract",
                    field="findings",
                    expected=f"target_submodule_id in {sorted(allowed)}",
                    received=[
                        {
                            "id": finding.id,
                            "target_submodule_id": finding.target_submodule_id,
                        }
                        for finding in findings
                    ],
                )
            return
        if isinstance(contract, CrossReviewInput):
            required_dimensions = {
                "terminology",
                "facts",
                "risk_levels",
                "dependencies",
                "propagation",
                "joint_verification",
            }
            if isinstance(payload, CrossReviewFindingSubmission):
                if contract.phase != "initial":
                    raise SubmissionValidationError(
                        "cross finding submission is only valid for initial phase",
                        field="kind",
                        expected="cross_review_verdict_submission during recheck",
                        example="cross_review_verdict_submission",
                        received=payload.kind,
                    )
            elif isinstance(payload, CrossReviewVerdictSubmission):
                self._require_exact_ids(
                    {verdict.finding_id for verdict in payload.verdicts},
                    {finding.id for finding in contract.required_findings},
                    "cross review verdicts",
                    field="verdicts.finding_id",
                )
            else:
                return
            if any(
                set(entry.checked_dimensions) != required_dimensions for entry in payload.coverage
            ):
                raise SubmissionValidationError(
                    "cross review coverage must include every declared dimension",
                    field="coverage.checked_dimensions",
                    expected=f"exactly these dimensions: {sorted(required_dimensions)}",
                    example=sorted(required_dimensions),
                    received=[
                        {
                            "module_id": entry.module_id,
                            "checked_dimensions": entry.checked_dimensions,
                        }
                        for entry in payload.coverage
                    ],
                )
            known_evidence = {
                source.id
                for source in SourceLedger(self.store.workspace, self.run_id).records
                if source.id.startswith("E-")
            }
            unknown_evidence = sorted(
                {
                    ref
                    for synthesis in payload.synthesis_inputs
                    for ref in synthesis.evidence_refs
                    if ref.startswith("E-") and ref not in known_evidence
                }
            )
            if unknown_evidence:
                raise SubmissionValidationError(
                    "Cross synthesis uses unregistered project evidence",
                    field="synthesis_inputs.evidence_refs",
                    expected=sorted(known_evidence),
                    received=unknown_evidence,
                )
            return
        if isinstance(contract, FinalReviewInput):
            required_sections = set(contract.required_section_ids)
            if isinstance(payload, FinalReviewFindingSubmission):
                if contract.phase != "initial":
                    raise SubmissionValidationError(
                        "final finding submission is only valid for initial phase",
                        field="kind",
                        expected="final_review_verdict_submission during recheck",
                        example="final_review_verdict_submission",
                        received=payload.kind,
                    )
                findings = payload.findings
            elif isinstance(payload, FinalReviewVerdictSubmission):
                self._require_exact_ids(
                    {verdict.finding_id for verdict in payload.verdicts},
                    {finding.id for finding in contract.required_findings},
                    "final review verdicts",
                    field="verdicts.finding_id",
                )
                findings = payload.new_findings
            else:
                return
            if set(payload.checked_section_ids) != required_sections:
                raise SubmissionValidationError(
                    "final review coverage differs from required sections",
                    field="checked_section_ids",
                    expected=f"exactly these section ids: {sorted(required_sections)}",
                    example=sorted(required_sections),
                    received=payload.checked_section_ids,
                )
            if any(
                not set(finding.target_section_ids).issubset(required_sections)
                for finding in findings
            ):
                raise SubmissionValidationError(
                    "final review finding targets an unassigned section",
                    field="findings.target_section_ids",
                    expected=f"section ids drawn from {sorted(required_sections)}",
                    example=sorted(required_sections),
                    received=[
                        {
                            "id": finding.id,
                            "target_section_ids": finding.target_section_ids,
                        }
                        for finding in findings
                    ],
                )
            return
        if isinstance(contract, ChiefRevisionInput):
            if not isinstance(payload, ChiefRevisionSubmission):
                return
            self._require_exact_ids(
                {response.finding_id for response in payload.revision_responses},
                {finding.id for finding in contract.findings},
                "chief revision responses",
                field="revision_responses.finding_id",
            )
            changed_targets = {
                target
                for response in payload.revision_responses
                for target in response.changed_target_ids
            }
            if not changed_targets.issubset(set(contract.target_section_ids)):
                raise SubmissionValidationError(
                    "chief revision response declares an out-of-scope section",
                    field="revision_responses.changed_target_ids",
                    expected=(f"section ids drawn only from {sorted(contract.target_section_ids)}"),
                    example=sorted(contract.target_section_ids),
                    received=sorted(changed_targets),
                )
            expected_sections = set(contract.target_section_ids)
            if (
                payload.base_subject_ref != contract.subject_ref
                or payload.revision != contract.revision
                or set(payload.section_bodies) != expected_sections
                or set(payload.section_part_refs) != expected_sections
            ):
                raise SubmissionValidationError(
                    "chief revision patch identity or section scope differs from its input",
                    field="$identity",
                    expected={
                        "base_subject_ref": contract.subject_ref,
                        "revision": contract.revision,
                        "section_ids": sorted(expected_sections),
                    },
                    example={
                        "base_subject_ref": contract.subject_ref,
                        "revision": contract.revision,
                        "section_ids": sorted(expected_sections),
                    },
                    received={
                        "base_subject_ref": payload.base_subject_ref,
                        "revision": payload.revision,
                        "section_ids": sorted(payload.section_bodies),
                    },
                )
            expected_root = (
                Path("Work/runs") / self.run_id / "drafts" / self.task_id / f"r{self.revision}"
            )
            for section_id, ref in payload.section_part_refs.items():
                if Path(ref) != (expected_root / f"{CHIEF_SECTION_RESULT_PART_IDS[section_id]}.md"):
                    raise SubmissionValidationError(
                        "chief revision uses a result part outside the active task",
                        field=f"section_part_refs.{section_id}",
                        expected=str(
                            expected_root / f"{CHIEF_SECTION_RESULT_PART_IDS[section_id]}.md"
                        ),
                        received=ref,
                    )
            return
        if isinstance(contract, (ChiefEditorInput, AggregateEditorInput)):
            if not isinstance(payload, EditedReportSubmission):
                return
            markers = contract.approved_module_markers
            for module_id, marker in markers.items():
                if payload.module_narratives[module_id].count(marker) != 1:
                    raise SubmissionValidationError(
                        f"module_narratives.{module_id} must contain its approved marker once",
                        field=f"module_narratives.{module_id}",
                        expected=f"exactly one {marker}",
                        example=marker,
                        received=payload.module_narratives[module_id],
                    )
                if "[[CLAIM:" in payload.module_narratives[module_id]:
                    raise SubmissionValidationError(
                        "chief editor must not emit Claim markers outside the approved module marker",
                        field=f"module_narratives.{module_id}",
                        expected=f"one {marker} and no direct [[CLAIM:C-*]] tokens",
                        example=marker,
                    )
            source_modules = (
                contract.modules
                if isinstance(contract, ChiefEditorInput)
                else contract.structured_modules
            )
            if source_modules:
                ledger_path = self.store.workspace / f"Work/runs/{self.run_id}/ledgers/claims.json"
                expected_claim_ids = (
                    {
                        claim.id
                        for claim in ClaimLedger.model_validate_json(
                            ledger_path.read_text(encoding="utf-8")
                        ).claims
                    }
                    if ledger_path.is_file()
                    else set()
                )
                self._require_exact_ids(
                    set(payload.protected_claim_ids),
                    expected_claim_ids,
                    "chief editor protected_claim_ids",
                    field="protected_claim_ids",
                )
            if (
                isinstance(contract, AggregateEditorInput)
                and contract.source_format == "markdown"
                and (payload.protected_claim_ids or payload.tables or payload.photo_ids)
            ):
                raise SubmissionValidationError(
                    "markdown aggregate input cannot produce unverified Claim, table, "
                    "or photo bindings",
                    field="$bindings",
                    expected=(
                        "protected_claim_ids=[], tables=[], and photo_ids=[] for "
                        "markdown-only aggregate input"
                    ),
                    example={
                        "protected_claim_ids": [],
                        "tables": [],
                        "photo_ids": [],
                    },
                    received={
                        "protected_claim_ids": payload.protected_claim_ids,
                        "tables": [table.model_dump(mode="json") for table in payload.tables],
                        "photo_ids": payload.photo_ids,
                    },
                )
            return
        if isinstance(contract, WorkflowExceptionInput):
            if isinstance(payload, WorkflowDecisionSubmission):
                self._require_exact_ids(
                    set(payload.finding_ids),
                    set(contract.finding_ids),
                    "workflow exception decision",
                    field="finding_ids",
                )

    async def __call__(self, payload: dict | str) -> dict:
        """Return structured correction feedback before bounded terminal failure."""

        try:
            return await self._submit_once(payload)
        except (ValidationError, SubmissionValidationError) as exc:
            self._validation_failures += 1
            issues = self._validation_issues(exc, payload)
            fingerprint = self._correction_fingerprint(exc, issues)
            count = self._validation_fingerprints.get(fingerprint, 0) + 1
            self._validation_fingerprints[fingerprint] = count
            affected_part_ids = sorted(
                {
                    str(issue["field"]).split(".", 1)[1]
                    for issue in issues
                    if str(issue.get("field", "")).startswith("submodule_narratives.")
                }
            )
            correction_ref = (
                f"Work/runs/{self.run_id}/submissions/{self.task_id}/correction-state.json"
            )
            terminal = count >= 2 or (self._validation_failures >= self.max_validation_failures)
            self.store.write_json(
                correction_ref,
                {
                    "kind": "submission_correction_state",
                    "run_id": self.run_id,
                    "task_id": self.task_id,
                    "revision": self.revision,
                    "status": "failed" if terminal else "correction_required",
                    "raw_payload": payload,
                    "validation_errors": issues,
                    "affected_part_ids": affected_part_ids,
                    "validation_failures": self._validation_failures,
                    "fingerprint_occurrences": count,
                },
            )
            if not terminal:
                return {
                    "status": "correction_required",
                    "validation_errors": issues,
                    "remaining_attempts": (
                        self.max_validation_failures - self._validation_failures
                    ),
                    "same_error_attempts_remaining": 1,
                    "attempt": self._submission_attempt,
                    "correction_state_ref": correction_ref,
                }
            if count >= 2:
                stop_cause = "the same validation defect was repeated"
            else:
                stop_cause = "the distinct validation-correction budget was exhausted"
            reason = (
                f"structured submission contract failed: {stop_cause}; the workflow "
                "stopped instead of guessing or repairing model output. "
                f"attempts={self._validation_failures}; fingerprint_occurrences={count}; "
                f"error={exc}"
            )
            result = AgentResult(
                task_id=self.task_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
                session_id=self.session_id,
                status=AgentRunStatus.FAILED,
                reason=reason,
            )
            relative = await self._persist_and_publish(result)
            return {
                "status": "failed",
                "error": reason,
                "validation_errors": issues,
                "remaining_attempts": 0,
                "result_path": relative,
                "correction_state_ref": correction_ref,
            }

    @staticmethod
    def _runtime_finding_ids(
        findings: object,
        *,
        prefix: str,
        existing_ids: list[str],
    ) -> list[dict]:
        if not isinstance(findings, list):
            return findings
        used_numbers = []
        for finding_id in existing_ids:
            suffix = finding_id.removeprefix(prefix)
            if finding_id.startswith(prefix) and suffix.isdigit():
                used_numbers.append(int(suffix))
        next_number = max(used_numbers, default=0) + 1
        normalized: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict):
                normalized.append(finding)
                continue
            item = dict(finding)
            item["id"] = f"{prefix}{next_number:03d}"
            next_number += 1
            normalized.append(item)
        return normalized

    def _normalize_runtime_review_fields(self, payload: dict) -> dict:
        """Fill workflow identities that the reviewer should never copy or invent."""

        normalized = deepcopy(payload)
        kind = str(normalized.get("kind", ""))
        contract = self._feedback_input_contract()
        if isinstance(contract, ModuleReviewInput):
            normalized["coverage"] = {"submodule_ids": list(contract.required_submodule_ids)}
            existing_ids = [finding.id for finding in contract.required_findings]
            prefix = contract.finding_id_prefix
            if kind == "module_review_finding_submission":
                normalized["findings"] = self._runtime_finding_ids(
                    normalized.get("findings", []),
                    prefix=prefix,
                    existing_ids=[],
                )
            elif kind == "module_review_verdict_submission":
                verdicts = normalized.get("verdicts")
                if isinstance(verdicts, list) and len(verdicts) == len(existing_ids):
                    normalized["verdicts"] = [
                        {**dict(verdict), "finding_id": finding_id}
                        if isinstance(verdict, dict)
                        else verdict
                        for verdict, finding_id in zip(verdicts, existing_ids, strict=True)
                    ]
                normalized["new_findings"] = self._runtime_finding_ids(
                    normalized.get("new_findings", []),
                    prefix=prefix,
                    existing_ids=existing_ids,
                )
        elif isinstance(contract, CrossReviewInput):
            normalized["coverage"] = [
                {
                    "module_id": module_id,
                    "checked_dimensions": list(CROSS_REVIEW_DIMENSIONS),
                }
                for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
            ]
            existing_ids = [finding.id for finding in contract.required_findings]
            if kind == "cross_review_finding_submission":
                normalized["findings"] = self._runtime_finding_ids(
                    normalized.get("findings", []),
                    prefix="XMR-",
                    existing_ids=[],
                )
            elif kind == "cross_review_verdict_submission":
                verdicts = normalized.get("verdicts")
                if isinstance(verdicts, list) and len(verdicts) == len(existing_ids):
                    normalized["verdicts"] = [
                        {**dict(verdict), "finding_id": finding_id}
                        if isinstance(verdict, dict)
                        else verdict
                        for verdict, finding_id in zip(verdicts, existing_ids, strict=True)
                    ]
                normalized["new_findings"] = self._runtime_finding_ids(
                    normalized.get("new_findings", []),
                    prefix="XMR-",
                    existing_ids=existing_ids,
                )
        elif isinstance(contract, FinalReviewInput):
            normalized["checked_section_ids"] = list(contract.required_section_ids)
            existing_ids = [finding.id for finding in contract.required_findings]
            if kind == "final_review_finding_submission":
                normalized["findings"] = self._runtime_finding_ids(
                    normalized.get("findings", []),
                    prefix="F-",
                    existing_ids=[],
                )
            elif kind == "final_review_verdict_submission":
                verdicts = normalized.get("verdicts")
                if isinstance(verdicts, list) and len(verdicts) == len(existing_ids):
                    normalized["verdicts"] = [
                        {**dict(verdict), "finding_id": finding_id}
                        if isinstance(verdict, dict)
                        else verdict
                        for verdict, finding_id in zip(verdicts, existing_ids, strict=True)
                    ]
                normalized["new_findings"] = self._runtime_finding_ids(
                    normalized.get("new_findings", []),
                    prefix="F-",
                    existing_ids=existing_ids,
                )
        return normalized

    async def _submit_once(self, payload: dict | str) -> dict:
        """Submit a typed result.

        Args:
            payload: One allowed typed workflow submission for the active task.
        """
        self._submission_attempt += 1
        self.store.write_json(
            (
                f"Work/runs/{self.run_id}/submissions/{self.task_id}/"
                f"attempt-{self._submission_attempt}-raw.json"
            ),
            {
                "agent_id": self.agent_id,
                "task_id": self.task_id,
                "revision": self.revision,
                "raw_payload": payload,
            },
        )
        if not isinstance(payload, dict):
            raise SubmissionValidationError(
                "submit_result payload must be the JSON object required by the tool schema",
                field="payload",
                expected="a native JSON object, not a JSON-encoded string",
                received=payload,
                repair_instruction=(
                    "Resubmit the same complete candidate as a native object in payload. "
                    "Do not quote or JSON-stringify the complete object. Preserve the "
                    "candidate content and escape quotation marks only inside individual "
                    "text fields."
                ),
            )
        normalized_payload = self._normalize_runtime_review_fields(payload)
        submission_kind = str(normalized_payload.get("kind", ""))
        if self.allowed_outputs and submission_kind not in self.allowed_outputs:
            raise SubmissionValidationError(
                f"submission kind '{submission_kind or '<missing>'}' is not an allowed output; "
                f"expected one of {sorted(self.allowed_outputs)}",
                field="kind",
                expected=f"one of {sorted(self.allowed_outputs)}",
                example=(
                    sorted(self.allowed_outputs)[0]
                    if len(self.allowed_outputs) == 1
                    else sorted(self.allowed_outputs)
                ),
                received=submission_kind or None,
                repair_instruction=(
                    "Set payload.kind exactly to one allowed output kind and resubmit "
                    "the complete native JSON object. Do not rename the contract or "
                    "stringify the payload."
                ),
            )
        if submission_kind == "module_submission":
            commit = ModuleSubmissionInput.model_validate(normalized_payload)
            normalized_payload = self._assemble_module_commit(commit).model_dump(mode="python")
        elif submission_kind == "module_revision_submission":
            commit = ModuleRevisionSubmissionInput.model_validate(normalized_payload)
            normalized_payload = self._assemble_module_revision_commit(commit).model_dump(
                mode="python"
            )
        elif submission_kind == "chief_revision_submission":
            commit = ChiefRevisionSubmissionInput.model_validate(normalized_payload)
            normalized_payload = self._assemble_chief_revision_commit(commit).model_dump(
                mode="python"
            )
        elif submission_kind == "edited_report_submission":
            editor_input = EditedReportSubmissionInput.model_validate(normalized_payload)
            materialized = self._materialize_text_artifacts(editor_input.model_dump(mode="python"))
            normalized_payload = self._assemble_edited_report(materialized).model_dump(
                mode="python"
            )
        else:
            normalized_payload = self._materialize_text_artifacts(normalized_payload)
        result = AgentResult(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            status=AgentRunStatus.COMPLETED,
            payload=normalized_payload,
        )
        self._validate_against_input_contract(result.payload)
        if isinstance(result.payload, ModuleSubmission):
            sources = SourceLedger(self.store.workspace, self.run_id).records
            known_source_ids = {source.id for source in sources}
            unknown_declared = sorted(set(result.payload.source_ids) - known_source_ids)
            if unknown_declared:
                raise SubmissionValidationError(
                    "module_submission source_ids contain unregistered sources: "
                    f"{unknown_declared}; use only ids returned by the current run's "
                    "evidence/reference tools",
                    field="source_ids",
                    expected=(
                        "only source ids registered by the current run's evidence/reference tools"
                    ),
                    example=sorted(
                        source_id
                        for source_id in result.payload.source_ids
                        if source_id in known_source_ids
                    ),
                    received=result.payload.source_ids,
                    repair_instruction=(
                        "Remove invented or stale source ids. Keep only ids returned by "
                        "current-run evidence tools, update affected Claim source_ids "
                        "consistently, and resubmit the complete payload."
                    ),
                )
            try:
                ClaimLedger(claims=result.payload.claims, sources=sources)
            except ValueError as exc:
                raise SubmissionValidationError(
                    f"module_submission ClaimLedger validation failed before persistence: {exc}",
                    field="claims",
                    expected=(
                        "Claims with unique ids, registered declared sources, correct "
                        "module/submodule ownership, and exactly one required marker in "
                        "the matching narrative"
                    ),
                    received=[claim.model_dump(mode="json") for claim in result.payload.claims],
                    repair_instruction=(
                        "Correct the Claim or its matching narrative marker exactly as "
                        "reported by ClaimLedger. Do not weaken, invent, or silently drop "
                        "unrelated Claims; then resubmit the complete payload."
                    ),
                ) from exc
        if isinstance(result.payload, ModuleRevisionSubmission):
            sources = SourceLedger(self.store.workspace, self.run_id).records
            known_source_ids = {source.id for source in sources}
            unknown_declared = sorted(set(result.payload.source_ids) - known_source_ids)
            if unknown_declared:
                raise SubmissionValidationError(
                    "module_revision_submission source_ids contain unregistered "
                    f"sources: {unknown_declared}",
                    field="source_ids",
                    expected="only source ids registered in the current run",
                    example=sorted(
                        source_id
                        for source_id in result.payload.source_ids
                        if source_id in known_source_ids
                    ),
                    received=result.payload.source_ids,
                )
            missing_claim_sources = sorted(
                {
                    source_id
                    for claim in result.payload.claims_upsert
                    for source_id in claim.source_ids
                }
                - set(result.payload.source_ids)
            )
            if missing_claim_sources:
                raise SubmissionValidationError(
                    "module_revision_submission source_ids must include every upserted "
                    f"Claim source: {missing_claim_sources}",
                    field="source_ids",
                    expected=("a list including every source_id referenced by claims_upsert"),
                    example=sorted(
                        {
                            source_id
                            for claim in result.payload.claims_upsert
                            for source_id in claim.source_ids
                        }
                    ),
                    received=result.payload.source_ids,
                )
        relative = await self._persist_and_publish(result)
        self.store.write_json(
            (f"Work/runs/{self.run_id}/submissions/{self.task_id}/correction-state.json"),
            {
                "kind": "submission_correction_state",
                "run_id": self.run_id,
                "task_id": self.task_id,
                "revision": self.revision,
                "status": "resolved",
                "raw_payload": payload,
                "validation_errors": [],
                "affected_part_ids": [],
                "validation_failures": self._validation_failures,
                "fingerprint_occurrences": 0,
            },
        )
        return {"status": "completed", "result_path": relative}


class _ResultPartTool(Tool):
    def __init__(
        self,
        run_id: str,
        task_id: str,
        revision: int,
        store: ReportingStore,
        expected_part_ids: list[str] | None = None,
        evidence_binding_required: bool = False,
        required_synthesis_input_ids: list[str] | None = None,
        required_synthesis_table_types: list[str] | None = None,
    ):
        if Path(run_id).name != run_id or not run_id:
            raise ValueError("run_id must be a single safe path component")
        if Path(task_id).name != task_id or not task_id:
            raise ValueError("task_id must be a single safe path component")
        if revision < 0:
            raise ValueError("revision must be non-negative")
        self.run_id = run_id
        self.task_id = task_id
        self.revision = revision
        self.store = store
        self.expected_part_ids = tuple(dict.fromkeys(expected_part_ids or ()))
        self.evidence_binding_required = evidence_binding_required
        self.required_synthesis_input_ids = tuple(dict.fromkeys(required_synthesis_input_ids or ()))
        self.required_synthesis_table_types = tuple(
            dict.fromkeys(required_synthesis_table_types or ())
        )

    @property
    def relative_root(self) -> Path:
        return Path("Work/runs") / self.run_id / "drafts" / self.task_id / f"r{self.revision}"


class WriteResultPartTool(_ResultPartTool):
    name = "write_result_part"
    description = (
        "Persist one durable report prose part before final submission. "
        "The returned artifact_ref can replace a long text value in submit_result."
    )

    async def __call__(
        self,
        part_id: str,
        content: str,
        evidence_ids: list[str] | None = None,
    ) -> dict:
        """Write one resumable prose part.

        Args:
            part_id: Stable identifier such as 2.4.1, overview, or conclusion.
            content: One non-empty prose part of at most 48000 characters.
            evidence_ids: For module prose, current-run E-* ids supporting this whole fixed submodule; use [] to record an explicit evidence gap.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", part_id or ""):
            raise ValueError("part_id may contain only letters, digits, dot, underscore, and dash")
        if self.expected_part_ids and part_id not in self.expected_part_ids:
            raise ValueError(
                f"part_id must be one of the fixed task parts: {list(self.expected_part_ids)}"
            )
        if not content.strip() or len(content) > 48_000:
            raise ValueError("result part must contain 1-48000 characters")
        if self.evidence_binding_required:
            if evidence_ids is None:
                raise ValueError(
                    "module result part requires evidence_ids; pass registered E-* ids "
                    "or [] for an explicit evidence gap"
                )
            if (
                not isinstance(evidence_ids, list)
                or not all(
                    isinstance(source_id, str) and source_id.startswith("E-")
                    for source_id in evidence_ids
                )
                or len(evidence_ids) != len(set(evidence_ids))
            ):
                raise ValueError("evidence_ids must be a unique list containing only E-* ids")
            known_evidence = {
                source.id
                for source in SourceLedger(self.store.workspace, self.run_id).records
                if source.id.startswith("E-")
            }
            unknown = sorted(set(evidence_ids) - known_evidence)
            if unknown:
                raise ValueError(f"evidence_ids contain unregistered current-run ids: {unknown}")
            if "[[CLAIM:" in content:
                raise ValueError(
                    "module prose must not contain [[CLAIM:...]] markers; pass E-* ids "
                    "through evidence_ids and let the runtime bind citations"
                )
        elif evidence_ids is not None:
            raise ValueError("evidence_ids is only valid for module result parts")
        relative = self.relative_root / f"{part_id}.md"
        target = self.store.workspace / relative
        previous = target.read_text(encoding="utf-8") if target.is_file() else None
        self.store.write_text(relative.as_posix(), content)
        if self.evidence_binding_required:
            self.store.write_json(
                (self.relative_root / "_evidence" / f"{part_id}.json").as_posix(),
                {
                    "kind": "module_part_evidence_binding",
                    "run_id": self.run_id,
                    "task_id": self.task_id,
                    "revision": self.revision,
                    "part_id": part_id,
                    "evidence_ids": evidence_ids,
                },
            )
        return {
            "status": "unchanged"
            if previous == content
            else ("updated" if previous else "created"),
            "part_id": part_id,
            "characters": len(content),
            **(
                {"evidence_ids": evidence_ids}
                if self.evidence_binding_required
                else {"artifact_ref": relative.as_posix()}
            ),
        }


class ListResultPartsTool(_ResultPartTool):
    name = "list_result_parts"
    description = (
        "List durable prose parts and any required Cross synthesis ids/table types "
        "for the active task revision."
    )

    async def __call__(self) -> dict:
        """List saved prose parts and structured synthesis requirements for continuation."""
        root = self.store.workspace / self.relative_root
        parts = []
        if root.is_dir():
            for path in sorted(root.glob("*.md")):
                part = {
                    "part_id": path.stem,
                    "characters": len(path.read_text(encoding="utf-8")),
                }
                if self.evidence_binding_required:
                    binding_path = root / "_evidence" / f"{path.stem}.json"
                    evidence_ids = None
                    if binding_path.is_file():
                        try:
                            binding = json.loads(binding_path.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            binding = None
                        if isinstance(binding, dict):
                            evidence_ids = binding.get("evidence_ids")
                    part.update(
                        {
                            "evidence_ids": evidence_ids,
                            "ready": isinstance(evidence_ids, list),
                        }
                    )
                else:
                    part["artifact_ref"] = path.relative_to(self.store.workspace).as_posix()
                parts.append(part)
        saved_ids = [part["part_id"] for part in parts]
        missing_ids = [part_id for part_id in self.expected_part_ids if part_id not in saved_ids]
        unbound_ids = (
            [part["part_id"] for part in parts if not part.get("ready", False)]
            if self.evidence_binding_required
            else []
        )
        return {
            "revision": self.revision,
            "parts": parts,
            "expected_part_ids": list(self.expected_part_ids),
            "missing_part_ids": missing_ids,
            "unbound_part_ids": unbound_ids,
            "required_synthesis_input_ids": list(self.required_synthesis_input_ids),
            "required_synthesis_table_types": list(self.required_synthesis_table_types),
            "complete": (bool(self.expected_part_ids) and not missing_ids and not unbound_ids),
        }


class SubmissionValidationError(ValueError):
    """A deterministic semantic validation failure for a submitted payload."""

    submission_validation = True

    def __init__(
        self,
        problem: str,
        *,
        field: str = "$",
        expected: str = "value matching the active submission contract",
        example: object | None = None,
        received: object | None = None,
        repair_instruction: str = (
            "Correct the reported field to match the active contract and resubmit the "
            "complete payload as a native JSON object. Preserve unrelated valid content; "
            "do not stringify the payload."
        ),
    ):
        super().__init__(problem)
        self.field = field
        self.expected = expected
        self.example = example
        self.received = received
        self.repair_instruction = repair_instruction


class ReportBlockedTool(_ResultTool):
    name = "report_blocked"
    description = "Persist a terminal blocked result with the concrete missing input or decision."

    async def __call__(
        self,
        reason: str,
        artifact_refs: list[str] | None = None,
        source_ids: list[str] | None = None,
    ) -> dict:
        """Report a blocked task.

        Args:
            reason: Concrete reason reliable completion is impossible.
            artifact_refs: Existing file artifacts that demonstrate or contextualize the block.
            source_ids: E-*, R-*, or W-* ledger records supporting the block.
        """
        supplied_refs = artifact_refs or []
        artifact_paths = artifact_path_refs(supplied_refs)
        supporting_source_ids = list(
            dict.fromkeys([*(source_ids or []), *source_record_ids(supplied_refs)])
        )
        result = AgentResult(
            task_id=self.task_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            status=AgentRunStatus.BLOCKED,
            reason=reason,
        )
        relative = await self._persist_and_publish(result)
        await self.bus.publish(
            BlockedNoticeMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                sender=self.agent_id,
                recipient="workflow",
                artifact_refs=[relative, *artifact_paths],
                source_ids=supporting_source_ids,
                content=reason,
                reason=reason,
                priority="high",
            )
        )
        return {"status": "blocked", "result_path": relative}


class QueryPeerTool(Tool):
    name = "query_peer"
    description = (
        "Ask one named peer a focused question, passing artifact references instead of "
        "full evidence or drafts, and wait for the matching session-scoped reply."
    )

    def __init__(
        self,
        bus: MessageBus,
        task_id: str,
        agent_id: str,
        session_id: str,
        workflow_id: str = "",
        timeout: float = 120.0,
    ):
        self.bus = bus
        self.task_id = task_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.workflow_id = workflow_id
        self.timeout = timeout

    async def __call__(
        self,
        target_agent: str,
        question: str,
        artifact_refs: list[str] | None = None,
        target_session_id: str | None = None,
    ) -> dict:
        """Query a peer.

        Args:
            target_agent: Registry id of the responsible peer.
            question: Focused question the peer can answer independently.
            artifact_refs: Shared project artifacts relevant to the question.
        """
        query_id = f"Q-{uuid4().hex[:12]}"
        waiter = asyncio.create_task(
            self.bus.wait_for(
                PeerReplyMessage,
                lambda message: (
                    message.workflow_id == self.workflow_id
                    and message.task_id == self.task_id
                    and message.query_id == query_id
                    and message.recipient == self.agent_id
                    and message.target_session_id == self.session_id
                ),
                timeout=self.timeout,
            )
        )
        # Let wait_for install its one-shot subscriber before the query is queued.
        await asyncio.sleep(0)
        await self.bus.publish(
            PeerQueryMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                query_id=query_id,
                sender=self.agent_id,
                recipient=target_agent,
                source_session_id=self.session_id,
                target_session_id=target_session_id,
                question=question,
                artifact_refs=artifact_refs or [],
                content=question,
            )
        )
        reply = await waiter
        return {
            "query_id": query_id,
            "status": "replied",
            "answer": reply.answer,
            "source_ids": reply.source_ids,
            "artifact_refs": reply.artifact_refs,
        }


class ReplyPeerTool(Tool):
    name = "reply_peer"
    description = "Reply to an existing peer query and identify any supporting sources."

    def __init__(self, bus: MessageBus, agent_id: str, workflow_id: str = ""):
        self.bus = bus
        self.agent_id = agent_id
        self.workflow_id = workflow_id

    async def __call__(
        self,
        task_id: str,
        query_id: str,
        target_agent: str,
        target_session_id: str,
        answer: str,
        source_ids: list[str] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> dict:
        """Reply to a peer.

        Args:
            task_id: Task id from the peer query.
            query_id: Query id from the peer query.
            target_agent: Agent that issued the query.
            target_session_id: Session id from the peer query.
            answer: Focused answer to the question.
            source_ids: E-*, R-*, or W-* records supporting the answer.
            artifact_refs: Shared artifacts containing longer supporting material.
        """
        await self.bus.publish(
            PeerReplyMessage(
                workflow_id=self.workflow_id,
                task_id=task_id,
                query_id=query_id,
                sender=self.agent_id,
                recipient=target_agent,
                target_session_id=target_session_id,
                answer=answer,
                source_ids=source_ids or [],
                artifact_refs=artifact_refs or [],
                content=answer,
            )
        )
        return {"query_id": query_id, "status": "replied"}


class ReportGapTool(Tool):
    name = "report_gap"
    description = (
        "Persist a non-terminal evidence or context gap so the workflow can request input "
        "or preserve a verification boundary while the agent continues useful work."
    )

    def __init__(
        self,
        agent_id: str,
        run_id: str,
        task_id: str,
        store: ReportingStore,
        bus: MessageBus,
        workflow_id: str = "",
    ):
        if Path(task_id).name != task_id or not task_id:
            raise ValueError("task_id must be a single safe path component")
        if Path(run_id).name != run_id or not run_id:
            raise ValueError("run_id must be a single safe path component")
        self.agent_id = agent_id
        self.run_id = run_id
        self.task_id = task_id
        self.store = store
        self.bus = bus
        self.workflow_id = workflow_id

    async def __call__(
        self,
        gap: str,
        impact: str,
        requested_input: str = "",
        artifact_refs: list[str] | None = None,
        source_ids: list[str] | None = None,
    ) -> dict:
        """Report a non-terminal gap.

        Args:
            gap: Missing customer fact, reference, or decision.
            impact: How the gap limits the current analysis.
            requested_input: Specific material or confirmation that would resolve it.
            artifact_refs: Existing file artifacts demonstrating the gap.
            source_ids: E-*, R-*, or W-* ledger records demonstrating the gap.
        """
        supplied_refs = artifact_refs or []
        artifact_paths = artifact_path_refs(supplied_refs)
        supporting_source_ids = list(
            dict.fromkeys([*(source_ids or []), *source_record_ids(supplied_refs)])
        )
        gap_id = f"GAP-{uuid4().hex[:12]}"
        relative = f"Work/runs/{self.run_id}/gaps/{gap_id}.json"
        self.store.write_json(
            relative,
            {
                "id": gap_id,
                "workflow_id": self.workflow_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "gap": gap,
                "impact": impact,
                "requested_input": requested_input,
                "artifact_refs": artifact_paths,
                "source_ids": supporting_source_ids,
            },
        )
        await self.bus.publish(
            ProgressNoteMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                sender=self.agent_id,
                recipient="workflow",
                note_kind="gap",
                artifact_refs=[relative, *artifact_paths],
                source_ids=supporting_source_ids,
                content=f"{gap}\nImpact: {impact}\nRequested input: {requested_input}",
                priority="high",
            )
        )
        return {"status": "reported", "gap_id": gap_id, "artifact_ref": relative}


class PeerMessageRouter:
    """Deliver typed peer queries to the target AgentLoop's existing input channel."""

    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.bus.subscribe(PeerQueryMessage, self._route_query)

    async def _route_query(self, message: PeerQueryMessage) -> None:
        artifacts = "".join(
            f"<artifact_ref>{escape(ref)}</artifact_ref>" for ref in message.artifact_refs
        )
        await self.bus.publish(
            UserMessage(
                agent_type=message.recipient,
                source=message.sender,
                message_id=message.query_id,
                summary=message.question,
                content=(
                    "<peer_query>"
                    f"<workflow_id>{escape(message.workflow_id)}</workflow_id>"
                    f"<task_id>{escape(message.task_id)}</task_id>"
                    f"<query_id>{escape(message.query_id)}</query_id>"
                    f"<source_agent>{escape(message.sender)}</source_agent>"
                    f"<source_session_id>{escape(message.source_session_id)}</source_session_id>"
                    f"<question>{escape(message.question)}</question>"
                    f"{artifacts}</peer_query>"
                ),
            )
        )

    def close(self) -> None:
        self.bus.unsubscribe(PeerQueryMessage, self._route_query)
