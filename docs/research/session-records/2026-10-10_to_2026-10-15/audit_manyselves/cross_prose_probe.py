"""Synthetic-only Cross prose lookup with production prompts/tools/acceptance; <=8 calls."""
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_closure_probe as common
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import CrossOwnerRuntime, CrossOwnerAgentInvoker
from manyselves.capabilities.distribution_reporting.runtime.cross_provider import CrossProviderRuntime
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import CrossOwnerFindingSubmission
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import DeclarativeCrossOwnerInitialAgentResult
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.services import RuntimeServicesView
from manyselves.config.schema import AgentDefaults
from tests.capabilities.distribution_reporting.test_cross_provider import _state

async def main():
    dry = '--dry-run' in sys.argv
    replay = '--replay' in sys.argv
    out = common.ROOT / 'audit_manyselves' / ('cross_prose_preflight.json' if dry else 'cross_prose_replay.json' if replay else 'cross_prose_live.json')
    report = {'synthetic_only': True, 'complete_workflow': False, 'max_api_calls': 8, 'attempts': 0, 'cases': []}
    def save(): out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    _, defs = common.load_distribution_reporting_capability()
    agent = defs.require(common.DefinitionKind.AGENT, 'cross-module-reviewer')
    task = defs.require(common.DefinitionKind.TASK, 'cross-owner-runtime-initial-review')
    provider = None
    if not dry:
        config = common.Settings().load_config()
        route = next(c for c in config.providers.configurations if c.id == config.providers.active)
        assert route.enabled and route.provider == 'anthropic' and urlsplit(route.api_base).hostname == 'api.anthropic.com'
        provider = common.AnthropicProvider(api_key=route.api_key, api_base=route.api_base, model=route.default_model, extra_headers=route.extra_headers)
        provider.client = provider.client.with_options(timeout=common.httpx.Timeout(50, connect=10), max_retries=0)
        report['model'] = route.default_model
    try:
        for repeat, case in enumerate(('different_conditions', 'different_conditions') if replay else ('conflict', 'different_conditions')):
            ws = common.ROOT / 'audit_manyselves' / ('cross-prose-dry' if dry else 'cross-prose-replay' if replay else 'cross-prose-live') / f'{repeat}-{case}'
            run_id = 'synthetic-cross'
            state = _state(run_id, ws)
            for mid, module in state['module_submissions'].items():
                module.submodule_narratives = {s: '本虚构实验无此项跨模块接口。' for s in module.submodule_narratives}
            state['module_submissions']['2.1'].submodule_narratives['2.1.1'] = (
                '唯一共享对象 T：2026-01-01 工况 A 的读数为 16.58 mA，结论异常。'
                '模块 2.2 也描述对象 T，结论是否一致需按其正文核对工况。' + common.RULE)
            related = state['module_submissions']['2.2']
            condition = 'A' if case == 'conflict' else 'B'
            related.submodule_narratives[next(iter(related.submodule_narratives))] = (
                f'唯一共享对象 T：2026-01-01 工况 {condition} 的读数为 16.58 mA，结论正常。' + common.RULE)
            for mid, module in state['module_submissions'].items():
                (ws / state['module_artifact_refs'][mid]['ref']).write_text(module.model_dump_json(indent=2), encoding='utf-8')
            state['report_instruction'] = '仅审查虚构实验的已有跨模块矛盾；未评估项目不作为新增缺陷。'
            lifecycle = CrossOwnerRuntime(ws)
            context = lifecycle.prepare_initial({'state': lifecycle.prepare(state), 'owner_module_id': '2.1'})
            prep = context.preparation
            bus = common.MessageBus()
            runtime = CrossProviderRuntime(RuntimeServicesView(workspace=ws, bus=bus, active_provider=provider or object(), agent_defaults=AgentDefaults(), global_knowledge_root=None))
            deps = runtime._compose_artifact_dependencies(agent, prep.envelope, session_id='synthetic-cross-reviewer')
            tools = common.build_module_provider_tools(ws, envelope=prep.envelope, module_id='2.1', session_id='synthetic-cross-reviewer', workflow_id='synthetic-cross', bus=bus, store=lifecycle.store, global_knowledge_root=None, tool_names=task.tools, expected_part_ids=[], dependencies=deps)
            bridge = CrossOwnerAgentInvoker(ws, execution=AgentExecutionService(bus), session_factory=lambda _: None)
            messages = [common.Message(role='system', content=agent.instructions), common.Message(role='user', content=bridge._prompt(agent, task, prep))]
            entry = {'case': case, 'requests': [], 'tool_reads': []}
            report['cases'].append(entry)
            if dry:
                assert tools.get('search_text') and tools.get('open_artifact')
                continue
            for turn in range(4):
                if report['attempts'] >= 8:
                    raise RuntimeError('probe_call_ceiling')
                report['attempts'] += 1
                start = time.perf_counter()
                response = await provider.chat(messages, tools.get_definitions(), max_tokens=3072, reasoning_enabled=False)
                req = {'seconds': time.perf_counter()-start, 'messages': [dataclasses.asdict(m) for m in messages], 'response': dataclasses.asdict(response), 'tool_results': []}
                entry['requests'].append(req)
                messages.append(common.Message(role='assistant', content=response.content or '', tool_calls=response.tool_calls))
                for call in response.tool_calls:
                    tool = tools.get(call.name)
                    result = await tool(**call.arguments) if tool else {'error': 'unknown_tool'}
                    req['tool_results'].append(result)
                    if call.name in ('search_text', 'open_artifact'):
                        entry['tool_reads'].append({'tool': call.name, 'arguments': call.arguments})
                    if call.name == 'submit_result' and result.get('accepted'):
                        payload = json.loads((ws / result['result_path']).read_text(encoding='utf-8'))['payload']
                        submission = CrossOwnerFindingSubmission.model_validate(payload)
                        accepted = lifecycle.accept_initial({'context': context, 'result': DeclarativeCrossOwnerInitialAgentResult(status='completed', submission=submission)})
                        entry['findings'] = [f.model_dump(mode='json') for f in submission.findings]
                        entry['acceptance_next_action'] = accepted.acceptance.next_action
                        break
                    messages.append(common.Message(role='tool', content=json.dumps(result, ensure_ascii=False), tool_call_id=call.id, is_tool_result=True))
                save()
                if 'findings' in entry:
                    break
                if not response.tool_calls:
                    messages.append(common.Message(role='user', content='请使用本次工具完成并正式提交。'))
            print(json.dumps({k:v for k,v in entry.items() if k != 'requests'}, ensure_ascii=False), flush=True)
        report['completed'] = True
    except Exception as exc:
        report['error'] = {'type': type(exc).__name__, 'status': getattr(exc, 'status_code', None)}
        if dry:
            raise
        print(json.dumps(report['error']), flush=True)
    finally:
        save()
        if provider:
            await provider.client.close()

if __name__ == '__main__': asyncio.run(main())
