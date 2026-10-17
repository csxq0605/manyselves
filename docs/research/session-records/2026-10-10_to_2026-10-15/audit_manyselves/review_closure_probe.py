"""Synthetic-only reviewer/submit_result/acceptance probe; at most 12 API calls.

Production preparation, role prompt, submission tools and acceptance are used.
The short request/tool loop is scripted, not a complete report workflow.
"""
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'manyselves'))
import httpx
from loguru import logger
from manyselves.config.schema import Settings
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message
from manyselves.runtime.loops.bus import MessageBus
from manyselves.kernel.definitions import DefinitionKind
from manyselves.capabilities.distribution_reporting import load_distribution_reporting_capability
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord, ModuleReviewFinding, ModuleReviewFindingSubmission,
    ModuleReviewCoverage, ModuleSubmission, RevisionResponse, ModuleReviewVerdictSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext, DeclarativeModuleReviewAgentResult,
    DeclarativeModuleRecheckAgentResult,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import prepare_current_module_review
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import accept_current_module_review
from manyselves.capabilities.distribution_reporting.runtime.module_recheck_tools import prepare_current_module_recheck, accept_current_module_recheck
from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import ModuleReviewerAgentBridge
from manyselves.capabilities.distribution_reporting.runtime.module_provider import build_module_provider_tools, ModuleProviderDependencies
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

logger.disable('manyselves')
OUT = ROOT / 'audit_manyselves/review_closure_live.json'
TARGET = '2.4.1.1'
RULE = '完全虚构的实验规则，无真实设施含义：工况 A 读数 >10 mA 为异常，<=10 正常；工况 B 读数 >20 mA 为异常，<=20 正常。比较必须保持对象、单位、时点、工况一致。'


def fixture(ws, case):
    run_id = 'synthetic-closure'
    store = ReportingStore(ws)
    ledger = SourceLedger(ws, run_id)
    ledger.register_project('E-0001', '虚构实验记录', 'Inputs/fiction.txt', '对象 T，2026-01-01，工况 A，读数 16.58 mA。')
    ledger.register_project('E-0002', '虚构实验记录 B', 'Inputs/fiction.txt', '对象 T，2026-01-01，工况 B，读数 16.58 mA。')
    kref = f'Work/runs/{run_id}/context/module-2.4-knowledge.md'
    path = ws / kref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'# 虚构依据\n## {TARGET} 条件\n{RULE}\n', encoding='utf-8')
    old = '对象 T，2026-01-01，工况 A 读数 16.58 mA，结论正常。'
    claim = ClaimRecord(id='C-closure', module_id='2.4', submodule_id=TARGET,
                        text=old, claim_type='project_fact', source_ids=['E-0001'])
    baseline = ModuleSubmission(module_id='2.4', submodule_narratives={
        s: (old + ' [[CLAIM:C-closure]]' if s == TARGET else '本实验未评估此小节。')
        for s in REPORT_TAXONOMY['2.4'].submodules
    }, claims=[claim], source_ids=['E-0001'], unresolved_questions=[], revision=0)
    state = {'run_id': run_id, 'revision_targets': [TARGET], 'module_knowledge_refs': {'2.4': kref}}
    context = prepare_current_module_review(DeclarativeModuleRuntimeLaneContext(
        module_id='2.4', workflow_id='synthetic-closure', reporting_state=state,
        status='authored', module=baseline,
    ), store=store)
    finding = ModuleReviewFinding(
        id='M-2.4-initial-r0-001', target_submodule_id=TARGET, category='conclusion', impact='blocking',
        observation='工况 A 的读数为 16.58 mA，超过虚构规则 10 mA，但正文写成正常。',
        evidence_refs=['E-0001', kref],
        required_change='按工况 A 的原始记录与虚构适用规则修正结论，不能只修改响应而不修改正文。',
        reviewer_checks=['核对工况 A 的正文结论应为异常，数值必须仍为 16.58 mA。', '如涉及工况 B，应独立按 B 的规则判断，不得把不同工况视为同一次观测冲突。'],
    )
    context = accept_current_module_review({'context': context, 'result': DeclarativeModuleReviewAgentResult(
        status='completed', submission=ModuleReviewFindingSubmission(coverage=ModuleReviewCoverage(submodule_ids=[TARGET]), findings=[finding]),
    )}, store=store)
    finding = context.review.acceptance.findings[0]
    current_text = old if case == 'unchanged' else old.replace('结论正常', '结论异常')
    sources = ['E-0001']
    if case == 'different_conditions':
        current_text += ' 同一对象同一时点的工况 B 读数 16.58 mA，结论正常。'
        sources.append('E-0002')
    current = baseline.model_copy(deep=True, update={
        'revision': 1,
        'submodule_narratives': {**baseline.submodule_narratives, TARGET: current_text + ' [[CLAIM:C-closure]]'},
        'claims': [claim.model_copy(update={'text': current_text, 'source_ids': sources})], 'source_ids': sources,
        'revision_responses': [RevisionResponse(finding_id=finding.id, action='implemented',
            summary='已经完整修正工况 A 的结论，所有数值与规则均已检查，请确认关闭该问题。', changed_target_ids=[TARGET])],
    })
    context.module = current
    return prepare_current_module_recheck(context, store=store), store


