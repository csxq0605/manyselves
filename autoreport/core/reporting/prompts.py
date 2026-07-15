"""Prompt assembly boundaries for static Agent identity and dynamic task context."""

from xml.sax.saxutils import escape

from .agentic_models import TaskEnvelope
from .config import AgentDefinition


class PromptAssembler:
    """Assemble identity and task prompts without mixing their lifecycles."""

    @staticmethod
    def system_prompt(definition: AgentDefinition) -> str:
        return (
            f'<agent_identity name="{escape(definition.name)}">\n'
            f"<description>{escape(definition.description)}</description>\n"
            f"{definition.instructions.strip()}\n"
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
        issues = "\n".join(
            f"<issue_ref>{escape(ref)}</issue_ref>" for ref in envelope.issue_refs
        )
        return (
            "<task_context>\n"
            f"<task_id>{escape(envelope.task_id)}</task_id>\n"
            f"<run_id>{escape(envelope.run_id)}</run_id>\n"
            f"<revision>{envelope.revision}</revision>\n"
            f"<objective>{escape(envelope.objective)}</objective>\n"
            f"{inputs}\n{artifacts}\n{constraints}\n{issues}\n"
            "</task_context>"
        )
