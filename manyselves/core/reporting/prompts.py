"""Prompt assembly boundaries for static Agent identity and dynamic task context."""

from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from .agentic_models import TaskEnvelope
from .config import AgentDefinition
from .module_skills import ModuleSkill
from .submission_contracts import KIND_SEMANTIC_RULES, KIND_SUMMARIES


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
                    "<payload_encoding>Pass payload as a native JSON object. "
                    "Never JSON-encode, quote, or stringify the complete object."
                    "</payload_encoding>"
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
