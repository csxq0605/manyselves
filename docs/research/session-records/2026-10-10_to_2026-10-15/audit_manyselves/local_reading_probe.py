"""Offline probes against current source. No provider, network, or project writes."""
import sys, json, asyncio, ast
from pathlib import Path
from types import SimpleNamespace
src=Path('manyselves/manyselves')
# Execute unchanged source definitions in isolation, with dependency stubs.
# This is a function-level probe, not an end-to-end runtime test.
def definition(path, name, method=None):
    tree=ast.parse((src/path).read_text(encoding='utf-8'))
    node=next(n for n in tree.body if getattr(n,'name',None)==name)
    if method: node=next(n for n in node.body if getattr(n,'name',None)==method)
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    ast.fix_missing_locations(module)
    ns={'Tool':object,'json':json,'ToolContractError':ValueError,'_sanitize_provider_visible_text':lambda x:x}
    exec(compile(module,str(path),'exec'),ns)
    return ns[method or name]
OpenArtifactTool=definition(Path('runtime/tools/artifact_tools.py'),'OpenArtifactTool')
format_result=definition(Path('runtime/loops/agent_loop.py'),'AgentLoop','_format_tool_result')
search=definition(Path('capabilities/distribution_reporting/runtime/research/project_evidence.py'),'ProjectEvidenceIndex','search')

class Gateway:
    def open(self,ref,offset=0,limit=4000):
        return SimpleNamespace(as_dict=lambda:{'content':'FACT X = 11.3 A','offset':offset,'returned':15,'truncated':False})
    def persist_internal(self,*args):return 'artifact:probe'

async def probe():
    tool=OpenArtifactTool(Gateway())
    first=await tool('dummy')
    second=await tool('dummy')
    print('reread',json.dumps({'first':first,'second':second},ensure_ascii=False))
    owner=SimpleNamespace(config=SimpleNamespace(max_tool_result_chars=6000),artifact_gateway=Gateway(),agent_type='probe')
    result=format_result(owner,{'content':'X'*8000,'next_offset':8000,'truncated':True},tool_name='open_tool_result')
    obj=json.loads(result)
    print('8000-page rewrapped',{'truncated':obj.get('truncated'),'original_chars':obj.get('original_chars'),'preview_chars':obj.get('preview_chars'),'next_offset':obj.get('next_offset')})
    p=Path('test/Work/runs/full-report-8c70828443b9499d80f8a6bda41c670c/preparation/evidence.jsonl')
    items=[]
    for line in p.read_text(encoding='utf-8').splitlines():
        item=json.loads(line);item['source']=SimpleNamespace(**item['source']);items.append(SimpleNamespace(**item))
    index=SimpleNamespace(_load_snapshot=lambda:items)
    for query in ['电涌保护器 SPD 避雷器 过电压','电涌保护装置','剩余电流 实测 占比 变压器 低压侧']:
        print('search',query,[(e.id,e.subject) for e in search(index,query,12)])
asyncio.run(probe())
