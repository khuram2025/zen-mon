from pathlib import Path
import re, html, json
from collections import Counter
from itertools import groupby
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader
import assessment_data as d

BASE=Path(__file__).resolve().parent
PDF_DIR=BASE.parent/'pdf'
PDF_DIR.mkdir(parents=True,exist_ok=True)
PDF=PDF_DIR/'ZenPlus-NPM-Assessment-and-Plan-2026-09-10.pdf'
MD=BASE/'ZenPlus-NPM-Assessment-and-Plan-2026-09-10.md'
ROOT=BASE.parent.parent
NAVY='#142C46'; TEAL='#007F86'; MUTED='#536478'; LINE='#D9E2E9'
STATUS_COLORS={'Observed':'#087F5B','Partial':'#A36205','Available / unproven':'#536C9B','Not found':'#B13B39','Unverified':'#6B5B95'}

fontpath=Path('C:/Windows/Fonts')
if (fontpath/'arial.ttf').exists():
    pdfmetrics.registerFont(TTFont('Body',str(fontpath/'arial.ttf')))
    pdfmetrics.registerFont(TTFont('BodyBold',str(fontpath/'arialbd.ttf')))
    pdfmetrics.registerFontFamily('Body',normal='Body',bold='BodyBold',italic='Body',boldItalic='BodyBold')
else:
    pdfmetrics.registerFontFamily('Body',normal='Helvetica',bold='Helvetica-Bold',italic='Helvetica-Oblique',boldItalic='Helvetica-BoldOblique')

styles={
 'title':ParagraphStyle('title',fontName='BodyBold',fontSize=29,leading=34,textColor=colors.HexColor(NAVY),spaceAfter=15),
 'subtitle':ParagraphStyle('subtitle',fontName='Body',fontSize=15,leading=21,textColor=colors.HexColor(TEAL),spaceAfter=12),
 'h1':ParagraphStyle('h1',fontName='BodyBold',fontSize=18,leading=22,textColor=colors.HexColor(NAVY),spaceBefore=15,spaceAfter=10,keepWithNext=True),
 'h2':ParagraphStyle('h2',fontName='BodyBold',fontSize=12,leading=16,textColor=colors.HexColor(TEAL),spaceBefore=12,spaceAfter=7,keepWithNext=True),
 'body':ParagraphStyle('body',fontName='Body',fontSize=10,leading=14.2,textColor=colors.HexColor(NAVY),spaceAfter=8),
 'small':ParagraphStyle('small',fontName='Body',fontSize=8.5,leading=11.8,textColor=colors.HexColor(MUTED),spaceAfter=5),
 'cell':ParagraphStyle('cell',fontName='Body',fontSize=8.7,leading=12,textColor=colors.HexColor(NAVY),spaceAfter=0),
 'th':ParagraphStyle('th',fontName='BodyBold',fontSize=8.5,leading=11,textColor=colors.white,spaceAfter=0),
}

def clean(x):
    return str(x).replace('\u2011','-').replace('\u2013','-').replace('\u2014','-').replace('\u2019',"'").replace('\u2018',"'").replace('\u201c','"').replace('\u201d','"')

def markup(s):
    s=html.escape(clean(s))
    def refs(m):
        keys=m.group(1).split()
        return '['+' '.join('<link href="'+html.escape(d.SOURCES[k][1],quote=True)+'" color="'+TEAL+'">'+k+'</link>' for k in keys)+']'
    return re.sub(r'\[((?:S\d{2}\s*)+)\]',refs,s)

def P(s,style='body',raw=False):
    return Paragraph(s if raw else markup(s),styles[style])

def refs(keys):
    return ' '.join(f'<link href="{html.escape(d.SOURCES[k][1],quote=True)}" color="{TEAL}">{k}</link>' for k in keys.split())