async def main():
    dry = '--dry-run' in sys.argv
    report = {'synthetic_only': True, 'mode': 'closed_packet_recheck', 'complete_workflow': False,
              'max_api_calls': 12, 'attempts': 0, 'cases': []}
    out = OUT.with_name('review_closure_preflight.json') if dry else OUT
    def save(): out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    _, definitions = load_distribution_reporting_capability()
    agent = definitions.require(DefinitionKind.AGENT, 'evidence-auditor')
    task = definitions.require(DefinitionKind.TASK, 'module-runtime-recheck').model_copy(update={'tools': ['submit_result']})
    provider = None
    if not dry:
        config = Settings().load_config()
        route = next(c for c in config.providers.configurations if c.id == config.providers.active)
        assert route.enabled and route.provider == 'anthropic' and urlsplit(route.api_base).hostname == 'api.anthropic.com'
        provider = AnthropicProvider(api_key=route.api_key, api_base=route.api_base, model=route.default_model, extra_headers=route.extra_headers)
        provider.client = provider.client.with_options(timeout=httpx.Timeout(50, connect=10), max_retries=0)
        report['model'] = route.default_model
    try:
        for repeat in range(2):
            for name in ('unchanged', 'fixed', 'different_conditions'):
                ws = ROOT / 'audit_manyselves' / ('review-closure-dry' if dry else 'review-closure-live') / f'{repeat}-{name}'
                context, store = fixture(ws, name)
                prepared = context.recheck.prepared
                packet = prepared.review_input
                registry = build_module_provider_tools(ws, envelope=prepared.envelope, module_id='2.4',
                    session_id='synthetic-reviewer', workflow_id='synthetic-closure', bus=MessageBus(),
                    store=store, global_knowledge_root=None, tool_names=['submit_result'], expected_part_ids=[], dependencies=ModuleProviderDependencies())
                messages = [Message(role='system', content=agent.instructions), Message(role='user', content=ModuleReviewerAgentBridge._prompt(agent, task, packet, inline_context=prepared.envelope.inline_context))]
                entry = {'case': name, 'repeat': repeat, 'expected': 'open' if name == 'unchanged' else 'resolved',
                         'input': packet.model_dump(mode='json'), 'requests': []}
                report['cases'].append(entry)
                save()
                if dry:
                    assert packet.knowledge_context and packet.subject.submodule_narratives[TARGET]
                    continue
                for turn in range(2):
                    if report['attempts'] >= 12:
                        raise RuntimeError('probe_call_ceiling')
                    report['attempts'] += 1
                    start = time.perf_counter()
                    response = await provider.chat(messages, registry.get_definitions(), max_tokens=2048, reasoning_enabled=False)
                    req = {'seconds': time.perf_counter()-start, 'messages': [dataclasses.asdict(m) for m in messages],
                           'response': dataclasses.asdict(response), 'tool_results': []}
                    entry['requests'].append(req)
                    messages.append(Message(role='assistant', content=response.content or '', tool_calls=response.tool_calls))
                    for call in response.tool_calls:
                        tool = registry.get(call.name)
                        result = await tool(**call.arguments) if tool else {'error': 'unknown_tool'}
                        req['tool_results'].append(result)
                        if call.name == 'submit_result' and result.get('accepted'):
                            submission = ModuleReviewVerdictSubmission.model_validate(call.arguments)
                            accepted = accept_current_module_recheck({'context': context, 'result': DeclarativeModuleRecheckAgentResult(status='completed', submission=submission)}, store=store)
                            entry['verdict'] = submission.verdicts[0].verdict
                            entry['matches_expected'] = entry['verdict'] == entry['expected']
                            entry['acceptance_next_action'] = accepted.recheck.acceptance.next_action
                            break
                        messages.append(Message(role='tool', content=json.dumps(result, ensure_ascii=False), tool_call_id=call.id, is_tool_result=True))
                    save()
                    if 'verdict' in entry:
                        break
                    if not response.tool_calls:
                        messages.append(Message(role='user', content='请调用 submit_result 正式提交当前合同。'))
                print(json.dumps({k:v for k,v in entry.items() if k not in {'input','requests'}}, ensure_ascii=False), flush=True)
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

if __name__ == '__main__':
    asyncio.run(main())
