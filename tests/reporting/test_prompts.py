from pathlib import Path
from xml.etree import ElementTree

import pytest

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


def test_system_prompt_quotes_identity_name_as_one_xml_attribute(tmp_path: Path) -> None:
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: 'auditor\" injected=\"yes'\ndescription: 审计员\n---\n"
        "<role_and_perspective>独立核验。</role_and_perspective>",
        encoding="utf-8",
    )

    root = ElementTree.fromstring(
        PromptAssembler.system_prompt(load_agent_definition(identity))
    )

    assert root.attrib == {"name": 'auditor" injected="yes'}


def test_system_prompt_rejects_malformed_identity_xml(tmp_path: Path) -> None:
    identity = tmp_path / "agent.md"
    identity.write_text(
        "---\nname: auditor\ndescription: 审计员\n---\n<role>AT&T</role>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="valid XML fragment"):
        PromptAssembler.system_prompt(load_agent_definition(identity))


def test_task_context_is_xml_and_separate_from_system() -> None:
    envelope = TaskEnvelope(
        task_id="t1",
        run_id="r1",
        agent_id="auditor",
        objective="审 2.4 & 核验",
        input_refs=["evidence/<2.4>.json"],
        constraints=["不得编造 & 必须引用"],
        allowed_outputs=["module_submission"],
        prior_result_ref="drafts/2.4-v0.json",
        issue_refs=["issues/2.4-1.json"],
    )

    message = PromptAssembler.task_message(envelope, ["drafts/2.4.json"])

    assert message.startswith("<task_context>")
    root = ElementTree.fromstring(message)
    assert root.findtext("agent_id") == "auditor"
    assert root.findtext("objective") == "审 2.4 & 核验"
    assert root.findtext("input_ref") == "evidence/<2.4>.json"
    assert root.findtext("shared_artifact") == "drafts/2.4.json"
    assert root.findtext("constraint") == "不得编造 & 必须引用"
    assert root.findtext("allowed_output") == "module_submission"
    assert root.findtext("prior_result_ref") == "drafts/2.4-v0.json"
    assert root.findtext("issue_ref") == "issues/2.4-1.json"
