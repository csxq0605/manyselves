from pathlib import Path
import json,re,zipfile,collections,hashlib
from lxml import etree
import openpyxl
R=Path(__file__).resolve().parents[1]; O=Path(__file__).parent
report=(R/'test/Outputs/Reports/配电安全专家咨询报告.md').read_text(encoding='utf-8')
ev=[json.loads(x) for x in (R/'test/Work/evidence.jsonl').read_text(encoding='utf-8').splitlines()]
wb={p.name:openpyxl.load_workbook(p,data_only=True) for p in (R/'test/Inputs').glob('*.xlsx')}
bad=[]; checked=0
for e in ev:
    s=e['source']
    if s.get('sheet') and s.get('cell'):
        try:
            cells=[wb[Path(s['path']).name][s['sheet']][r] for r in s['cell'].split(';')]
            checked+=1
        except Exception as ex: bad.append((e['id'],str(ex)))
facts=collections.defaultdict(list)
for e in ev: facts[(e['subject'],e['fact'],e['source']['path'],e['source'].get('cell'))].append(e['id'])
dups=[v for v in facts.values() if len(v)>1]
ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
p=R/'test/Outputs/Reports/配电安全专家咨询报告.docx'
with zipfile.ZipFile(p) as z:
    x=etree.fromstring(z.read('word/document.xml'))
    ft=etree.fromstring(z.read('word/footnotes.xml'))
    fnotes=[a for a in ft.xpath('//w:footnote',namespaces=ns) if int(a.get('{'+ns['w']+'}id'))>0]
    frefs=x.xpath('//w:footnoteReference/@w:id',namespaces=ns)
    heads=[(''.join(a.xpath('.//w:t/text()',namespaces=ns)),a.xpath('./w:pPr/w:pStyle/@w:val',namespaces=ns)) for a in x.xpath('//w:p',namespaces=ns) if a.xpath('./w:pPr/w:pStyle',namespaces=ns)]
    toc=x.xpath('//w:instrText/text()',namespaces=ns)
    text='\n'.join(x.xpath('//w:t/text()',namespaces=ns))
    print('docx footnotes',len(fnotes),'refs',len(frefs),'unique',len(set(frefs)), 'raw markers',re.findall(r'\[\[(?:CITE|PHOTO):.*?\]\]',text))
    print('docx standard/harmonic error retained', '符合限值，已超过推荐值' in text,'chapter4',bool(re.search(r'4\.\s*专项',text)))
    print('first footnote',''.join(fnotes[0].xpath('.//w:t/text()',namespaces=ns))[:800])
print('checked source addresses',checked,'bad',bad)
print('duplicate identical facts',len(dups),dups[:20])
print('md cites',len(re.findall(r'\[\[CITE:',report)),'photo occurrences',len(re.findall(r'\[\[PHOTO:',report)),'unique photos',len(set(re.findall(r'\[\[PHOTO:(.*?)\]\]',report))))
print('capacity',sum(wb['S4-4诊断工作用表.xlsx']['总配评估详情表'].cell(i,4).value for i in range(3,12)))
run=R/'test/Work/runs/revise-report-8129b20dfb0d4bb9b1574645447375f0'
state=json.loads((run/'report-state.json').read_text(encoding='utf-8'))
print('state keys',list(state))
for k,v in state.items():
    if 'review' in k or 'audit' in k or 'status' in k: print(k,json.dumps(v,ensure_ascii=False)[:7000])
print('hash match',hashlib.sha256(p.read_bytes()).hexdigest()==json.loads((run/'render-log.json').read_text(encoding='utf-8'))['output_sha256'])
print('superscript refs', x.xpath('//w:r[w:rPr/w:vertAlign[@w:val="superscript"]]/w:t/text()',namespaces=ns))
print('hyperlink count',len(x.xpath('//w:hyperlink',namespaces=ns)))
print('special topic state',json.dumps({k:v for k,v in state.items() if 'special' in k},ensure_ascii=False)[:2000])
(O/'source_checks.json').write_text(json.dumps({'addresses_checked':checked,'invalid':bad,'duplicates':dups,'evidence_count':len(ev)},ensure_ascii=False,indent=2),encoding='utf-8')
