"""Prompt assembly boundaries for static Agent identity and dynamic task context."""

from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from .agentic_models import TaskEnvelope
from .config import AgentDefinition


class PromptAssembler:
    """Assemble identity and task prompts without mixing their lifecycles."""

    @staticmethod
    def system_prompt(definition: AgentDefinition) -> str:
        instructions = definition.instructions.strip()
        try:
            ElementTree.fromstring(
                f"<identity_instructions>{instructions}</identity_instructions>"
            )
        except ElementTree.ParseError as exc:
            raise ValueError(
                f"{definition.source_path}: instructions must be a valid XML fragment"
            ) from exc
        return (
            f"<agent_identity name={quoteattr(definition.name)}>\n"
            f"<description>{escape(definition.description)}</description>\n"
            f"{instructions}\n"
            "</agent_identity>"
        )

    @staticmethod
    def task_message(envelope: TaskEnvelope, shared_artifacts: list[str]) -> str:
        inputs = "\n".join(
            f"<input_ref>{escape(ref)}</input_ref>" for ref in envelope.input_refs
        )
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
        issues = "\n".join(
            f"<issue_ref>{escape(ref)}</issue_ref>" for ref in envelope.issue_refs
        )
        return (
            "<task_context>\n"
            f"<task_id>{escape(envelope.task_id)}</task_id>\n"
            f"<run_id>{escape(envelope.run_id)}</run_id>\n"
            f"<agent_id>{escape(envelope.agent_id)}</agent_id>\n"
            f"<revision>{envelope.revision}</revision>\n"
            f"<objective>{escape(envelope.objective)}</objective>\n"
            f"{inputs}\n{artifacts}\n{constraints}\n{allowed_outputs}\n"
            f"{prior_result}\n{issues}\n"
            "</task_context>"
        )
