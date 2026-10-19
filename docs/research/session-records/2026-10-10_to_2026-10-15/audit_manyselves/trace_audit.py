import json, hashlib
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta
runs=Path('test/Work/runs')
for rid in ['full-report-8c70828443b9499d80f8a6bda41c670c','revise-report-8129b20dfb0d4bb9b1574645447375f0']:
    p=runs/rid
    rows=[json.loads(x) for x in (p/'tool-result-index.jsonl').read_text(encoding='utf-8').splitlines()]
    print('\nRUN',rid,'tools',Counter(r['tool_name'] for r in rows),'dedup',Counter(r.get('deduplicated') for r in rows))
    print('already_read',sum('already_read' in json.dumps(r.get('result')) for r in rows))
    for r in rows:
        if r.get('tool_name')=='search_project_evidence' and any(t in json.dumps(r.get('arguments'),ensure_ascii=False) for t in ['剩余','热','SPD','防雷']):
            result=r.get('result') or {}
            print('SEARCH',r.get('task_id'),r['arguments'],[(x.get('id'),x.get('subject'),x.get('fact')) for x in result.get('hits',[])])
    print('Missing-fact delivered raw tool hits:')
    for id in ['E-0133','E-0142','E-0230','E-0231','E-0232','E-0233','E-0041']:
        found=[]
        for r in rows:
            s=json.dumps(r.get('result'),ensure_ascii=False)
            if id in s and ('fact' in s):
                found.append((r.get('task_id'),r['tool_name'],r.get('arguments')))
        print(id,found[:12], 'count',len(found))
    usage=[json.loads(x) for x in Path(f'test/.manyselves/usage/{rid}.jsonl').read_text(encoding='utf-8').splitlines()]
    print('errors',[(r['timestamp'],r.get('error_class'),str(r.get('error'))[:230]) for r in usage if r['status']=='error'])
    intervals=[]
    for r in usage:
        end=datetime.fromisoformat(r['timestamp']); start=end-timedelta(milliseconds=r.get('provider_active_ms',0))
        intervals.append((start,end))
    intervals.sort();merged=[]
    for a,b in intervals:
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    span=(max(b for a,b in intervals)-min(a for a,b in intervals)).total_seconds()
    print('time',{'span_minutes':span/60,'active_union_minutes':sum((b-a).total_seconds() for a,b in merged)/60,'sum_active_minutes':sum(r.get('provider_active_ms',0) for r in usage)/60000})
    ds=json.loads((p/'resolved-plan.json').read_text(encoding='utf-8'))['definition_snapshots']
    print('snapshot shape',type(ds).__name__,str(ds)[:1000])
for p in runs.glob('*/delivery/*/delivery-manifest.json'):
    d=json.loads(p.read_text(encoding='utf-8'))
    print('delivery manifest',p.parts[3], {k:v for k,v in d.items() if any(x in k for x in ['time','created','updated','version'])})
