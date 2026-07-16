"""Prompt assembly boundaries for static Agent identity and dynamic task context."""

from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from .agentic_models import TaskEnvelope
from .config import AgentDefinition
from .module_skills import ModuleSkill


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
            f"{instructions}\n"
            f"{skills_section}"
            f"{index_section}"
            "</agent_identity>"
        )
        return _validated_xml(prompt, label="system prompt")

    @staticmethod
    def task_message(envelope: TaskEnvelope, shared_artifacts: list[str]) -> str:
        inputs = "\n".join(f"<input_ref>{escape(ref)}</input_ref>" for ref in envelope.input_refs)
        artifacts = "\n".join(
            f"<shared_artifact>{escape(ref)}</shared_artifact>" for ref in shared_artifacts
        )
        constraints = "\n".join(
            f"<constraint>{escape(value)}</constraint>" for value in envelope.constraints
        )
        allowed_outputs = "\n".join(
            f"<allowed_output>{escape(value)}</allowed_output>"
            for value in envelope.allowed_outputs
        )
        prior_result = (
            f"<prior_result_ref>{escape(envelope.prior_result_ref)}</prior_result_ref>"
            if envelope.prior_result_ref is not None
            else ""
        )
        issues = "\n".join(f"<issue_ref>{escape(ref)}</issue_ref>" for ref in envelope.issue_refs)
        summaries = "\n".join(
            f'<context_summary_ref context_only="true">{escape(ref)}</context_summary_ref>'
            for ref in envelope.context_summary_refs
        )
        message = (
            "<task_context>\n"
            f"<task_id>{escape(envelope.task_id)}</task_id>\n"
            f"<run_id>{escape(envelope.run_id)}</run_id>\n"
            f"<agent_id>{escape(envelope.agent_id)}</agent_id>\n"
            f"<revision>{envelope.revision}</revision>\n"
            f"<objective>{escape(envelope.objective)}</objective>\n"
            f"{inputs}\n{artifacts}\n{constraints}\n{allowed_outputs}\n"
            f"{prior_result}\n{issues}\n{summaries}\n"
            "</task_context>"
        )
        return _validated_xml(message, label="task context")
