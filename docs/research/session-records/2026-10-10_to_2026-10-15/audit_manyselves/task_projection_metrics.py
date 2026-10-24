"""Summarize retained synthetic answers without treating ambiguous JSON as success."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
raw=json.loads((ROOT/'task_projection_live.json').read_text(encoding='utf-8'))
result={'source':'task_projection_live.json','completed':raw.get('completed'),'physical_requests':raw['attempts_started'],'lanes':{}}
def objects(text):
    out=[]; start=0; decoder=json.JSONDecoder()
    while start<len(text):
        index=text.find('{',start)
        if index<0: break
        try:
            obj,end=decoder.raw_decode(text[index:])
            if isinstance(obj,dict) and 'rows' in obj: out.append(obj)
            start=index+end
        except ValueError: start=index+1
    return out
for name in ('G1','G2','G3'):
    lanes=[l for l in raw['lanes'] if l['name']==name]
    requests=[r for l in lanes for r in l['requests']]
    totals={k:sum(r.get('usage_record',{}).get(k) or 0 for r in requests) for k in ('input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','duration_ms')}
    totals['uncached_input_tokens']=totals['input_tokens']-totals['cached_input_tokens']-totals['cache_write_input_tokens']
    totals['requests']=len(requests)
    totals['summaries']=sum(r['phase']=='context_compaction' for r in requests)
    totals['summary_input_tokens']=sum(r.get('usage_record',{}).get('input_tokens') or 0 for r in requests if r['phase']=='context_compaction')
    totals['summary_duration_ms']=sum(r.get('usage_record',{}).get('duration_ms') or 0 for r in requests if r['phase']=='context_compaction')
    totals['elapsed_seconds']=sum(l['elapsed_seconds'] for l in lanes)
    totals['first_object_correct_fields']=sum(p['score']['correct_fields'] for l in lanes for p in l['phases'])
    totals['total_fields']=sum(p['score']['total_fields'] for l in lanes for p in l['phases'])
    totals['tool_calls']=sum(len(l['tool_reads']) for l in lanes)
    totals['redundant_subject_reads']=sum('T35' in t['subjects'] for l in lanes for t in l['tool_reads'])
    totals['ambiguous_answers']=[]
    totals['failures']=[]
    for lane in lanes:
        for phase in lane['phases']:
            parsed=objects(phase.get('raw_response') or '')
            if len(parsed)>1:
                totals['ambiguous_answers'].append({'trial':lane['trial'],'phase':phase['phase'],'objects':len(parsed),
                                                   'first_rows':parsed[0]['rows'],'last_rows':parsed[-1]['rows']})
            if phase['score']['errors']:
                totals['failures'].append({'trial':lane['trial'],'phase':phase['phase'],'errors':phase['score']['errors']})
    result['lanes'][name]=totals
(ROOT/'task_projection_metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
