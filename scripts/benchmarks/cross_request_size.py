"""Joint Cross first-request size: estimate vs tokenizer (real-run blocker).

The 2026-10-19 real run blocked ``cross-joint-r0`` before sending: about 204k
characters of messages exceeded the 128k - max_tokens budget by estimate.
This rebuilds the inline five-module payload from the archived report
sections, S06 compact per-observation records and the archived evidence
snapshot, then compares indented vs compact JSON under the per-character and
whitespace-run estimators against real BPE tokenizers.  Provider calls: 0.

    uv run --with tokenizers python scripts/benchmarks/cross_request_size.py \\
        --tokenizer deepseek=PATH [--tokenizer mimo=PATH] [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assessment_burden as burden  # noqa: E402
from reporting_quality_replay import ARCHIVE, section  # noqa: E402

from manyselves.capabilities.distribution_reporting.runtime.assessment_context import (  # noqa: E402
    source_observation_groups,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (  # noqa: E402
    module_content_view,
)
from manyselves.runtime.loops import agent_loop  # noqa: E402

logger.disable("manyselves")
EVIDENCE = ARCHIVE / "fact_packet_replay/Work/runs/replay/preparation/evidence.jsonl"
BUDGET = 128_000 - 8_192  # real-run window minus the saved max_tokens


def per_character_estimate(text: str) -> int:
    """The estimator before whitespace runs were merged (comparison only)."""
    ascii_bytes = text.encode("ascii", "ignore")
    digits = sum(byte in b"0123456789" for byte in ascii_bytes)
    letters = sum(chr(byte).isalpha() for byte in ascii_bytes)
    other = len(ascii_bytes) - digits - letters
    return int((len(text) - len(ascii_bytes)) * 0.72 + digits + letters * 0.22 + other * 0.5)


def payload() -> dict:
    report = (ARCHIVE / "配电安全专家咨询报告.docx.txt").read_text(encoding="utf-8")
    views = {}
    for module_id, module in burden.synthetic_modules(grouped=False, compact=True).items():
        narratives = {}
        for part_id, placeholder in module.submodule_narratives.items():
            try:
                narratives[part_id] = section(report, part_id)
            except Exception:
                narratives[part_id] = placeholder
        views[module_id] = module_content_view(
            module.model_copy(update={"submodule_narratives": narratives})
        ).model_dump(mode="json")
    with TemporaryDirectory() as directory:
        snapshot = Path(directory) / "Work/runs/replay/preparation/evidence.jsonl"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(EVIDENCE.read_bytes())
        groups = source_observation_groups(Path(directory), "replay") or []
    return {"modules": views, "source_observation_groups": groups}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer", action="append", default=[], help="name=tokenizer.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from tokenizers import Tokenizer

    tokenizers = {name: Tokenizer.from_file(path) for name, path in (t.split("=", 1) for t in args.tokenizer)}
    value = payload()
    rows = {}
    for name, text in (
        ("indent2", json.dumps(value, ensure_ascii=False, indent=2)),
        ("compact", json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
    ):
        rows[name] = {
            "chars": len(text),
            "per_character_estimate": per_character_estimate(text),
            "whitespace_run_estimate": agent_loop._estimate_text_tokens(text),
            "tokenizer_tokens": {
                key: len(tok.encode(text, add_special_tokens=False).ids) for key, tok in tokenizers.items()
            },
        }
    result = {"budget_tokens": BUDGET, "payload": rows, "provider_calls": 0,
              "note": "Payload only; the real request adds Agent instructions, task text and ~10k chars of tools."}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
