"""Paired synthetic d0eabff/d3a011a recheck comparison, max 24 provider requests.
No checkout changes; baseline preparation delta and role body loaded from Git.
Same model/tools/output requirements. Scripted bounded loop, not full report timing.
"""
import asyncio
import dataclasses
import json
import random
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_closure_probe as p

def git_text(ref, path):
    return subprocess.run(['git', 'show', f'{ref}:{path}'], cwd=p.ROOT/'manyselves',
                          check=True, capture_output=True).stdout.decode('utf-8')

async def main():
    dry='--dry-run' in sys.argv
    out=p.ROOT/'audit_manyselves'/('review_increment_preflight.json' if dry else 'review_increment_ab.json')
    report={'baseline':'d0eabff','changed':'d3a011a','synthetic_only':True,'max_requests':24,
            'design':'2 repeats x 3 fixtures x 2 arms, seeded randomized within-pair order; no cache hint; same flat submit tool',
            'attempts':0,'cases':[]}
    def save(): out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    package='manyselves.capabilities.distribution_reporting.runtime'
    ns={'__name__':package+'.baseline_delta','__package__':package}
    exec(compile(git_text('d0eabff', 'manyselves/capabilities/distribution_reporting/runtime/module_review_delta.py'), 'baseline_delta', 'exec'),ns)
    _, defs=p.load_distribution_reporting_capability()
    new_agent=defs.require(p.DefinitionKind.AGENT,'evidence-auditor')
    old_agent=new_agent.model_copy(update={'instructions':git_text('d0eabff','manyselves/capabilities/distribution_reporting/agents/evidence-auditor.md').split('---',2)[2].strip()})
    task=defs.require(p.DefinitionKind.TASK,'module-runtime-recheck').model_copy(update={'tools':['submit_result']})
    provider=None
    if not dry:
        cfg=p.Settings().load_config()
        route=next(c for c in cfg.providers.configurations if c.id==cfg.providers.active)
        assert route.provider=='anthropic' and urlsplit(route.api_base).hostname=='api.anthropic.com'
        provider=p.AnthropicProvider(api_key=route.api_key,api_base=route.api_base,model=route.default_model,extra_headers=route.extra_headers)
        provider.client=provider.client.with_options(timeout=p.httpx.Timeout(50,connect=10),max_retries=0)
        report['model']=route.default_model
    rng=random.Random(1281)
    try:
        for repeat in range(2):
            for name in ('unchanged','fixed','different_conditions'):
                arms=['before','after'];rng.shuffle(arms)
                for arm in arms:
                    ws=p.ROOT/'audit_manyselves'/('increment-dry' if dry else 'increment-live')/f'{repeat}-{name}-{arm}'
                    context,store=p.fixture(ws,name)
                    prepared=context.recheck.prepared
                    packet=prepared.review_input
                    agent=new_agent if arm=='after' else old_agent
                    if arm=='before':
                        baseline=p.ModuleSubmission.model_validate_json((ws/packet.baseline_subject_ref).read_text(encoding='utf-8'))
                        delta=ns['build_module_recheck_delta'](baseline,context.module,set(packet.required_submodule_ids))
                        active=delta.pop('relevant_evidence_ids') | {r for f in packet.required_findings for r in f.evidence_refs if r.startswith('E-')}
                        packet=type(packet).model_validate({**packet.model_dump(mode='python'), **delta,
                            'knowledge_ref':None,'knowledge_context':'','evidence':[e for e in packet.evidence if e.evidence_id in active]})
                        store.write_json(prepared.review_input_ref,packet.model_dump(mode='json'))
                    tools=p.build_module_provider_tools(ws,envelope=prepared.envelope,module_id='2.4',
                        session_id='paired',workflow_id='paired',bus=p.MessageBus(),store=store,
                        global_knowledge_root=None,tool_names=['submit_result'],expected_part_ids=[],dependencies=p.ModuleProviderDependencies())
                    messages=[p.Message(role='system',content=agent.instructions),p.Message(role='user',content=p.ModuleReviewerAgentBridge._prompt(agent,task,packet,inline_context=None))]
                    entry={'repeat':repeat,'case':name,'arm':arm,'expected':'open' if name=='unchanged' else 'resolved',
                           'request_chars':sum(len(m.content) for m in messages),'requests':[]}
                    report['cases'].append(entry);save()
                    if dry:continue
                    for _ in range(2):
                        assert report['attempts']<24
                        report['attempts']+=1
                        start=time.perf_counter()
                        response=await provider.chat(messages,tools.get_definitions(),max_tokens=2048,reasoning_enabled=False)
                        req={'seconds':time.perf_counter()-start,'messages':[dataclasses.asdict(m) for m in messages],
                             'response':dataclasses.asdict(response),'tool_results':[]}
                        entry['requests'].append(req)
                        messages.append(p.Message(role='assistant',content=response.content or '',tool_calls=response.tool_calls))
                        for call in response.tool_calls:
                            result=await tools.get(call.name)(**call.arguments)
                            req['tool_results'].append(result)
                            if result.get('accepted'):
                                payload=json.loads((ws/result['result_path']).read_text(encoding='utf-8'))['payload']
                                sub=p.ModuleReviewVerdictSubmission.model_validate(payload)
                                entry['verdict']=sub.verdicts[0].verdict
                                entry['correct']=entry['verdict']==entry['expected']
                                break
                            messages.append(p.Message(role='tool',content=json.dumps(result,ensure_ascii=False),tool_call_id=call.id,is_tool_result=True))
                        save()
                        if 'verdict' in entry:break
                        if not response.tool_calls:messages.append(p.Message(role='user',content='请调用 submit_result 正式提交。'))
                    print(json.dumps({k:v for k,v in entry.items() if k!='requests'},ensure_ascii=False),flush=True)
        report['completed']=True
    except Exception as exc:
        report['error']={'type':type(exc).__name__,'status':getattr(exc,'status_code',None)}
        if dry:raise
        print(json.dumps(report['error']),flush=True)
    finally:
        save()
        if provider:await provider.client.close()

if __name__=='__main__':asyncio.run(main())
