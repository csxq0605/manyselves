"""Rescore retained synthetic outputs without making any Provider call."""
import json
from pathlib import Path
import re
import statistics
import writing_architecture_ab as probe

root = Path(__file__).parent
reports = {}
for suffix in ("", "_typed"):
    report = json.loads((root / f"writing_architecture_ab{suffix}.json").read_text(encoding="utf-8"))
    rows = []
    for arm in ("direct", "write_review", "judgment_first"):
        cases = [c for c in report["cases"] if c["arm"] == arm]
        requests = [r for c in cases for r in c["requests"]]
        findings = []
        for case in cases:
            thermal = next((r.get("text", "") for r in case["result"].get("judgments", []) if r.get("id", "").endswith("0004")), "")
            findings.append({"repeat": case["repeat"], "size": case["size"],
                             "errors": probe.score(case["result"]), "thermal_text": thermal,
                             "temperature_named_as_rise": bool(re.search(r"温升.{0,3}55", thermal)),
                             "ledger_unknowns": [r["id"] for r in case.get("ledger", []) if r["status"] == "unknown"]})
        rows.append({"arm": arm, "runs": len(cases), "calls": len(requests),
                     "wall_mean_seconds": statistics.mean(c["wall_seconds"] for c in cases),
                     "wall_range_seconds": [min(c["wall_seconds"] for c in cases), max(c["wall_seconds"] for c in cases)],
                     "input_tokens": sum((r.get("usage") or {}).get("input_tokens", 0) for r in requests),
                     "output_tokens": sum((r.get("usage") or {}).get("output_tokens", 0) for r in requests),
                     "cached_input_tokens": sum((r.get("usage") or {}).get("cached_input_tokens", 0) for r in requests),
                     "zero_structured_error_runs": sum(not f["errors"] for f in findings),
                     "temperature_named_as_rise_runs": sum(f["temperature_named_as_rise"] for f in findings),
                     "findings": findings})
    reports["typed" if suffix else "untyped"] = {"model": report["model"], "complete": report.get("complete"), "results": rows}
(root / "writing_architecture_metrics.json").write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(reports,ensure_ascii=False,indent=2))
