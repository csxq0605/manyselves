from pathlib import Path

from autoreport.core.reporting.agentic_models import TaskEnvelope
from autoreport.core.reporting.config import load_agent_definition
from autoreport.core.reporting.prompts import PromptAssembler


def test_system_prompt_contains_identity_but_not_runtime_corpus(tmp_path: Path) -> None:
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: auditor\ndescription: 审计员\ntools: [submit_result]\nmaxTurns: 4\n---\n"
        "<role_and_perspective>独立核验。</role_and_perspective>",
        encoding="utf-8",
    )

    prompt = PromptAssembler.system_prompt(load_agent_definition(identity))

    assert "独立核验" in prompt
    assert "01_页面导入知识库" not in prompt
    assert "02_本地skill提示词资料_禁止导入" not in prompt


def test_task_context_is_xml_and_separate_from_system() -> None:
    envelope = TaskEnvelope(
        task_id="t1", run_id="r1", agent_id="auditor", objective="审计 2.4"
    )

    message = PromptAssembler.task_message(envelope, ["drafts/2.4.json"])

    assert message.startswith("<task_context>")
    assert "<objective>审计 2.4</objective>" in message
    assert "<shared_artifact>drafts/2.4.json</shared_artifact>" in message
