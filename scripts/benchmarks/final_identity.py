"""What object each judgment names, as seen by the Final auditor (Q07 / S06 fix).

The Final auditor (``chief-editor-auditor``) has only ``submit_result``: it
cannot open E-* records.  Before S06 every assessment carried a model-written
``scope`` (object/location/time); S06 made it optional, so Final saw bare
E-IDs.  The fix adds a runtime-derived ``observation_identities`` map (subject
and observed time from the run's evidence snapshot) to its source context.

Uses the S06 synthetic per-observation records from the archived fact packets
(``assessment_burden``) and the production ``observation_identities``.
Provider calls: 0.

    uv run --with tokenizers python scripts/benchmarks/final_identity.py \\
        --tokenizer PATH [--output out.json]
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

from manyselves.capabilities.distribution_reporting.runtime.assessment_context import (  # noqa: E402
    assessment_source_context,
    observation_identities,
)

EVIDENCE = burden.PACKETS.parent / "preparation/evidence.jsonl"
logger.disable("manyselves")


def judged(modules: dict) -> list[dict]:
    return [record for module in modules.values() for part in module.assessments.values()
            for record in part.records or []]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    count = lambda text: len(tokenizer.encode(text).ids)  # noqa: E731
    pre = burden.synthetic_modules(grouped=True, compact=False)
    compact = burden.synthetic_modules(grouped=True, compact=True)
    with TemporaryDirectory(prefix="final-identity-") as directory:
        workspace = Path(directory)
        snapshot = workspace / "Work/runs/replay/preparation/evidence.jsonl"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(EVIDENCE.read_bytes())
        identities = observation_identities(workspace, "replay", compact)
    known = json.loads(identities)
    records = judged(compact)
    ids = {eid for record in records for eid in record.evidence_ids}
    report = {
        "provider_calls": 0,
        "judged_records": len(records),
        "judged_evidence_ids": len(ids),
        "records_with_object_identity": {
            "pre_s06_scope": sum(bool(r.scope) for r in judged(pre)),
            "s06_compact": sum(bool(r.scope) for r in records),
            "now_runtime_identities": sum(all(e in known for e in r.evidence_ids) for r in records),
        },
        "final_context_tokens": {
            "pre_s06_module_assessments": count(assessment_source_context(pre)),
            "s06_module_assessments": count(assessment_source_context(compact)),
            "observation_identities": count(identities),
        },
        "example": {eid: known[eid] for eid in ("E-0071", "E-0230") if eid in known},
    }
    tokens = report["final_context_tokens"]
    tokens["now_total"] = tokens["s06_module_assessments"] + tokens["observation_identities"]
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
