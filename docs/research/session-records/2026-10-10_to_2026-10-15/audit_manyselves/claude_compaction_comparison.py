"""Live synthetic-only fixed-trajectory comparison; never loads customer inputs."""
import asyncio
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'manyselves'))
import httpx
from loguru import logger
from manyselves.config.schema import AgentDefaults, Settings
from manyselves.runtime.loops.agent_loop import AgentLoop, _estimate_tokens
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message, LLMToolCall
from manyselves.runtime.tools.registry import ToolRegistry

logger.disable('manyselves')
SELECTED = sys.argv[1] if len(sys.argv) > 1 else 'all'
OUT = ROOT / ('audit_manyselves/claude_compaction_comparison.json' if SELECTED == 'all'
              else f'audit_manyselves/claude_compaction_comparison_postfix_{SELECTED}.json')
SYSTEM = ('This is a fictional laboratory record audit, not a real safety assessment. '
          'Use supplied evidence only. A later explicit correction supersedes the old value. '
          'Missing observations never establish a pass. Preserve exact values and source identifiers. '
          'All supplied records can be relevant to later questions. Return only the requested JSON; '
          'use null when the evidence is no longer available, never invent a value or citation.')


def fixtures():
    rng = random.Random(73129)
    batches, lookup = [], {}
    for batch in range(1, 11):
        rows = []
        for row in range(1, 101):
            rid = f'LAB-{batch:02}-{row:03}'
            item = dict(source=rid, instrument=f'fictional-{batch:02}-{row:03}',
                        reading=f'{rng.randrange(1000, 9900) / 100:.2f}', unit='test_unit',
                        status='observation_only', note='Synthetic bench observation; no compliance limit or real installation is represented.')
            lookup[rid] = item
            rows.append(item)
        updates = []
        if batch == 1:
            updates = [dict(source='EX-A17', sample='A17', confirmation='missing', conclusion='unresolved'),
                       dict(source='REV-OLD', sample='B29', reading='18.4', unit='test_unit')]
        if batch == 4:
            updates = [dict(source='REV-NEW', sample='B29', reading='27.6', unit='test_unit',
                            supersedes='REV-OLD', instruction='Explicit correction: old 18.4 is invalid.')]
        batches.append(json.dumps(dict(batch=batch, updates=updates, observations=rows), separators=(',', ':')))
    return batches, lookup


def parse_json(text):
    text = (text or '').strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[-1].rsplit('\n', 1)[0]
    try:
        return json.loads(text)
    except ValueError:
        # Record raw output too: prose around JSON is a format issue, not a fact error.
        start = text.find('{')
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(text[start:])[0]
            except ValueError:
                pass
        return None


