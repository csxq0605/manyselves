"""Offline S04 experiment: estimated vs tokenizer-counted Provider request size.

Measures the production ``_estimate_tokens`` heuristic against real BPE
tokenizers on representative request components: Chinese report prose,
fact packets, evidence JSON, Excel table text, Agent prompts and the actual
module-2.4 authoring tool schemas.  No Provider call is made.

Run (tokenizers is an experiment-only dependency, not a project dependency)::

    uv run --with tokenizers python scripts/benchmarks/request_token_estimate.py \
        --tokenizer mimo=/path/mimo_tokenizer.json --tokenizer qwen3=/path/qwen3.json
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import yaml
from loguru import logger

from manyselves.runtime.loops import agent_loop
from manyselves.runtime.providers.base import Message

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves"
CAP = ROOT / "manyselves/capabilities/distribution_reporting"
logger.disable("manyselves")


def _tool_definitions(task_name: str) -> list[dict]:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        build_module_provider_tools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
    from manyselves.runtime.loops.bus import MessageBus

    task = yaml.safe_load((CAP / "tasks" / f"{task_name}.yaml").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp())
    envelope = TaskEnvelope(
        task_id="t", run_id="r", agent_id=task["agent"], revision=0,
        objective="x", allowed_outputs=["module_submission"],
    )

    class _Opaque:  # schema construction only; no artifact is opened
        pass

    registry = build_module_provider_tools(
        tmp, envelope=envelope, module_id="2.4", session_id="s", workflow_id="w",
        bus=MessageBus(), store=ReportingStore(tmp), global_knowledge_root=None,
        tool_names=task["tools"], expected_part_ids=["2.4.1.1"],
        dependencies=ModuleProviderDependencies(
            artifact_gateway=_Opaque(), artifact_access=_Opaque(), result_index=_Opaque()
        ),
    )
    return registry.get_definitions()


def _components() -> dict[str, str]:
    read = lambda path: path.read_text(encoding="utf-8")  # noqa: E731
    tools = _tool_definitions("module-2.4-authoring")
    return {
        "report_prose": read(ARCHIVE / "配电安全专家咨询报告.docx.txt")[:60_000],
        "fact_packet": read(ARCHIVE / "fact_packet_replay/Work/runs/replay/context/module-2.4-facts.md"),
        "evidence_json": read(ARCHIVE / "evidence_compact.txt")[:60_000],
        "excel_table": read(ARCHIVE / "S4-6评估总表.txt"),
        "agent_prompts": "\n".join(read(p) for p in sorted((CAP / "agents").glob("*.md"))),
        "tool_schemas": json.dumps(tools, ensure_ascii=False),
        "python_source": read(ROOT / "manyselves/runtime/loops/context_rebase.py"),
    }


def _estimate(text: str) -> int:
    return agent_loop._estimate_tokens([Message(role="user", content=text)]) - 4


def _flat_estimate(text: str) -> int:
    """The pre-S04 heuristic, kept here only as the comparison baseline."""

    return int(len(text) * 0.3)


def _composite_request(components: dict[str, str]) -> tuple[str, str]:
    """A module-author request shaped like the historical largest turns.

    Messages: Agent prompts as system text, the 2.4 fact packet, about 30k
    characters of drafted prose and 60k characters of evidence tool results.
    Tools: the actual 2.4 authoring tool schemas, sent beside the messages.
    """

    messages = "\n".join(
        (
            components["agent_prompts"][:16_000],
            components["fact_packet"],
            components["report_prose"][:30_000],
            components["evidence_json"],
        )
    )
    return messages, components["tool_schemas"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer", action="append", default=[], help="name=tokenizer.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from tokenizers import Tokenizer

    tokenizers = {
        name: Tokenizer.from_file(path)
        for name, path in (item.split("=", 1) for item in args.tokenizer)
    }
    def count(text: str) -> int:
        return max(len(tok.encode(text, add_special_tokens=False).ids) for tok in tokenizers.values())

    components = _components()
    rows = []
    for kind, text in components.items():
        real = count(text)
        flat, current = _flat_estimate(text), _estimate(text)
        rows.append({
            "component": kind,
            "chars": len(text),
            "tokenizer_tokens_max": real,
            "flat_0_3_estimate": flat,
            "current_estimate": current,
            "flat_ratio": round(flat / real, 2),
            "current_ratio": round(current / real, 2),
        })
    messages, tools = _composite_request(components)
    real_messages, real_tools = count(messages), count(tools)
    window = 128_000
    composite = {
        "description": "2.4 author request shaped like the largest historical turns",
        "real_message_tokens": real_messages,
        "real_tool_tokens": real_tools,
        "real_request_tokens": real_messages + real_tools,
        "pre_s04_budgeted_tokens": _flat_estimate(messages),
        "current_budgeted_tokens": _estimate(messages)
        + agent_loop._estimate_tool_definition_tokens(json.loads(tools)),
        "context_window_assumed": window,
    }
    for key in ("pre_s04_budgeted_tokens", "current_budgeted_tokens"):
        composite[key.replace("tokens", "share_of_real")] = round(
            composite[key] / composite["real_request_tokens"], 2
        )
    result = {"tokenizers": sorted(tokenizers), "rows": rows, "composite_request": composite}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
