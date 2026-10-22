"""Replay the program steps of a finished ledger report run, without models.

The source project is copied first; nothing in it is changed.  From the run's
saved files the copy re-runs, with the current code:

1. photo comparison on the saved transcriptions;
2. shared context and known values (summary-sheet rows re-marked by mapper);
3. the ledger program findings on the first judgments;
4. the chief table checks on the first chapter 1 and 3 drafts;
5. the report check on the written sections and the chief text;
6. composition and delivery from the final ledger, chief output and edits.

It prints old against new finding counts and what the delivered Markdown and
Word files contain.  Model output is reused as written, so text the models
wrote in this run (for example a table inside a chief text field) stays in the
replayed report; the replay shows whether the checks now catch it.

Usage:
  uv run python scripts/maintenance/replay_ledger_checks.py \
      --project runs/real-run-option2-20261020b --run-id full-report-... \
      --output runs/replay-option2-20261021
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from docx import Document

from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import (
    CHIEF_OWNER,
    LedgerTools,
)
from manyselves.capabilities.distribution_reporting.runtime.mappers import map_s4_6
from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    JudgmentRow,
    action_groups,
)
from manyselves.capabilities.distribution_reporting.runtime.models.photo_reading import (
    PhotoReading,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem
from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
    compare_readings,
    expected_values,
    photo_readings_ref,
)

CHIEF_ACTIONS = ("chapter-3", "revise-3", "chapter-1", "revise-1", "chapter-4")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def _blocking(findings: Mapping[str, Any]) -> list[str]:
    return sorted(t for t, f in findings.items() if (f or {}).get("severity") == "blocking")


def _agent_results(run_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = run_dir / "agent-results"
    return [
        (path, _load(path))
        for path in sorted(root.rglob("*.json"))
        if path.is_file()
    ]


# -- 1. evidence and photo readings --------------------------------------------


def _rebind_trusted_blobs(workspace: Path) -> int:
    """Re-bind the copy's verified content records to the copied files.

    A verified record names the file's device, inode and mtime, so a copied
    project fails the check by design.  Each copied file is hashed first; a
    file whose bytes differ from its record is an error, not re-bound.
    """

    count = 0
    for manifest in sorted((workspace / "Work/content/verified").rglob("*.json")):
        payload = _load(manifest)
        target = workspace / payload["relative_path"]
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != payload["sha256"] or target.stat().st_size != payload["size"]:
            raise ValueError(f"copied content differs from its record: {target}")
        stat = target.stat()
        payload.update(device=stat.st_dev, inode=stat.st_ino, mtime_ns=stat.st_mtime_ns)
        _write(manifest, payload)
        count += 1
    return count


def _refresh_evidence_sources(workspace: Path, run_id: str, items: list[EvidenceItem]) -> int:
    """Store the current serialization of each fact in the copy's source ledger.

    The ledger keeps every fact's JSON and its digest.  A fact field added since
    the run (summary_entry) changes that JSON, which the ledger would reject as
    a conflicting fact id.  The replay re-records the facts it now uses.
    """

    root = workspace / "Work/runs" / run_id
    ledger = root / "ledgers/sources.json"
    if not ledger.is_file():
        return 0
    by_id = {item.id: item for item in items}
    records = _load(ledger)
    count = 0
    for record in records:
        item = by_id.get(record.get("id"))
        if record.get("kind") != "project_evidence" or item is None:
            continue
        content = item.model_dump_json()
        (root / "sources").mkdir(exist_ok=True)
        (root / "sources" / f"{item.id}.txt").write_text(content, encoding="utf-8")
        record["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        count += 1
    ledger.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return count


def _fact_key(item: EvidenceItem) -> tuple:
    # The fact text is left out: preparation may append a note to it.
    return (item.source.file_id, item.source.sheet, item.source.cell, item.submodule_id)


def _remark_summary_rows(
    workspace: Path, run_dir: Path
) -> tuple[dict[str, int], list[EvidenceItem]]:
    """Mark summary-sheet rows by re-running the summary-sheet mapper on its input."""

    manifest_path = next(
        (
            path
            for path in (run_dir / "preparation/manifest.json", workspace / "Work/manifest.json")
            if path.is_file()
        ),
        None,
    )
    manifest = _load(manifest_path) if manifest_path else {}
    files = manifest.get("files") or manifest.get("manifest_files") or []
    # Preparation renumbers facts (E-0001…), so a mapped row is matched to
    # its prepared fact by where it came from and what it says.
    summary_keys: set[tuple] = set()
    summary_files = 0
    for entry in files:
        if entry.get("purpose") != "s4-6":
            continue
        summary_files += 1
        source = workspace / (entry.get("snapshot_ref") or entry["path"])
        mapped = map_s4_6(source, file_id=entry["id"])
        summary_keys.update(_fact_key(item) for item in mapped.evidence_items if item.summary_entry)
    path = run_dir / "preparation/evidence.jsonl"
    items = [
        EvidenceItem.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    items = [
        item.model_copy(update={"summary_entry": _fact_key(item) in summary_keys})
        for item in items
    ]
    path.write_text(
        "".join(item.model_dump_json() + "\n" for item in items), encoding="utf-8"
    )
    marked = sum(item.summary_entry for item in items)
    if summary_keys and not marked:
        raise ValueError(
            f"{len(summary_keys)} summary-sheet rows were mapped but none matched a prepared fact"
        )
    return {"summary_files": summary_files, "mapped": len(summary_keys), "marked": marked}, items


def _recompare_photos(
    workspace: Path, run_id: str, items: Iterable[EvidenceItem], results: list
) -> dict[str, Any]:
    path = workspace / photo_readings_ref(run_id)
    if not path.is_file():
        return {"photos": 0}
    by_id = {item.id: item for item in items}
    reader_flag = {
        record["input"]["photo_id"]: bool((record.get("result") or {}).get("needs_review"))
        for _path, record in results
        if isinstance(record.get("input"), dict) and "photo_path" in record["input"]
    }
    payload = _load(path)
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    changed = []
    readings = []
    for raw in payload.get("readings", []):
        old = PhotoReading.model_validate(raw)
        # Only structured readings are compared; a run read before they
        # existed has none, so its photos come out as "no comparable value".
        bound = [by_id[e] for e in old.evidence_ids if e in by_id]
        expected = expected_values(bound, list(by_id.values()))
        match, reason = compare_readings(old.readings, expected["bound"], expected["same_subject"])
        flagged = reader_flag.get(old.photo_id, old.needs_review and old.record_match != "conflicts")
        new = old.model_copy(update={
            "record_match": match,
            "reason": reason,
            "needs_review": flagged or match == "conflicts",
        })
        before[old.record_match] = before.get(old.record_match, 0) + 1
        after[match] = after.get(match, 0) + 1
        if match != old.record_match:
            changed.append(f"{old.photo_id}: {old.record_match} → {match}（{reason}）")
        readings.append(new.model_dump(mode="json"))
    payload["readings"] = readings
    _write(path, payload)
    return {"photos": len(readings), "before": before, "after": after, "changed": changed}


# -- 2.–5. checks ------------------------------------------------------------


def _replay_checks(
    tools: LedgerTools, run_dir: Path, state: dict[str, Any], results: list
) -> dict[str, Any]:
    run_id = state["run_id"]
    context = tools.prepare_context(state)
    first = _load(run_dir / "ledger/judgments-r0.json")
    built = tools.build_ledger({
        "context": context,
        "judgments": {m: {"module_id": m, "rows": rows} for m, rows in first.items()},
    })
    old_review = {
        record["input"]["module_id"]: record["input"].get("program_findings") or {}
        for _path, record in results
        if isinstance(record.get("input"), dict)
        and "dossiers" in record["input"] and "program_findings" in record["input"]
    }
    ledger_checks = {
        module_id: {
            "old_blocking": len(_blocking(old_review.get(module_id, {}))),
            "new_blocking": len(_blocking(findings)),
            "new": {t: f["problem"] for t, f in findings.items()},
        }
        for module_id, findings in built["program_findings"].items()
    }

    # Result files are named by the action id, plus "--<task id>" when the
    # task id differs from it (runs after the store-key change).
    def action_of(path: Path) -> str:
        return path.stem.split("--", 1)[0]

    chief_dir = next(
        (path.parent for path, _ in results if action_of(path) == "chapter-3"), None
    )
    chief_files = {
        action_of(path): path
        for path in (sorted(chief_dir.glob("*.json")) if chief_dir is not None else ())
    }
    chief_runs = {
        action: _load(chief_files[action])["result"]
        for action in CHIEF_ACTIONS
        if action in chief_files
    }
    final = {
        "run_id": run_id,
        "ledger": _load(run_dir / "ledger/judgments.json"),
        "unanswered_findings": {},
    }
    rows = {m: [JudgmentRow.model_validate(r) for r in rs] for m, rs in final["ledger"].items()}
    reference = _load(run_dir / "ledger/reference-values.json")
    final["action_groups"] = action_groups(rows, reference.get("checkpoint_of") or {})
    final["chief_input"] = {"module_titles": context.get("module_titles") or {}}
    for action in ("chapter-3", "revise-3"):
        if action in chief_runs:
            chief_runs[action] = _chapter3_now(chief_runs[action], final["action_groups"])
    chief_checks = {}
    if "chapter-3" in chief_runs:
        chief_checks["chapter3_first"] = tools.check_chief(
            {"ledger": final, "chapter3": chief_runs["chapter-3"]}
        )["findings"]
    if "chapter-1" in chief_runs:
        chief_checks["chapter1_first"] = tools.check_chief({
            "ledger": final,
            "chapter3": chief_runs.get("revise-3") or chief_runs["chapter-3"],
            "chapter1": chief_runs["chapter-1"],
        })["findings"]
    chief = {
        "chapter3": chief_runs.get("revise-3") or chief_runs.get("chapter-3"),
        "chapter1": chief_runs.get("revise-1") or chief_runs.get("chapter-1"),
        "chapter4": chief_runs.get("chapter-4"),
    }

    old_edits = {
        record["input"]["owner"]: record["input"]
        for _path, record in results
        if isinstance(record.get("input"), dict)
        and {"owner", "sections", "program_findings"} <= set(record["input"])
    }
    written = {
        owner: [{"submodule_id": key, "text": text} for key, text in item["sections"].items()]
        for owner, item in old_edits.items()
        if owner != CHIEF_OWNER
    }
    checked = tools.check_report({"ledger": final, "written": written, "chief": chief})
    edit_checks = {
        item["owner"]: {
            "old_blocking": len(_blocking(old_edits.get(item["owner"], {}).get("program_findings") or {})),
            "new_blocking": len(_blocking(item["program_findings"])),
            "new": {t: f["problem"] for t, f in item["program_findings"].items()},
        }
        for item in checked["edit_items"]
    }

    revisions = [
        record["result"]["sections"]
        for _path, record in results
        if isinstance(record.get("result"), dict)
        and isinstance(record["result"].get("sections"), dict)
        and isinstance(record.get("input"), dict) and "findings" in record["input"]
    ]
    edits = {}
    for owner, item in old_edits.items():
        sections = dict(item["sections"])
        for revised in revisions:
            if revised and set(revised) <= set(sections):
                sections.update(revised)
        edits[owner] = {"owner": owner, "sections": sections, "findings": {}}
    return {
        "final": final,
        "chief": chief,
        "edits": edits,
        "ledger_checks": ledger_checks,
        "chief_checks": chief_checks,
        "edit_checks": edit_checks,
    }


def _chapter3_now(chapter3: Mapping[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any]:
    """An older chapter 3 in today's shape: its overview as one summary line, and
    each program group worded by the old action that covered its first row."""

    if "risk_summary" in chapter3:
        return dict(chapter3)
    old = list(chapter3.get("actions") or [])
    texts = []
    for group in groups:
        action = next((a for a in old if set(a.get("row_ids") or ()) & set(group["row_ids"])), None)
        if action is not None:
            texts.append({
                "group_id": group["group_id"],
                **{k: action[k] for k in ("problem", "hazard", "solution") if k in action},
                "note": action.get("note", ""),
            })
    return {
        "risk_summary": [chapter3.get("risk_overview") or ""],
        "risk_details": [],
        "cross_findings": chapter3.get("cross_findings") or [],
        "action_lead": chapter3.get("action_lead") or "",
        "actions": texts,
    }


# -- 6. delivery ---------------------------------------------------------------


def _markdown_tables(text: str) -> list[int]:
    """Data-row count of each Markdown table (header included, separator not)."""

    counts: list[int] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            if not re.fullmatch(r"[|\s:-]+", stripped):
                if not inside:
                    counts.append(0)
                counts[-1] += 1
            inside = True
        else:
            inside = False
    return counts


def _deliver(tools: LedgerTools, workspace: Path, state: dict[str, Any], replayed: dict) -> dict:
    delivered = tools.compose_report({
        "state": state,
        "ledger": replayed["final"],
        "chief": replayed["chief"],
        "edits": replayed["edits"],
        "reviews": {},
    })
    outputs = [Path(a.get("path") or a.get("ref") or "") for a in delivered["output_artifacts"]]
    # The report is the first Markdown/Word pair among the delivered reports.
    reports = [p for p in outputs if p.parts[:2] == ("Outputs", "Reports")]
    markdown = next((workspace / p for p in reports if p.suffix == ".md"), None)
    word = markdown.with_suffix(".docx") if markdown else None
    report: dict[str, Any] = {
        "render_verification": delivered.get("render_verification"),
        "outputs": [p.as_posix() for p in outputs],
    }
    if markdown and markdown.is_file():
        text = markdown.read_text(encoding="utf-8")
        report["markdown"] = {
            "file": markdown.relative_to(workspace).as_posix(),
            "补充补充": text.count("补充补充"),
            "K-编号": len(re.findall(r"K-\d{4}", text)),
            "各表行数": _markdown_tables(text),
            "中文字数": len(re.findall(r"[\u4e00-\u9fff]", text)),
        }
    if word and word.is_file():
        document = Document(str(word))
        # Word also holds template tables (cover, headers); compare by table.
        report["word"] = {
            "file": word.relative_to(workspace).as_posix(),
            "各表行数": [len(table.rows) for table in document.tables],
        }
        if "markdown" in report:
            remaining = list(report["word"]["各表行数"])
            short = []
            for rows in report["markdown"]["各表行数"]:
                if rows in remaining:
                    remaining.remove(rows)
                else:
                    short.append(rows)
            report["markdown_tables_without_equal_word_table"] = short
    return report


def replay(project: Path, run_id: str, output: Path, knowledge_root: Path | None) -> dict:
    project, output = project.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(project):
        raise ValueError("output must be a new directory outside the source project")
    shutil.copytree(project, output, symlinks=True)
    rebound = _rebind_trusted_blobs(output)
    run_dir = output / "Work/runs" / run_id
    state_file = _load(run_dir / "runtime-state.json")
    variables = state_file["variables"]
    # full-report hands the ledger its state as "reporting-state"; a direct
    # report-ledger run holds it as "state".
    state = variables.get("reporting-state") or variables["state"]
    results = _agent_results(run_dir)

    # Reporting files live under the reporting run id, which a direct
    # report-ledger run may hold apart from the workflow run id.
    reporting_dir = output / "Work/runs" / state["run_id"]
    summary_rows, items = _remark_summary_rows(output, reporting_dir)
    by_id = {item.id: item for item in items}
    state["evidence_items"] = [
        by_id[item["id"]].model_dump(mode="json") if item.get("id") in by_id else item
        for item in state.get("evidence_items", [])
    ]
    _refresh_evidence_sources(output, state["run_id"], items)
    photos = _recompare_photos(output, state["run_id"], items, results)
    tools = LedgerTools(output, global_knowledge_root=knowledge_root)
    replayed = _replay_checks(tools, reporting_dir, state, results)
    delivery = _deliver(tools, output, state, replayed)
    summary = {
        "run_id": run_id,
        "trusted_blobs_rebound": rebound,
        "summary_rows_marked": summary_rows,
        "photo_comparison": photos,
        "ledger_checks": replayed["ledger_checks"],
        "chief_checks": replayed["chief_checks"],
        "edit_checks": replayed["edit_checks"],
        "delivery": delivery,
    }
    _write(output / "replay-summary.json", summary)
    return summary


def _print(summary: dict) -> None:
    photos = summary["photo_comparison"]
    rows = summary["summary_rows_marked"]
    print(
        f"run {summary['run_id']}；评估总表文件 {rows['summary_files']} 个，"
        f"映射出汇总条目 {rows['mapped']} 条，标记 {rows['marked']} 条"
    )
    print(f"读图比对：{photos.get('before')} → {photos.get('after')}")
    for line in photos.get("changed", []):
        print("  ", line)
    print("台账程序意见（阻断条数，旧 → 新）：")
    for module_id, check in summary["ledger_checks"].items():
        print(f"  {module_id}: {check['old_blocking']} → {check['new_blocking']}")
        for target, problem in check["new"].items():
            print(f"     {target}: {problem}")
    print("总编表格检查（首稿）：")
    for name, findings in summary["chief_checks"].items():
        print(f"  {name}: " + ("；".join(f"{k}: {v['problem']}" for k, v in findings.items()) or "无"))
    print("成稿程序核对（阻断条数，旧 → 新）：")
    for owner, check in summary["edit_checks"].items():
        print(f"  {owner}: {check['old_blocking']} → {check['new_blocking']}")
        for target, problem in check["new"].items():
            print(f"     {target}: {problem}")
    print("交付：", json.dumps(summary["delivery"], ensure_ascii=False, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--knowledge-root", type=Path)
    args = parser.parse_args()
    _print(replay(args.project, args.run_id, args.output, args.knowledge_root))


if __name__ == "__main__":
    main()
