"""Prompt assembly boundaries for static Agent identity and dynamic task context."""

from typing import Any, Mapping
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from manyselves.capabilities.distribution_reporting.runtime.contracts.submissions import (
    KIND_SEMANTIC_RULES,
    KIND_SUMMARIES,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope

from .config import AgentDefinition
from .module_skills import ModuleSkill

SUBMISSION_CONTRACT_VERSION = 1


def _validated_xml(document: str, *, label: str) -> str:
    try:
        ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{label} must be valid XML: {exc}") from exc
    return document


class PromptAssembler:
    """Assemble identity and task prompts without mixing their lifecycles."""

    @staticmethod
    def semantic_turn(
        reason: str,
        *,
        turn_kind: str = "semantic_continuation",
        next_action: str | None = None,
    ) -> str:
        """Build a small continuation/correction message.

        Follow-up turns intentionally carry only a semantic reason and the
        next action.  The immutable task contract and any prior tool trace are
        supplied by the typed context capsule, never copied into this message.
        """

        kind = str(turn_kind or "semantic_continuation").strip() or "semantic_continuation"
        action = (
            f"<next_action>{escape(str(next_action))}</next_action>"
            if next_action
            else ""
        )
        message = (
            f"<semantic_turn kind={quoteattr(kind)}>"
            f"<reason>{escape(str(reason))}</reason>{action}"
            "</semantic_turn>"
        )
        return _validated_xml(message, label="semantic turn")

    @staticmethod
    def system_prompt(
        definition: AgentDefinition,
        module_skills: list[ModuleSkill] | tuple[ModuleSkill, ...] = (),
        *,
        module_skill_index: str | None = None,
    ) -> str:
        instructions = definition.instructions.strip()
        try:
            ElementTree.fromstring(f"<identity_instructions>{instructions}</identity_instructions>")
        except ElementTree.ParseError as exc:
            raise ValueError(
                f"{definition.source_path}: instructions must be a valid XML fragment"
            ) from exc
        skill_nodes = "\n".join(
            f"<module_skill id={quoteattr(skill.id)} "
            f"version={quoteattr(skill.version)} "
            f"module_id={quoteattr(skill.module_id)} "
            f"title={quoteattr(skill.title)}>{escape(skill.content)}</module_skill>"
            for skill in module_skills
        )
        skills_section = (
            f"<module_skills>\n{skill_nodes}\n</module_skills>\n" if skill_nodes else ""
        )
        index_section = (
            f"<module_skill_index>{escape(module_skill_index)}</module_skill_index>\n"
            if module_skill_index
            else ""
        )
        prompt = (
            f"<agent_identity name={quoteattr(definition.name)}>\n"
            f"<description>{escape(definition.description)}</description>\n"
            f"<execution_profile effort={quoteattr(definition.effort)} "
            f"model={quoteattr(definition.model)}>"
            + (
                "Perform deep, explicit internal comparison of evidence, alternative "
                "explanations, failure propagation, and recommendation tradeoffs before "
                "submitting the typed result."
                if definition.effort == "high"
                else (
                    "Perform a balanced evidence and alternative-explanation review before "
                    "submitting the typed result."
                    if definition.effort == "medium"
                    else "Use a concise evidence check before submitting the typed result."
                )
            )
            + "</execution_profile>\n"
            f"{instructions}\n"
            f"{skills_section}"
            f"{index_section}"
            "</agent_identity>"
        )
        return _validated_xml(prompt, label="system prompt")

    @staticmethod
    def task_message(
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        input_contract_payload: str | None = None,
    ) -> str:
        inputs = "\n".join(
            (
                f"<input_ref delivery_mode="
                f"{quoteattr(envelope.artifact_delivery_modes[ref])}>"
                f"{escape(ref)}</input_ref>"
            )
            for ref in envelope.input_refs
        )
        already_declared = set(envelope.input_refs) | set(envelope.context_summary_refs)
        if envelope.prior_result_ref:
            already_declared.add(envelope.prior_result_ref)
        artifacts = "\n".join(
            f"<shared_artifact>{escape(ref)}</shared_artifact>"
            for ref in dict.fromkeys(shared_artifacts)
            if ref not in already_declared
        )
        constraints = "\n".join(
            f"<constraint>{escape(value)}</constraint>" for value in envelope.constraints
        )
        allowed_outputs = "\n".join(
            f"<allowed_output>{escape(value)}</allowed_output>"
            for value in envelope.allowed_outputs
        )
        submission_contracts = "\n".join(
            (
                f"<submission_contract kind={quoteattr(value)} "
                f'version="{SUBMISSION_CONTRACT_VERSION}" '
                'machine_schema="submit_result.input_schema">'
                f"<purpose>{escape(KIND_SUMMARIES[value])}</purpose>"
                + "".join(
                    f"<semantic_rule>{escape(rule)}</semantic_rule>"
                    for rule in KIND_SEMANTIC_RULES.get(value, ())
                )
                + (
                    "<argument_encoding>The submit_result tool arguments are the "
                    "submission object itself. Put kind and every declared field at "
                    "the top level. Never add a payload wrapper or JSON-encode, quote, "
                    "or stringify the complete object.</argument_encoding>"
                    "<task_example_source>Use the current task-specific example in "
                    "submit_result.input_schema; its module, taxonomy subsection, revision, and request "
                    "identities are authoritative for this task.</task_example_source>"
                    "</submission_contract>"
                )
            )
            for value in envelope.allowed_outputs
        )
        input_contract = (
            (
                f"<input_contract kind={quoteattr(envelope.input_contract_kind)} "
                f"artifact_ref={quoteattr(envelope.input_contract_ref or '')} "
                'delivery_mode="inline">'
                f"{escape(input_contract_payload or '')}"
                "</input_contract>"
            )
            if envelope.input_contract_kind
            else ""
        )
        allowed_tools = "\n".join(
            f"<allowed_tool>{escape(value)}</allowed_tool>"
            for value in envelope.allowed_tools
        )
        prior_result = (
            (
                "<prior_result_ref delivery_mode="
                f"{quoteattr(envelope.artifact_delivery_modes[envelope.prior_result_ref])}>"
                f"{escape(envelope.prior_result_ref)}</prior_result_ref>"
            )
            if envelope.prior_result_ref is not None
            else ""
        )
        summaries = "\n".join(
            (
                '<context_summary_ref context_only="true" delivery_mode='
                f"{quoteattr(envelope.artifact_delivery_modes[ref])}>"
                f"{escape(ref)}</context_summary_ref>"
            )
            for ref in envelope.context_summary_refs
        )
        inline_context = (
            f"<inline_context>{escape(envelope.inline_context)}</inline_context>"
            if envelope.inline_context
            else ""
        )
        target_submodules = "\n".join(
            f"<target_submodule_id>{escape(submodule_id)}</target_submodule_id>"
            for submodule_id in envelope.target_submodule_ids
        )
        message = (
            "<task_context>\n"
            f"<task_id>{escape(envelope.task_id)}</task_id>\n"
            f"<run_id>{escape(envelope.run_id)}</run_id>\n"
            f"<agent_id>{escape(envelope.agent_id)}</agent_id>\n"
            f"<revision>{envelope.revision}</revision>\n"
            f"<objective>{escape(envelope.objective)}</objective>\n"
            f"{inputs}\n{artifacts}\n{constraints}\n{allowed_outputs}\n"
            f"{input_contract}\n{submission_contracts}\n{allowed_tools}\n"
            f"{prior_result}\n{summaries}\n{inline_context}\n"
            f"{target_submodules}\n"
            "</task_context>"
        )
        return _validated_xml(message, label="task context")

    @staticmethod
    def task_delta_message(
        envelope: TaskEnvelope,
        shared_artifacts: list[str],
        *,
        previous: Mapping[str, Any],
        input_contract_payload: str | None = None,
        input_contract_sha256: str | None = None,
    ) -> str:
        """Build a boundary plus exact current-task delta for one identity.

        The first task receives :meth:`task_message` with the complete context.
        Later transitions retain the lossless conversation and send only changed
        task fields/new references. The active input contract is compared by
        its immutable business reference; its body is inlined only for a new
        reference, never persisted or unioned with historical contracts.
        """

        previous_task_id = str(previous.get("task_id") or "")
        previous_revision = previous.get("revision")
        previous_contract_kind = str(previous.get("input_contract_kind") or "")
        previous_contract_ref = str(previous.get("input_contract_ref") or "")
        current_contract_kind = str(envelope.input_contract_kind or "")
        current_contract_ref = str(envelope.input_contract_ref or "")
        contract_unchanged = (
            current_contract_kind == previous_contract_kind
            and current_contract_ref == previous_contract_ref
        )
        changed_fields: list[str] = []
        scalar_fields = (
            "objective",
            "constraints",
            "allowed_outputs",
            "allowed_tools",
            "input_refs",
            "prior_result_ref",
            "context_summary_refs",
            "inline_context",
            "target_submodule_ids",
            "artifact_delivery_modes",
        )
        current = {
            "objective": envelope.objective,
            "constraints": list(envelope.constraints),
            "allowed_outputs": list(envelope.allowed_outputs),
            "allowed_tools": list(envelope.allowed_tools),
            "input_refs": list(envelope.input_refs),
            "prior_result_ref": envelope.prior_result_ref,
            "context_summary_refs": list(envelope.context_summary_refs),
            "inline_context": envelope.inline_context,
            "target_submodule_ids": list(envelope.target_submodule_ids),
            "artifact_delivery_modes": dict(envelope.artifact_delivery_modes),
        }
        for field in scalar_fields:
            if (
                field == "target_submodule_ids"
                and previous.get("attention_scope_ref") == current_contract_ref
            ):
                continue
            if current[field] != previous.get(field):
                changed_fields.append(field)

        previous_refs = set(str(item) for item in previous.get("shared_artifacts", ()) or ())
        new_shared = [
            str(ref)
            for ref in dict.fromkeys(shared_artifacts)
            if str(ref) not in previous_refs
        ]
        refs = "\n".join(
            f"<new_shared_artifact>{escape(ref)}</new_shared_artifact>"
            for ref in new_shared
        )
        constraints = (
            "\n".join(
                f"<constraint>{escape(value)}</constraint>"
                for value in envelope.constraints
            )
            if "constraints" in changed_fields
            else ""
        )
        allowed_outputs = (
            "\n".join(
                f"<allowed_output>{escape(value)}</allowed_output>"
                for value in envelope.allowed_outputs
            )
            if "allowed_outputs" in changed_fields
            else ""
        )
        allowed_tools = (
            "\n".join(
                f"<allowed_tool>{escape(value)}</allowed_tool>"
                for value in envelope.allowed_tools
            )
            if "allowed_tools" in changed_fields
            else ""
        )
        input_refs = (
            "\n".join(
                f"<input_ref delivery_mode={quoteattr(envelope.artifact_delivery_modes.get(ref, 'reference'))}>{escape(ref)}</input_ref>"
                for ref in envelope.input_refs
            )
            if "input_refs" in changed_fields
            else ""
        )
        objective = (
            f"<objective>{escape(envelope.objective)}</objective>"
            if "objective" in changed_fields
            else ""
        )
        contract_mode = envelope.artifact_delivery_modes.get(
            envelope.input_contract_ref or "", "inline"
        )
        if contract_unchanged and current_contract_ref:
            input_contract = (
                f"<input_contract_ref unchanged=\"true\" delivery_mode={quoteattr(contract_mode)}>{escape(envelope.input_contract_ref or '')}</input_contract_ref>"
            )
        elif envelope.input_contract_kind:
            input_contract = (
                f"<input_contract kind={quoteattr(envelope.input_contract_kind)} "
                f"artifact_ref={quoteattr(envelope.input_contract_ref or '')} delivery_mode=\"inline\" "
                f">{escape(input_contract_payload or '')}</input_contract>"
            )
        else:
            input_contract = ""
        prior_result = (
            f"<prior_result_ref delivery_mode={quoteattr(envelope.artifact_delivery_modes.get(envelope.prior_result_ref or '', 'reference'))}>"
            f"{escape(envelope.prior_result_ref)}</prior_result_ref>"
            if envelope.prior_result_ref and "prior_result_ref" in changed_fields
            else ""
        )
        summaries = "\n".join(
            f"<context_summary_ref context_only=\"true\" delivery_mode={quoteattr(envelope.artifact_delivery_modes.get(ref, 'reference'))}>{escape(ref)}</context_summary_ref>"
            for ref in envelope.context_summary_refs
        ) if "context_summary_refs" in changed_fields else ""
        inline_context = (
            f"<inline_context>{escape(envelope.inline_context)}</inline_context>"
            if envelope.inline_context and "inline_context" in changed_fields
            else ""
        )
        target_submodules = "\n".join(
            f"<target_submodule_id>{escape(submodule_id)}</target_submodule_id>"
            for submodule_id in envelope.target_submodule_ids
        ) if "target_submodule_ids" in changed_fields else ""
        message = (
            "<task_boundary>\n"
            f"<previous_task_id>{escape(previous_task_id)}</previous_task_id>\n"
            f"<previous_revision>{escape(str(previous_revision if previous_revision is not None else ''))}</previous_revision>\n"
            f"<task_delta changed_fields={quoteattr(','.join(changed_fields))}>\n"
            f"<task_id>{escape(envelope.task_id)}</task_id>\n"
            f"<run_id>{escape(envelope.run_id)}</run_id>\n"
            f"<agent_id>{escape(envelope.agent_id)}</agent_id>\n"
            f"<revision>{envelope.revision}</revision>\n"
            f"{objective}\n{input_refs}\n{constraints}\n{allowed_outputs}\n{allowed_tools}\n"
            f"{input_contract}\n{prior_result}\n{summaries}\n{inline_context}\n"
            f"{target_submodules}\n{refs}\n"
            "<history_policy>Historical calls are transcript history only; do not validate or replay them with the current task contract.</history_policy>\n"
            "</task_delta>\n"
            "</task_boundary>"
        )
        return _validated_xml(message, label="task boundary")