def table(rows,widths,header=True):
    tbl=Table(rows,colWidths=widths,repeatRows=1 if header else 0,hAlign='LEFT')
    commands=[('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,-1),0.4,colors.HexColor(LINE))]
    if header:
        commands += [('BACKGROUND',(0,0),(-1,0),colors.HexColor(NAVY)),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F4F7FA')])]
    tbl.setStyle(TableStyle(commands))
    return tbl

W=A4[0]-88
story=[]
def add(s,style='body'): story.append(P(s,style))
def heading(s): add(s,'h1')

story += [Spacer(1,26),P('ASSESSMENT / 10 SEPTEMBER 2026','small'),P(d.TITLE,'title'),P(d.SUBTITLE,'subtitle'),P(d.SCOPE,'small'),Spacer(1,16)]
callout=table([[P('<b>Revised assessment: development environment</b><br/>Assess implemented features and confirmed gaps. Empty or simulated telemetry is expected here and is not a product defect. Real-device readiness was not tested.','body',True)]],[W],False)
callout.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#E9F4F4')),('BOX',(0,0),(-1,-1),1,colors.HexColor(TEAL))]))
story += [callout,Spacer(1,16)]
for s in d.EXEC_SUMMARY[1:]: add(s)
counts=[[P(k,'th') for k in ['Capability status','Rows']]]
for status,n in d.COUNTS.items(): counts.append([P(status,'cell'),P(str(n),'cell')])
story += [table(counts,[W-58,58]),Spacer(1,6)]
add(f'{len(d.ROWS)} assessed capabilities. Counts include explicitly marked platform, add-on and extension rows. They are not a parity percentage.','small')
add('Revision 2 supersedes the original operational-risk framing following the owner\'s clarification. See scope, findings, feature matrix, implementation plan, validation gates and evidence registers below.','small')

story.append(PageBreak()); heading('1. Scope, evidence and interpretation')
for s in d.METHODOLOGY: add(s)
add('Status definitions','h2')
for k,v in d.STATUS_DEFS.items(): add(f'{k}: {v}','small')
add('Priority: P0 denotes a candidate defect requiring reproduction before acceptance; P1/P2 rank implementation depth. Validation means a test task, not a missing feature. A-H are workstreams; E is appliance UI evidence, C source evidence, S official comparator documentation.','small')

story.append(PageBreak()); heading('2. Priority findings')
for idx,(title,body) in enumerate(d.FINDINGS):
    if idx==5:
        story.append(PageBreak())
        heading('Priority findings / continued')
    story.append(KeepTogether([P(title,'h2'),P(body)]))

story.append(PageBreak()); heading('3. Feature-by-feature assessment')
add('Each row states the comparator capability, observed ZenPlus status, specific gap and recommended priority. "Not found" is limited to the inspected surfaces and scoped code review. A working control or lab reading does not certify production behavior.','small')
for category,group in groupby(d.ROWS,key=lambda r:r['category']):
    rows=[[P(category,'th'),'',''],[P('Feature / SolarWinds baseline','th'),P('Status / priority','th'),P('ZenPlus evidence and required work','th')]]
    for r in group:
        left=f'<b>{r["id"]} {markup(r["feature"])}</b><br/><br/>{markup(r["baseline"])}<br/>{markup(r["scope"])} | {refs(r["source"])}'
        mid=f'<font color="{STATUS_COLORS[r["status"]]}"><b>{markup(r["status"])}</b></font><br/><br/>{r["priority"]} | Workstream {r["epic"]}'
        right=markup(r['gap'])+f'<br/><br/><font size="7.8" color="{MUTED}">Evidence: {r["evidence"]}</font>'
        rows.append([P(left,'cell',True),P(mid,'cell',True),P(right,'cell',True)])
    mt=Table(rows,colWidths=[145,91,W-236],repeatRows=2,hAlign='LEFT')
    mt.setStyle(TableStyle([
        ('SPAN',(0,0),(-1,0)),('BACKGROUND',(0,0),(-1,0),colors.HexColor(TEAL)),
        ('BACKGROUND',(0,1),(-1,1),colors.HexColor(NAVY)),
        ('ROWBACKGROUNDS',(0,2),(-1,-1),[colors.white,colors.HexColor('#F4F7FA')]),
        ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),
        ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor(LINE))]))
    story += [mt,Spacer(1,12)]

story.append(PageBreak()); heading('4. Keep the module boundaries clear')
add('A full NPM review must include its shared platform functions, while separating other products. SolarWinds documents NPM, NTA, UDT, VNQM, NCM and IPAM as distinct NAM components. Configuration compliance, firmware and approval workflows belong to NCM. [S28 S29]')
rows=[[P('Adjacent capability','th'),P('SolarWinds scope','th'),P('Treatment in this assessment','th')]]
for cap,module,note in d.BOUNDARIES: rows.append([P(cap,'cell'),P(module,'cell'),P(note,'cell')])
story.append(table(rows,[140,90,W-230]))
add('Licensing and legacy caveats','h2')
add('Fleet Routing Insights is explicitly an Observability Self-Hosted extension. HA and scalability engines require deployment/entitlement review. Cisco RF heat maps depend on supported Cisco controllers and legacy Network Atlas; current release notes deprecate Network Atlas. Do not prioritize copying a legacy UI without checking its successor and your RF requirement. [S17 S26 S09 S04]')

story.append(PageBreak()); heading('5. Implementation plan')
add('Recommended sequence','h2')
add('Start with development fixtures and reproduction of source candidates. Implement confirmed gaps in alerting, MIBs, topology and required device workflows, then analytics and integrations. Schedule real-device interoperability, multi-site continuity and scale certification separately when infrastructure is available. Workstream windows below are provisional sequencing scenarios to re-estimate after triage.')
for note in d.PLAN_NOTES: add(note)
add('Week-1 decisions to finalize scope','h2')
for s in d.DECISIONS: add(s,'small')
add('Workstream summary','h2')
rows=[[P('Workstream','th'),P('Calendar window','th'),P('Effort','th')]]
for e in d.EPICS: rows.append([P(e['id']+' - '+e['name'],'cell'),P(e['when'],'cell'),P(e['effort'],'cell')])
story.append(table(rows,[W-190,110,80]))

for e in d.EPICS:
    story.append(Spacer(1,8))
    add(e['id']+' | '+e['name'],'h2')
    add(e['when']+' | '+e['effort']+' | Owner: '+e['owner'],'small')
    add('Dependencies: '+e['depends'],'small')
    add('Deliver: '+e['deliver'])
    add('Acceptance ('+e['id']+'): '+e['accept'])
    add('Primary feature rows: '+e['features'],'small')

story.append(PageBreak()); heading('6. Validation and release decisions')
add('These are proposed tests, not tests completed during this assessment. Run fault injection in an isolated test network or approved maintenance environment. Replace every "unverified" claim with reproducible evidence before making a parity claim.')
rows=[[P('Test','th'),P('Required proof','th'),P('Owner lane','th')]]
for ident,name,detail,lane in d.TESTS: rows.append([P(ident+' '+name,'cell'),P(detail,'cell'),P(lane,'cell')])
story.append(table(rows,[130,W-184,54]))
add('Release gates','h2')
for s in [
'Gate 1 - Development validation: deterministic fixtures reproduce or dismiss source candidates; counters, alert semantics, missing-data policy and local notification sinks pass; health and demo labels match agreed behavior.',
'Gate 2 - Real-device qualification, when equipment is available: discovery/counters and required router/switch/firewall families are certified; known traffic, faults, delivery and topology impact match reference outputs; no confirmed critical defects remain.',
'Gate 3 - Enterprise release: required wireless/controller integrations, forecasting/reporting, scale, access isolation and recovery objectives pass a sustained pilot. Publish the supported-feature/device/firmware matrix and explicit limits.',
'Full parity claim: all required baseline rows are evidenced at the agreed deployment scale. Exclusions, optional licenses, legacy features and customer-specific integrations must be explicit in the acceptance agreement.'
]: add(s)

story.append(PageBreak()); heading('7. Appliance evidence register')
add('Links reopen the live appliance and may show a different state later. This register records the assessment observations, not immutable screenshots. No secret values are included.','small')
for ident,(name,path,note) in d.EVIDENCE.items():
    url='https://192.168.8.221'+path
    story.append(KeepTogether([P(ident+' | '+name,'h2'),P('<link href="'+html.escape(url,quote=True)+'" color="'+TEAL+'">Open appliance view</link>','small',True),P(note)]))

story.append(PageBreak()); heading('8. Source corroboration register')
add('Local checkout: C:/Users/user/Documents/ZenPlus | HEAD b4256ff | version label 1.23.10. These source paths are references for engineering follow-up. Deployed binary equivalence was not verified.','small')
for ident,(name,path,note) in d.CODE.items():
    story.append(KeepTogether([P(ident+' | '+name,'h2'),P(path,'small'),P(note)]))

story.append(PageBreak()); heading('9. Official comparator sources')
add('All sources were consulted on 10 September 2026. Product-specific requirements, supported models and license restrictions in the linked pages govern their applicability. No third-party review is used as a technical authority.','small')
refrows=[[P('Reference','th'),P('Official documentation (click title to open)','th')]]
for ident,(name,url) in d.SOURCES.items():
    refrows.append([P(ident,'cell'),P('<link href="'+html.escape(url,quote=True)+'" color="'+TEAL+'">'+html.escape(name)+'</link>','cell',True)])
rt=table(refrows,[65,W-65])
rt.setStyle(TableStyle([('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3)]))
story.append(rt)

def page(canvas,doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor(LINE)); canvas.setLineWidth(.5)
    canvas.line(44,A4[1]-35,A4[0]-44,A4[1]-35)
    canvas.setFillColor(colors.HexColor(MUTED)); canvas.setFont('Body',8)
    canvas.drawString(44,A4[1]-26,'ZENPLUS / NPM CAPABILITY ASSESSMENT')
    canvas.drawRightString(A4[0]-44,A4[1]-26,'10 SEP 2026')
    canvas.line(44,36,A4[0]-44,36)
    canvas.drawString(44,24,'Development capability review | Revision 2 | v1.23.10')
    canvas.drawRightString(A4[0]-44,24,str(doc.page))
    canvas.restoreState()

doc=SimpleDocTemplate(str(PDF),pagesize=A4,rightMargin=44,leftMargin=44,topMargin=50,bottomMargin=49,title=d.TITLE+' - Assessment and Plan',author='ZenPlus assessment',subject=d.SUBTITLE)
doc.build(story,onFirstPage=page,onLaterPages=page)

def mdrefs(s):
    def sub(m): return '['+' '.join('['+k+']('+d.SOURCES[k][1]+')' for k in m.group(1).split())+']'
    return re.sub(r'\[((?:S\d{2}\s*)+)\]',sub,clean(s))

m=[f'# {d.TITLE}',f'\n{d.SUBTITLE}\n\n{d.DATE}\n\n{d.SCOPE}\n','## Executive assessment\n']
m += [mdrefs(s)+'\n' for s in d.EXEC_SUMMARY]
m += ['| Status | Rows |','|---|---:|']+[f'| {k} | {v} |' for k,v in d.COUNTS.items()]
m += [f'\n{len(d.ROWS)} capability rows. Counts are not a parity percentage.\n','## Scope and method\n']
m += [mdrefs(s)+'\n' for s in d.METHODOLOGY]
m += ['### Status definitions\n']+[f'- **{k}:** {v}' for k,v in d.STATUS_DEFS.items()]
m += ['\nP0 = candidate requiring reproduction; P1/P2 = feature-depth priority; Validation = testing task, not a gap. A-H = workstream; E = UI evidence; C = source evidence; S = SolarWinds source.\n','## Priority findings\n']
for name,note in d.FINDINGS: m += ['### '+name+'\n',mdrefs(note)+'\n']
m += ['## Feature-by-feature matrix\n']
for category,group in groupby(d.ROWS,key=lambda r:r['category']):
    m += ['### '+category+'\n','| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |','|---|---|---|---|---|']
    for r in group:
        rs=' '.join(f'[{k}]({d.SOURCES[k][1]})' for k in r['source'].split())
        m.append(f'| {r["id"]} **{r["feature"]}** | {r["baseline"]}; {r["scope"]}. {rs} | {r["status"]} | {r["gap"]} **Evidence:** {r["evidence"]}. | {r["priority"]} / {r["epic"]} |')
    m.append('')
m += ['## Module boundaries\n','| Capability | SolarWinds module | Assessment treatment |','|---|---|---|']+[f'| {a} | {b} | {c} |' for a,b,c in d.BOUNDARIES]
m += ['\n'+mdrefs('Distinct module scope: [S28 S29]. Fleet Routing Insights: [S17]. HA/scalability: [S26]. RF heat maps and Network Atlas caveats: [S09 S04].')+'\n','## Implementation plan\n']
m += [mdrefs(s)+'\n' for s in d.PLAN_NOTES]
for e in d.EPICS:
    m += ['### '+e['id']+' - '+e['name']+'\n',f'**Window:** {e["when"]}. **Effort:** {e["effort"]}. **Owner:** {e["owner"]}.\n','**Dependencies:** '+e['depends']+'\n','**Deliver:** '+e['deliver']+'\n','**Acceptance:** '+e['accept']+'\n','**Primary feature rows:** '+e['features']+'\n']
m += ['## Acceptance tests\n','These tests were not executed during the read-only assessment.\n','| ID / test | Required evidence | Workstream |','|---|---|---|']+[f'| {i} {n} | {t} | {w} |' for i,n,t,w in d.TESTS]
m += ['\n## Release gates\n','1. **Development validation:** deterministic fixtures reproduce or dismiss source candidates; alert semantics, counters, missing data and local notification sinks pass.\n','2. **Real-device qualification, when equipment is available:** required device families, known traffic, faults, delivery and topology match reference outputs; no confirmed critical defect remains.\n','3. **Enterprise:** required integrations, analytics, scale, access and recovery objectives pass a sustained pilot.\n','4. **Full parity claim:** every required baseline capability has reproducible evidence and all exclusions/entitlements are explicit.\n','## Week-1 decisions\n']+[f'- {s}' for s in d.DECISIONS]
m += ['\n## Appliance evidence register\n','These are time-of-inspection notes. Live links may change; they are not immutable screenshots.\n']
for i,(name,path,note) in d.EVIDENCE.items(): m += ['### '+i+' - '+name+'\n',f'[Appliance view](https://192.168.8.221{path})\n',note+'\n']
m += ['## Source corroboration\n','Local HEAD b4256ff; source/deployed identity was not verified.\n']
for i,(name,path,note) in d.CODE.items():
    m += ['### '+i+' - '+name+'\n',f'[{path}](<{(ROOT/path).as_posix()}>)\n',note+'\n']
m += ['## Official references\n','Consulted 10 September 2026.\n']
for i,(name,url) in d.SOURCES.items(): m.append(f'- **{i}:** [{name}]({url})')
MD.write_text('\n'.join(m),encoding='utf-8')

reader=PdfReader(PDF)
texts=[p.extract_text() or '' for p in reader.pages]
full='\n'.join(texts)
missing=[r['id'] for r in d.ROWS if r['id'] not in full]
assert not missing,missing
assert all(s in full for s in d.SOURCES), 'Missing source references'
summary={'pdf':str(PDF),'markdown':str(MD),'pages':len(reader.pages),'features':len(d.ROWS),'status_counts':dict(d.COUNTS),'page_text_lengths':[len(t) for t in texts]}
(BASE/'qa-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
