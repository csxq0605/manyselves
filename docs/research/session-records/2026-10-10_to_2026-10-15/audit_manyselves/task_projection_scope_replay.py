"""Targeted replay of two failing synthetic payloads; only scope header changes.
Saved original answers are never overwritten. At most four further API requests.
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parent))
import task_projection_probe as probe
from manyselves.runtime.providers.base import Message

async def main():
    original=json.loads((probe.ROOT/'audit_manyselves/task_projection_live.json').read_text(encoding='utf-8'))
    config=probe.Settings().load_config()
    route=next(c for c in config.providers.configurations if c.id==config.providers.active)
    assert route.enabled and route.provider=='anthropic' and urlsplit(route.api_base).hostname=='api.anthropic.com'
    provider=probe.AnthropicProvider(api_key=route.api_key,api_base=route.api_base,model=route.default_model,extra_headers=route.extra_headers)
    provider.client=provider.client.with_options(timeout=probe.httpx.Timeout(50,connect=10),max_retries=0)
    report={'scope':'synthetic_only','design':'replay original failing G3 late payload, replace fact packet scope header only; stochastic spot check, not controlled causal evidence','attempts':0,'cases':[]}
    out=probe.ROOT/'audit_manyselves/task_projection_scope_replay.json'
    def save(): out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for trial in (0,1):
            lane=next(l for l in original['lanes'] if l['trial']==trial and l['name']=='G3')
            req=lane['requests'][-1]
            assert req['phase']!='context_compaction'
            ws=probe.ROOT/'audit_manyselves/projection-live'/f'{trial}-G3'
            env=probe.TaskEnvelope(task_id='lab-audit',run_id='synthetic',agent_id='lab-author',objective='虚构专题审查',allowed_outputs=['lab-json'],target_submodule_ids=['2.4.3.1'])
            packet=probe.task_fact_packet(ws,env,'2.4')
            new_header=packet[packet.index('记录总数：'):packet.index('每条 JSON 数组列')]
            messages=[]
            changes=0
            for item in req['messages']:
                text,n=re.subn(r'记录总数：[^\n]*\n完整性仅限[^\n]*\n',lambda _:new_header,item['content'])
                changes+=n
                obj=dict(item,content=text)
                if obj.get('tool_calls'):
                    from manyselves.runtime.providers.base import LLMToolCall
                    obj['tool_calls']=[LLMToolCall(**c) for c in obj['tool_calls']]
                messages.append(Message(**obj))
            assert changes==1
            records,_,expected=probe.fixture(trial)
            case={'trial':trial,'requests':[],'tool_reads':[],'changed_header_count':changes}
            report['cases'].append(case)
            agent=probe.AgentLoop(agent_type='scope-replay',workspace=ws/'replay',tools=probe.ToolRegistry(),bus=probe.MessageBus(),llm_provider=provider,
                                 config=probe.AgentDefaults(working_memory_tokens=1000000,max_tokens=2048))
            async def before(messages,tools,phase,attempt):
                if report['attempts']>=4: raise RuntimeError('experiment_call_limit')
                report['attempts']+=1
                case['requests'].append({'messages':[probe.dataclasses.asdict(m) for m in messages]})
                save()
            async def record(usage):
                case['requests'][-1]['usage']={k:usage.get(k) for k in ('input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','duration_ms')}
                save()
            agent.provider_attempt_observer=before
            agent.provider_attempt_record_observer=record
            for turn in range(2):
                response=await agent._chat_with_retries(messages,probe.TOOLS,f'replay-{trial}-{turn}',reasoning_enabled=False)
                case['requests'][-1]['raw_response']=response.content
                messages.append(Message(role='assistant',content=response.content or '',tool_calls=response.tool_calls))
                if not response.tool_calls:
                    case['actual']=probe.parse(response.content)
                    case['score']=probe.score(case['actual'],expected)
                    break
                for call in response.tool_calls:
                    subjects=call.arguments.get('subjects',[]) if call.name=='lookup_local_fact' else []
                    found=[r.model_dump(mode='json') for r in records if r.subject in subjects]
                    missing=[s for s in subjects if not any(r.subject==s for r in records)]
                    messages.append(Message(role='tool',content=probe.dump(dict(records=found,missing_subjects=missing)),is_tool_result=True,tool_call_id=call.id))
                    case['tool_reads'].append({'subjects':subjects,'returned':[r['id'] for r in found]})
            save()
            print(probe.dump({'trial':trial,'score':case.get('score'),'tool_reads':case['tool_reads']}),flush=True)
        report['completed']=True
    except Exception as exc:
        report['error']={'type':type(exc).__name__,'status':getattr(exc,'status_code',None)}
        print(probe.dump(report['error']),flush=True)
    finally:
        save()
        await provider.client.close()

if __name__=='__main__': asyncio.run(main())
