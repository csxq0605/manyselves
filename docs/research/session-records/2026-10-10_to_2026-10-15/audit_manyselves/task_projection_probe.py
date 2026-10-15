"""Bounded synthetic G1/G2/G3 probe of production context construction and dispatch.
No customer input access. --dry-run validates assembly without a model call.
"""
import asyncio
import dataclasses
import json
import random
import sys
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'manyselves'))
import httpx
from loguru import logger
from manyselves.config.schema import AgentDefaults, Settings
from manyselves.runtime.loops.agent_loop import AgentLoop, _estimate_tokens
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.loops.context_rebase import TaskContextRebuilder
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message, LLMStreamChunk
from manyselves.runtime.tools.registry import ToolRegistry
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem, SourceLocation
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.task_context import bind_task_context, task_fact_packet

logger.disable('manyselves')
DRY = '--dry-run' in sys.argv
OUT = ROOT / 'audit_manyselves' / ('task_projection_preflight.json' if DRY else 'task_projection_live.json')
SYSTEM = ('你在审计完全虚构的实验室数据，不评估真实设施或法律规范。仅以当前任务事实为依据，旧任务不适用。'
          '精确保留数值、单位、否定、缺失、冲突和证据编号。虚构实验规则：读数>10标abnormal，<=10标normal；'
          '未测或来源不存在标missing，同一对象相互矛盾且未明确取代标conflict。明确更正优先。'
          '只有缺少必要原始记录时才调用lookup_local_fact，不用搜索已经给出的记录。不得编造数值或来源。'
          '回答只用JSON。每个对象包含values（数值字符串数组），status，sources（证据ID数组）。')
TOOLS = [{'name':'lookup_local_fact', 'description':'按对象名批量读取本地虚构当前快照；无记录返回missing，不证明现实资料未提供。',
          'input_schema':{'type':'object','properties':{'subjects':{'type':'array','items':{'type':'string'}}},'required':['subjects'],'additionalProperties':False}}]


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def fixture(trial):
    rng = random.Random(881 + trial)
    records = []
    def add(subject, fact, number, module='2.4', **kwargs):
        record = EvidenceItem(id=f'SYN{trial}-E{number:03}', subject=subject, fact=fact,
                              module_id=module, submodule_id='2.4.3.1' if module=='2.4' else '2.2.1.1',
                              source=SourceLocation(file_id='fiction', path=Path('Inputs/fictional-lab.txt'), cell=f"A{number}"), **kwargs)
        records.append(record)
        return record.id
    a = f'{27.6 + trial:.1f}'
    sa=add('A', f'读数{a} mA，明确更正旧任务0.1 mA；旧值无效。', 1)
    sb=add('B', '未测量，未确认；不得记作合格。', 2, needs_confirmation=True)
    sc1=add('C', '同名仪器读数3.2 mA；与另一记录没有先后取代说明。', 3)
    sc2=add('C', '同名仪器读数18.0 mA；与另一记录没有先后取代说明。', 4)
    sd=add('D', '读数0 mA；零是有效测量值。', 5, value=0, unit='mA')
    expected={'A':dict(values=[a],status='abnormal',sources=[sa]),'B':dict(values=[],status='missing',sources=[sb]),
              'C':dict(values=['3.2','18.0'],status='conflict',sources=[sc1,sc2]),'D':dict(values=['0'],status='normal',sources=[sd])}
    for i in range(6, 36):
        value=f'{rng.randrange(100, 3000)/100:.2f}'
        sid=add(f'T{i}', f'读数{value} mA；虚构观测，未作现场认证。', i)
    late={'T35':dict(values=[value],status='abnormal' if float(value)>10 else 'normal',sources=[sid]),
          'Z':dict(values=['44.8'],status='abnormal',sources=[add('Z','读数44.8 mA；另一个模块的当前观测。',99,module='2.2')]),
          'W':dict(values=[],status='missing',sources=[])}
    return records, expected, late


def process_history():
    # Long but finite irrelevant process observations; no fixture answers hidden here.
    return [Message(role='assistant', content=f'过程批次{i}，已完成排序，尚未下结论。\n' +
                    '\n'.join(f'工作序号{i}-{j}：核对标题和排列；保留全部原始证据，待最终按当前任务评估。' for j in range(26)))
            for i in range(16)]


def parse(text):
    try:
        return json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
    except (ValueError, TypeError):
        return None


