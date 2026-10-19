"""Recompute PR 6 evidence from archived runs without Provider calls or raw prose export.

Run from the repository root:
  PYTHONPATH=. .venv/bin/python scripts/benchmarks/reporting_scope_audit.py --output /tmp/scope.json

Historical counters and synthetic experiments are separate populations. This
script does not estimate current-report latency, false-positive rates or savings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    WriteResultPartTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossSynthesisInput,
    SourceRecord,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.task_context import bind_task_context
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message
from manyselves.runtime.tools.registry import ToolRegistry
from scripts.benchmarks.report_writing_architectures import score

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves'


def read(name):
    return json.loads((ARCHIVE / name).read_text(encoding='utf-8'))


def aggregate(segments):
    return {
        phase: {
            key: sum(segment[phase][key] for segment in segments)
            for key in ('requests', 'input_sum', 'output_sum', 'provider_active_seconds_sum')
        }
        for phase in ('normal', 'summary')
    }


async def current_request_probe():
    """Exercise real context reconstruction/serialization with fictional facts only."""
    with TemporaryDirectory(prefix='scope-probe-') as directory:
        workspace = Path(directory)
        loop = AgentLoop(agent_type='author', workspace=workspace, tools=ToolRegistry(),
                         bus=MessageBus(), llm_provider=None, config=AgentDefaults())
        task = TaskEnvelope(run_id='synthetic', task_id='author', agent_id='author', objective='probe')
        # Larger than the default history budget, but authoritative state is separate.
        facts = 'Fictional exact fact E-0001 with its source and condition.\n' * 5000
        projection = bind_task_context(loop, workspace, task, output_contract='module_submission',
                                       render_prompt=lambda: facts)
        history = [Message(role='system', content='Stable synthetic instructions', cache_control=True),
                   Message(role='user', content=projection.initial_message())]
        compact_inputs = []
        original = loop._compact_working_memory_async

        async def observe(messages):
            compact_inputs.append(sum(len(m.content or '') for m in messages))
            return await original(messages)

        loop._compact_working_memory_async = observe
        converter = object.__new__(AnthropicProvider)
        converter._supports_cache = True
        writer = WriteResultPartTool(task.run_id, task.task_id, 0, ReportingStore(workspace))
        rows = []
        for number in (1, 2):
            await writer(part_id='part-1', content=f'Fictional draft revision {number}.')
            rebuilt = await projection.rebuild(history, loop=loop)
            system, messages = converter._convert_messages(rebuilt.messages)
            wire = json.dumps(messages, ensure_ascii=False)
            rows.append({'draft_revision': number, 'system_cache_markers': json.dumps(system).count('cache_control'),
                         'message_cache_markers': wire.count('cache_control'),
                         'all_facts_present': facts in rebuilt.messages[1].content,
                         'current_draft_present': f'Fictional draft revision {number}.' in wire,
                         'compactor_input_chars': compact_inputs[-1]})
        assert all(r['all_facts_present'] and r['current_draft_present'] for r in rows)
        assert max(compact_inputs) < len(facts)
        return {'facts_chars': len(facts), 'rows': rows,
                'meaning': 'Real serializer, no client/network; cache markers do not prove hits.'}


def audit():
    run = next(r for r in read('context_growth_metrics.json')
               if r['run'] == 'full-report-8c70828443b9499d80f8a6bda41c670c')
    totals = aggregate(run['segments'])
    recorded = read('runtime_metrics.json')[run['run']]
    assert sum(p['requests'] for p in totals.values()) == recorded['requests']
    for key, recorded_key in [('input_sum', 'input_tokens'), ('output_sum', 'output_tokens')]:
        assert sum(p[key] for p in totals.values()) == recorded['sums'][recorded_key]
    grouped = defaultdict(list)
    for segment in run['segments']:
        grouped[segment['agent'].split(':')[1]].append(segment)

    review_requests = [request for case in read('review_increment_ab.json')['cases']
                       for request in case['requests']]
    prose_chars = sum(len(r['response'].get('content') or '') for r in review_requests)
    args_chars = sum(len(json.dumps(c['arguments'], ensure_ascii=False, separators=(',', ':')))
                     for r in review_requests for c in r['response'].get('tool_calls', [])
                     if c['name'] == 'submit_result')
    experiments = {}
    for name in ('writing_architecture_ab', 'writing_architecture_ab_typed'):
        experiment = read(name + '.json')
        arms = defaultdict(list)
        for case in experiment['cases']:
            arms[case['arm']].append(case)
        experiments[name] = {
            arm: {
                'runs': len(cases),
                'requests': sum(len(c['requests']) for c in cases),
                'recorded_zero_error_runs': sum(not c['errors'] for c in cases),
                'rescored_zero_error_runs': sum(not score(c['result']) for c in cases),
                'temperature_named_as_rise_runs': sum(
                    bool(re.search(r'温升.{0,3}55', next((j.get('text', '') for j in c['result'].get('judgments', [])
                                                       if j.get('id', '').endswith('0004')), '')))
                    for c in cases
                ),
                'wall_mean_seconds': sum(c['wall_seconds'] for c in cases) / len(cases),
                'tokens': {
                    key: sum(r['usage'].get(key, 0) for c in cases for r in c['requests'])
                    for key in ('input_tokens', 'output_tokens', 'cached_input_tokens')
                },
            }
            for arm, cases in sorted(arms.items())
        }
    replay = read('fact_packet_replay_metrics.json')
    tasks = ROOT / 'manyselves/capabilities/distribution_reporting/tasks'
    selected_tasks = {}
    for name in ('module-2.4-authoring', 'module-runtime-initial-review', 'module-runtime-recheck'):
        definition = yaml.safe_load((tasks / f'{name}.yaml').read_text())
        selected_tasks[name] = {'tool_count': len(definition['tools']), 'tools': definition['tools']}
    agents = ROOT / 'manyselves/capabilities/distribution_reporting/agents'
    selectors = {}
    for name in ('module-2.4-specialist', 'evidence-auditor', 'cross-relation-reviewer', 'chief-editor', 'chief-editor-auditor'):
        definition = yaml.safe_load((agents / f'{name}.md').read_text().split('---', 2)[1])
        selectors[name] = {key: definition.get(key) for key in ('model', 'profile')}
    return {
        'provider_calls_this_audit': 0,
        'historical': {'run': run['run'], 'models': run['model_counts'], 'totals': totals,
                       'by_role': {key: aggregate(values) for key, values in sorted(grouped.items())}},
        'review_output_sample': {'requests': len(review_requests), 'prose_chars': prose_chars,
                                'submit_arguments_chars': args_chars,
                                'meaning': 'Separate visible outputs, not exact duplicates or token counts.'},
        'writing_experiments': experiments,
        'fact_delivery_replay': {'source_run': replay['source_run'], 'modules': replay['modules'],
                                'checks': {key: {'records': value['records'],
                                                 'keyword_probe': value['keyword_probe']}
                                           for key, value in replay['checks'].items()}},
        'current_schema': {'cross_relation_required_fields': CrossSynthesisInput.model_json_schema()['required'],
                           'source_record_fields': list(SourceRecord.model_fields)},
        'current_tasks': selected_tasks,
        'current_agent_selectors': selectors,
        'current_context_probe': asyncio.run(current_request_probe()),
        'limits': ['Historical models and orchestration differ from this PR.',
                   'Provider-active durations overlap; they are not wall-clock latency.',
                   'Fact delivery does not prove prose coverage, correct judgment or citation support.',
                   'Synthetic arm timings do not establish full-report improvement.',
                   'Recorded errors can use an older evaluator; rescored errors reuse the committed synthetic score, not a complete semantic oracle.'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'provider_calls': 0,
                      'review_output_sample': result['review_output_sample'],
                      'current_task_tools': {k: v['tool_count'] for k, v in result['current_tasks'].items()}},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
