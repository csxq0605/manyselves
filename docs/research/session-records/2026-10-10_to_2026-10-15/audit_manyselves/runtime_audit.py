import json, hashlib
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta

root = Path('test')
runs = root/'Work/runs'
out = {}
for ledger in (root/'.manyselves/usage').glob('*.jsonl'):
    rows = [json.loads(x) for x in ledger.read_text(encoding='utf-8').splitlines() if x.strip()]
    if not rows: continue
    sent = [r for r in rows if r.get('provider_request_sent', True)]
    fields = ['input_tokens','output_tokens','total_tokens','cached_input_tokens','uncached_input_tokens','duration_ms','queue_wait_ms','provider_active_ms','request_chars','repeated_chars']
    groups = {}
    for field in ['phase','round_reason','status','model','agent_id']:
        g = defaultdict(lambda: {'n':0,'tokens':0,'active_ms':0})
        for r in sent:
            d=g[str(r.get(field))]; d['n']+=1; d['tokens']+=r.get('total_tokens',0) or 0;d['active_ms']+=r.get('provider_active_ms',0) or 0
        groups[field]=dict(g)
    result = {'rows':len(rows),'requests':len(sent),'first':min(r['timestamp'] for r in rows),'last':max(r['timestamp'] for r in rows),'sums':{f:sum(r.get(f,0) or 0 for r in sent) for f in fields},'groups':groups}
    run=runs/ledger.stem
    result['delivery']=(run/'delivery-receipt.json').exists()
    for name in ['delivery-completion.json','request-snapshot.json']:
        p=run/name
        if p.exists():result[name]=json.loads(p.read_text(encoding='utf-8'))
    out[ledger.stem]=result
Path('audit_manyselves/runtime_metrics.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
for name,v in sorted(out.items(),key=lambda x:x[1]['last']):
    print(name, json.dumps({k:v[k] for k in ['requests','first','last','delivery','sums']},ensure_ascii=False))
for name in ['full-report-8c70828443b9499d80f8a6bda41c670c','revise-report-8129b20dfb0d4bb9b1574645447375f0']:
    p=runs/name
    print('\nDETAIL',name,json.dumps(out[name]['groups'],ensure_ascii=False))
    for n in ['delivery-completion.json','runtime-state.json','resolved-plan.json']:
        obj=json.loads((p/n).read_text(encoding='utf-8'))
        print(n, 'keys',list(obj))
        print('time/status', {k:v for k,v in obj.items() if any(t in k for t in ['time','created','updated','status','commit','version'])})
    files=list((p/'agent-conversations').glob('*.json'))
    print('conversations',[(f.name,f.stat().st_size) for f in files[:4]])
    if files: print('conversation schema',list(json.loads(files[0].read_text(encoding='utf-8'))))
    a=next((p/'task-attempts').rglob('current.json'))
    print('attempt example',a.as_posix(),a.read_text(encoding='utf-8')[:1200])
print('REPORT HASH',hashlib.sha256((root/'Outputs/Reports/配电安全专家咨询报告.docx').read_bytes()).hexdigest())