async def main():
    config = Settings().load_config()
    route = next(c for c in config.providers.configurations if c.id == config.providers.active)
    assert route.enabled and route.provider == 'anthropic'
    assert urlsplit(route.api_base).hostname == 'api.anthropic.com'
    provider = AnthropicProvider(api_key=route.api_key, api_base=route.api_base, model=route.default_model)
    provider.client = provider.client.with_options(timeout=httpx.Timeout(50, connect=10), max_retries=0)
    batches, lookup = fixtures()
    report = dict(started_utc=datetime.now(timezone.utc).isoformat(), model=provider.model,
                  scope='synthetic_only', design='one trial, rotating serial order, fixed tool trajectory; real model answers retained',
                  limitations=['English synthetic data, not production reports', 'one trial; no statistical latency conclusion',
                               'cross-lane provider cache reuse may affect results', 'thresholds use runtime heuristic tokens, not provider tokens'],
                  fixture_characters=sum(map(len, batches)), lanes={})
    states = {}
    for name, threshold in [('none', 1000000), ('12k', 12000), ('36k', 36000)]:
        if SELECTED not in ('all', name):
            continue
        agent = AgentLoop(agent_type=f'comparison-{name}', workspace=ROOT / f'audit_manyselves/comparison-{name}',
                          tools=ToolRegistry(), bus=MessageBus(), llm_provider=provider,
                          config=AgentDefaults(working_memory_tokens=threshold, max_tokens=4096,
                                               context_compaction_model=route.default_model))
        agent.persist_handoff_summary = False
        agent.begin_typed_task(dict(task_id='synthetic-audit', objective='Audit fictional laboratory observations with exact source citations',
                                   constraints=['All data are synthetic.', 'A17 missing confirmation is unresolved, never a pass.',
                                                'Retain corrections and recover exact observations when needed.']))
        lane = dict(threshold_estimated_tokens=threshold, requests=[], rounds=[], summaries=[])
        report['lanes'][name] = lane
        states[name] = dict(agent=agent, history=[Message(role='system', content=SYSTEM),
                                              Message(role='user', content='Audit fictional laboratory records as they arrive.')], lane=lane)

    def save():
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    async def ask(state, messages, phase, max_tokens=512):
        start = time.perf_counter()
        response = await provider.chat(messages, max_tokens=max_tokens, reasoning_enabled=False)
        state['lane']['requests'].append(dict(phase=phase, elapsed_seconds=round(time.perf_counter()-start, 3),
                                             usage=response.usage, stop_reason=response.stop_reason, raw_response=response.content))
        return response

    try:
        for step, batch in enumerate(batches, 1):
            names = list(states)
            names = names[step % 3:] + names[:step % 3]
            for name in names:
                state = states[name]
                history, agent, lane = state['history'], state['agent'], state['lane']
                history.extend([Message(role='assistant', content='', tool_calls=[LLMToolCall(id=f'batch-{step}', name='read_synthetic_records', arguments={'batch':step})]),
                                Message(role='tool', content=batch, is_tool_result=True, tool_call_id=f'batch-{step}')])
                expected = {'a17_status':'unresolved', 'a17_source':'EX-A17', 'b29_value':'18.4' if step < 4 else '27.6',
                            'b29_source':'REV-OLD' if step < 4 else 'REV-NEW'}
                history.append(Message(role='user', content='Return JSON with a17_status, a17_source, b29_value (string), b29_source. Use the latest available evidence.'))
                before, seq = _estimate_tokens(history), agent._compaction_sequence
                start = time.perf_counter()
                async with asyncio.timeout(90):
                    history = await agent._compact_working_memory_async(history)
                duration = round(time.perf_counter()-start, 3)
                if agent._compaction_sequence > seq:
                    usage = agent._last_usage_record or {}
                    lane['summaries'].append(dict(step=step, elapsed_seconds=duration, before_estimated_tokens=before,
                                                 after_estimated_tokens=_estimate_tokens(history), summary=agent.handoff_summary,
                                                 usage={k:usage.get(k) for k in ['input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens','total_tokens']}))
                response = await ask(state, history, 'ordinary')
                actual = parse_json(response.content)
                errors = [k for k,v in expected.items() if not isinstance(actual,dict) or actual.get(k) != v]
                lane['rounds'].append(dict(step=step, expected=expected, actual=actual, errors=errors,
                                           estimated_input_tokens=_estimate_tokens(history)))
                history.append(Message(role='assistant', content=response.content or ''))
                state['history'] = history
                save()
                print(json.dumps(dict(lane=name, step=step, errors=errors, summaries=len(lane['summaries']))), flush=True)
        ids = ['LAB-01-073', 'LAB-03-041', 'LAB-06-088']
        for name, state in states.items():
            history = state['history']
            prompt = 'Return a JSON object mapping each source ID to its exact reading string: ' + ', '.join(ids) + '. If unavailable in current context use null. Do not estimate.'
            history.append(Message(role='user', content=prompt))
            response = await ask(state, history, 'late_detail_probe')
            actual = parse_json(response.content)
            expected = {rid:lookup[rid]['reading'] for rid in ids}
            state['lane']['late_detail'] = dict(expected=expected, actual=actual,
                wrong=[rid for rid in ids if isinstance(actual,dict) and actual.get(rid) is not None and actual[rid] != expected[rid]],
                unavailable=[rid for rid in ids if not isinstance(actual,dict) or actual.get(rid) is None],
                correct=[rid for rid in ids if isinstance(actual,dict) and actual.get(rid) == expected[rid]])
            # Explicit exact reread tests recovery, separately from unaided summary retention.
            history.append(Message(role='assistant', content=response.content or ''))
            history.extend([Message(role='assistant', content='', tool_calls=[LLMToolCall(id='recover', name='read_synthetic_records', arguments={'source_ids':ids})]),
                            Message(role='tool', content=json.dumps([lookup[rid] for rid in ids]), is_tool_result=True, tool_call_id='recover'),
                            Message(role='user', content=prompt)])
            recovered = await ask(state, history, 'explicit_reread_probe')
            state['lane']['after_reread'] = dict(actual=parse_json(recovered.content), expected=expected,
                                                all_correct=parse_json(recovered.content) == expected)
            save()
            print(json.dumps(dict(lane=name, late_detail=state['lane']['late_detail'], recovered=state['lane']['after_reread']['all_correct'])), flush=True)
        report['completed'] = True
    except Exception as exc:
        report['completed'] = False
        report['error'] = dict(error_class=type(exc).__name__, http_status=getattr(exc,'status_code',None))
        print(json.dumps(report['error']), flush=True)
    finally:
        save()
        await provider.client.close()


if __name__ == '__main__':
    asyncio.run(main())