def score(actual, expected):
    rows = actual.get('rows', {}) if isinstance(actual,dict) else {}
    errors=[]
    for key, wanted in expected.items():
        got=rows.get(key,{})
        for field in ('values','sources','status'):
            v=got.get(field) if isinstance(got,dict) else None
            if field in ('values','sources') and isinstance(v,list):
                ok=sorted(v)==sorted(wanted[field])
            else:
                ok=v==wanted[field]
            if not ok:
                errors.append(dict(subject=key,field=field,expected=wanted[field],actual=v))
    extras=sorted(set(rows)-set(expected)) if isinstance(rows,dict) else []
    return dict(correct_fields=len(expected)*3-len(errors),total_fields=len(expected)*3,errors=errors,unexpected_subjects=extras)


class DryProvider:
    model='offline'
    async def chat_stream(self, **kwargs):
        summary=kwargs['messages'][0].content.startswith('You are the context-compaction')
        answer=dict(progress=['整理结束，未作判断'],decisions=[],constraints=[],remaining_work=['按任务核查'],critical_refs=[]) if summary else {}
        yield LLMStreamChunk(delta=dump(answer),done=True,usage={'input_tokens':0,'output_tokens':0},stop_reason='end_turn')


async def main():
    provider=DryProvider()
    if not DRY:
        config=Settings().load_config()
        route=next(c for c in config.providers.configurations if c.id==config.providers.active)
        assert route.enabled and route.provider=='anthropic' and urlsplit(route.api_base).hostname=='api.anthropic.com'
        provider=AnthropicProvider(api_key=route.api_key,api_base=route.api_base,model=route.default_model,extra_headers=route.extra_headers)
        provider.client=provider.client.with_options(timeout=httpx.Timeout(50,connect=10),max_retries=0)
    report=dict(scope='synthetic_only',baseline_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT/'manyselves',text=True).strip(),
                dirty_source=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT/'manyselves',text=True).strip()),dry_run=DRY,model=provider.model,
                design='2 paired fixtures; G1 complete facts + cumulative history, G2 task projection, G3 projection + history summary; serial rotated order',
                limits={'max_physical_attempts':26,'max_normal_requests_per_phase':2,'summary_trigger':4096,'task_max_output':2048},
                caveats=['Synthetic stress history and lower experimental threshold; not optimum for production 36K',
                         'Production context helpers and AgentLoop dispatch, scripted task phases/tool executor, not whole reporting workflow',
                         'Two trials; no statistical latency claim; request payloads lack production system-prefix cache scale'],
                attempts_started=0,lanes=[])
    def save():
        OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for trial in (0,1):
            names=['G1','G2','G3'] if trial==0 else ['G3','G2','G1']
            for name in names:
                ws=ROOT/'audit_manyselves'/('projection-preflight' if DRY else 'projection-live')/f'{trial}-{name}'
                records, expected, late=fixture(trial)
                envelope=TaskEnvelope(task_id='lab-audit',run_id='synthetic',agent_id='lab-author',objective='虚构专题审查',
                                      allowed_outputs=['lab-json'],target_submodule_ids=['2.4.3.1'])
                ReportingStore(ws).write_jsonl('Work/runs/synthetic/preparation/evidence.jsonl',[r.model_dump(mode='json') for r in records])
                packet=task_fact_packet(ws,envelope,'2.4')
                task='当前任务：核查虚构记录A、B、C、D。只依据以下当前事实。\n'+packet
                lane=dict(trial=trial,name=name,requests=[],phases=[],tool_reads=[],build_seconds=0)
                report['lanes'].append(lane)
                agent=AgentLoop(agent_type=f'probe-{name}',workspace=ws,tools=ToolRegistry(),bus=MessageBus(),llm_provider=provider,
                                config=AgentDefaults(working_memory_tokens=4096 if name=='G3' else 1000000,max_tokens=2048,
                                                     context_compaction_model=provider.model))
                agent.persist_handoff_summary=False
                agent.begin_typed_task({'task_id':'lab-audit','objective':'虚构专题审查','constraints':['仅当前事实，缺失不合格，矛盾不关闭。']})
                async def observe(messages, tools, phase, attempt):
                    if report['attempts_started']>=26:
                        raise RuntimeError('experiment_call_limit')
                    report['attempts_started']+=1
                    lane['requests'].append(dict(phase=phase,attempt=attempt,estimated_tokens=_estimate_tokens(messages),
                                                  messages=[dataclasses.asdict(m) for m in messages],tools=tools))
                    save()
                async def record(usage):
                    lane['requests'][-1]['usage_record']={k:usage.get(k) for k in ('phase','status','input_tokens','cached_input_tokens',
                        'cache_write_input_tokens','output_tokens','duration_ms','ttft_ms','usage_source','stop_reason')}
                    save()
                agent.provider_attempt_observer=observe
                agent.provider_attempt_record_observer=record
                start=time.perf_counter()
                projection=bind_task_context(agent,ws,envelope,output_contract='lab-json',render_prompt=lambda:task)
                capsule=projection.initial_message()
                if name=='G1':
                    agent.set_context_rebuilder(None)
                old=TaskContextRebuilder('obsolete-task',lambda:'旧任务历史，已失效：A读数0.1 mA，一切正常。')
                history=[Message(role='system',content=SYSTEM,cache_control=True),Message(role='user',content=old.initial_message()),
                         *process_history(),Message(role='user',content=capsule),*process_history()]
                lane['build_seconds']=time.perf_counter()-start
                lane['raw_history_estimated_tokens']=_estimate_tokens(history)
                started=time.perf_counter()
                for phase, answer_key in [('initial',expected),('late',late)]:
                    subjects=list(answer_key)
                    prompt='返回JSON：{"rows":{对象:{"values":[],"status":"...","sources":[]}},"paragraph":"附证据编号的简短中文发现"}。本轮对象为'+dump(subjects)+'。禁止以无资料作正常。'
                    history.append(Message(role='user',content=prompt))
                    result=None
                    phase_start=time.perf_counter()
                    for turn in range(2):
                        async with asyncio.timeout(70):
                            response=await agent._chat_with_retries(history,TOOLS,f'{trial}-{name}-{phase}-{turn}',reasoning_enabled=False)
                        history.append(Message(role='assistant',content=response.content or '',tool_calls=response.tool_calls))
                        if not response.tool_calls:
                            result=response
                            break
                        for call in response.tool_calls:
                            subjects=call.arguments.get('subjects',[]) if call.name=='lookup_local_fact' else []
                            found=[r.model_dump(mode='json') for r in records if r.subject in subjects]
                            missing=[s for s in subjects if not any(r.subject==s for r in records)]
                            payload=dict(records=found,missing_subjects=missing,scope='current synthetic snapshot')
                            history.append(Message(role='tool',content=dump(payload),is_tool_result=True,tool_call_id=call.id))
                            lane['tool_reads'].append(dict(phase=phase,name=call.name,subjects=subjects,returned=[r['id'] for r in found]))
                    actual=parse(result.content) if result else None
                    lane['phases'].append(dict(phase=phase,raw_response=result.content if result else None,
                                               stop_reason=result.stop_reason if result else 'experiment_turn_limit',
                                               elapsed_seconds=time.perf_counter()-phase_start,expected=answer_key,actual=actual,
                                               score=score(actual,answer_key)))
                    save()
                lane['elapsed_seconds']=time.perf_counter()-started
                lane['summary_count']=sum(r['phase']=='context_compaction' for r in lane['requests'])
                first_normal=next(r for r in lane['requests'] if r['phase']!='context_compaction')
                texts='\n'.join(m['content'] for m in first_normal['messages'])
                lane['checks']={'last_record_visible':f'SYN{trial}-E035' in texts,'stale_task_visible':'旧任务历史，已失效' in texts,
                                'fixed_packet_copies':texts.count('# 模块 2.4 项目事实清单'),
                                'facts_excluded_from_summary':all(f'SYN{trial}-E001' not in dump(r['messages']) for r in lane['requests'] if r['phase']=='context_compaction')}
                save()
                print(dump(dict(lane=name,trial=trial,requests=len(lane['requests']),summaries=lane['summary_count'],
                                elapsed=round(lane['elapsed_seconds'],3),scores=[p['score']['correct_fields'] for p in lane['phases']],checks=lane['checks'])),flush=True)
        report['completed']=True
    except Exception as exc:
        report['completed']=False
        report['error']={'type':type(exc).__name__,'http_status':getattr(exc,'status_code',None)}
        print(dump(report['error']),flush=True)
    finally:
        save()
        if not DRY:
            await provider.client.close()

if __name__=='__main__':
    asyncio.run(main())

