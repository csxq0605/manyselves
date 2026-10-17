"""Replay archived object coverage through current routing, without generating reports.

The report is historical. Citation/identifier presence is not semantic coverage.
Only aggregate counts, source coordinates and IDs are exported, never project prose.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
    render_fact_packet,
)
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import (
    ProjectEvidenceIndex,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves'
HEADING = re.compile(r'(?m)^(\d(?:\.\d+){1,3})\.\s+([^\n]+)$')


def section(text: str, number: str) -> str:
    # Exclude table-of-contents lines with attached page numbers. This parser
    # deliberately targets the archived extraction, not arbitrary future DOCX.
    headings = [m for m in HEADING.finditer(text) if not m[2].rstrip()[-1].isdigit()]
    for index, heading in enumerate(headings):
        if heading[1] == number:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[heading.end():end]
    raise ValueError(f'archived report section not found: {number}')


def _has_id(text: str, identifier: str) -> bool:
    return bool(re.search(r'(?<![\w-])' + re.escape(identifier) + r'(?![\w-])', text))


def observation_key(item):
    """Alias only identical frozen observations routed under different E IDs.

    Object, source position, time and exact fact all have to match. Matching a
    number alone would incorrectly merge different operating conditions.
    """
    return (item.subject, item.source.path, item.source.sheet, item.source.cell,
            item.source.page, item.observed_at, item.fact)


def replay(archive: Path = ARCHIVE) -> dict:
    report = (archive / '配电安全专家咨询报告.docx.txt').read_text(encoding='utf-8')
    evidence = archive / 'fact_packet_replay/Work/runs/replay/preparation/evidence.jsonl'
    scopes = (
        ('residual_current', '2.4', '2.4.3.1', '现状：', lambda e: e.fact.startswith('实测剩余电流=')),
        ('thermal', '2.2', '2.2.2.1', '现状描述：', lambda e: e.source.sheet == '红外热成像检测记录表' and '测点温度=' in e.fact),
        ('spd', '2.3', '2.3.3', '现状：', lambda e: e.source.sheet == '总配评估详情表' and e.fact.startswith('电涌保护装置=')),
    )
    groups = {}
    with TemporaryDirectory(prefix='quality-replay-') as directory:
        workspace = Path(directory)
        snapshot = workspace / 'Work/runs/replay/preparation/evidence.jsonl'
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(evidence.read_bytes())
        index = ProjectEvidenceIndex(workspace, 'replay')
        aliases = {}
        for item in index.items():
            aliases.setdefault(observation_key(item), []).append(item.id)
        for name, module, chapter, marker, predicate in scopes:
            module_items = index.module_items(module)
            packet = render_fact_packet(module_items, module)
            body = section(report, chapter)
            if marker not in body:
                raise ValueError(f'archived narrative boundary missing: {chapter}')
            narrative = body.split(marker, 1)[1]
            rows = []
            for item in module_items:
                if not predicate(item):
                    continue
                rows.append({
                    'evidence_id': item.id,
                    'evidence_aliases': aliases[observation_key(item)],
                    'source': {'sheet': item.source.sheet, 'cell': item.source.cell},
                    'input_marked_ng': '=NG' in item.fact,
                    'delivered_by_current_packet': _has_id(packet, item.id),
                    'source_alias_in_historical_section_including_captions': any(_has_id(body, eid) for eid in aliases[observation_key(item)]),
                    'source_alias_in_historical_narrative': any(_has_id(narrative, eid) for eid in aliases[observation_key(item)]),
                    'judgment_coverage': 'requires_semantic_review',
                    'action_coverage': 'requires_object_mapping',
                })
            groups[name] = {
                'module': module, 'section': chapter, 'records': len(rows),
                'delivered': sum(r['delivered_by_current_packet'] for r in rows),
                'section_id_mentions': sum(r['source_alias_in_historical_section_including_captions'] for r in rows),
                'narrative_id_mentions': sum(r['source_alias_in_historical_narrative'] for r in rows),
                'rows': rows,
            }
    residual = section(report, '2.4.3.1').split('现状：', 1)[1]
    spd = section(report, '2.3.3').split('现状：', 1)[1]
    actions = section(report, '3.2')
    # These are specific previously reviewed defects, not general-purpose
    # judgments inferred from arbitrary prose. Keep their scope explicit.
    checks = {
        'residual_caption_has_E0071_but_narrative_omits_it': (
            _has_id(section(report, '2.4.3.1'), 'E-0071') and not _has_id(residual, 'E-0071')),
        'residual_narrative_still_says_all_at_most_2_1A_and_overall_OK': (
            '均不大于 2.1A' in residual and '当前整体处于 OK 状态' in residual),
        'spd_all_four_NG_ids_in_captions': all(_has_id(section(report, '2.3.3'), eid)
                                              for eid in ('E-0088', 'E-0115', 'E-0133', 'E-0142')),
        'spd_narrative_still_limits_confirmed_defects_to_two': '存在两处已确认' in spd,
        'spd_action_still_targets_two': bool(re.search(r'已确认的\s*2\s*台', actions)),
        'thermal_last_two_source_rows_not_cited_in_section': all(
            not _has_id(section(report, '2.2.2.1'), eid)
            for eid in ('E-0230', 'E-0231', 'E-0232', 'E-0233')),
    }
    return {
        'provider_calls': 0, 'new_report_runs': 0,
        'source_run': 'full-report-8c70828443b9499d80f8a6bda41c670c',
        'historical_report_run': 'revise-report-8129b20dfb0d4bb9b1574645447375f0',
        'groups': groups, 'known_historical_defect_checks': checks,
        'limits': [
            'Frozen normalized evidence is replayed; original workbooks are not reparsed here.',
            'Current routing is checked against an old report, not current model output.',
            'An ID in prose or a caption does not establish correct judgment or action coverage.',
            'Missing literal IDs do not prove an object is absent under a different name or citation.',
            'No domain risk threshold is inferred from NG or source-ID presence.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({name: {k: v for k, v in group.items() if k != 'rows'}
                      for name, group in result['groups'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
