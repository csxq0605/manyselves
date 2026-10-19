"""Offline current-code request-shape probe; fictional prose; no API calls."""
import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'manyselves'))
from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import WriteResultPartTool
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.task_context import bind_task_context
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import Message, LLMToolCall
from manyselves.runtime.tools.registry import ToolRegistry


async def main():
    workspace = ROOT / ('request-shape-1014-' + uuid.uuid4().hex[:8])
    workspace.mkdir()
    loop = AgentLoop(agent_type='author', workspace=workspace, tools=ToolRegistry(),
                     bus=MessageBus(), llm_provider=None, config=AgentDefaults())
    envelope = TaskEnvelope(task_id='author', run_id='fictional', agent_id='author', revision=0,
                            objective='synthetic request shape', allowed_outputs=['module_submission'])
    writer = WriteResultPartTool(envelope.run_id, envelope.task_id, 0, ReportingStore(workspace))
    facts = 'SYNTHETIC_FACT_17: value 17; fictional unit; source E-0017.\n' * 100
    projection = bind_task_context(loop, workspace, envelope, output_contract='module_submission',
                                   render_prompt=lambda: facts)
    history = [Message(role='system', content='offline instructions', cache_control=True),
               Message(role='user', content=projection.initial_message())]
    # Bypass client construction entirely; exercise only production serialization.
    converter = object.__new__(AnthropicProvider)
    converter._supports_cache = True
    rows = []
    for i in range(1, 5):
        part_id = f'part-{i}'
        marker = f'UNIQUE_DRAFT_MARKER_{i}'
        prose = marker + '\n' + ('Fictional working paragraph. ' * 80)
        await writer(part_id=part_id, content=prose)
        history.extend([
            Message(role='assistant', content='', tool_calls=[LLMToolCall(
                id=f'write-{i}', name='write_result_part', arguments={'part_id': part_id, 'content': prose})]),
            Message(role='tool', content='saved', is_tool_result=True, tool_call_id=f'write-{i}')])
        result = await projection.rebuild(history, loop=loop)
        system, messages = converter._convert_messages(result.messages)
        wire = json.dumps(messages, ensure_ascii=False)
        rows.append({'written_parts': i, 'message_json_chars': len(wire),
                     'current_state_chars': len(result.messages[1].content),
                     'latest_draft_occurrences': wire.count(marker),
                     'first_draft_occurrences': wire.count('UNIQUE_DRAFT_MARKER_1'),
                     'system_cache_markers': json.dumps(system).count('cache_control'),
                     'message_cache_markers': wire.count('cache_control')})
    report = {'source_head': 'cf29d06', 'model_calls': 0, 'customer_data': False,
              'measurement': 'characters and occurrence counts, not tokens or latency',
              'rows': rows}
    (ROOT / 'request_shape_metrics_1014.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


asyncio.run(main())
