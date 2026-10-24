"""Read-only local sizing probe: enumerate table facts, no model calls."""
import json
from pathlib import Path

run=Path('test/Work/runs/full-report-8c70828443b9499d80f8a6bda41c670c')
items=[json.loads(s) for s in (run/'preparation/evidence.jsonl').read_text(encoding='utf-8').splitlines()]
groups={
    'residual_current': lambda e: e['fact'].startswith('实测剩余电流='),
    'thermal': lambda e: '测点温度=' in e['fact'] and e['source']['sheet']=='红外热成像检测记录表',
    'spd': lambda e: e['fact'].startswith('电涌保护装置=') and e['source']['sheet']=='总配评估详情表',
}
result={}
for group,predicate in groups.items():
    selected=[e for e in items if predicate(e)]
    by_location={}
    for e in selected:
        s=e['source'];key=(s['path'],s['sheet'],s['cell'],e['subject'],e['fact'])
        by_location.setdefault(key,[]).append(e['id'])
    lines=['证据ID | 对象 | 原始事实 | 单元格']
    # Source file and sheet repeated once, not on every row.
    for (path,sheet,cell,subject,fact),ids in by_location.items():
        lines.append(f"{','.join(ids)} | {subject} | {fact} | {cell}")
    source=next(iter(by_location))
    packet=f'来源：{source[0]}；工作表：{source[1]}\n'+'\n'.join(lines)
    result[group]={'normalized_records':len(selected),'unique_facts':len(by_location),'packet_chars':len(packet),'packet_utf8_bytes':len(packet.encode()),'evidence_ids':[e['id'] for e in selected]}
    Path(f'audit_manyselves/{group}_packet.txt').write_text(packet,encoding='utf-8')
Path('audit_manyselves/evidence_packet_metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
