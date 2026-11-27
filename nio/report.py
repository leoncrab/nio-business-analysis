"""Generate a compact evidence packet and a brief with source-bound text."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .data import CST, snapshot

LABELS = {"Q":"集团交付量","GP":"毛利","OP":"营业利润","Revenue":"营业收入",
          "RD":"研发费用","SG":"销售及管理费用","OI":"其他经营收益净额"}
QUESTIONS = {
    "expense": "销管费用增量里，新车上市推广、人员和股权激励各占多少？需要费用明细才能区分。",
    "mix": "下一期单车收入代理值的变化有多少来自品牌组合，又有多少来自收入确认时点？",
}
ACTIONS = {
    "delivery": "下次月交付公告发布后，用实际替换该月预测，并重算剩余月份的交付要求。",
    "finance": "下次季报发布后，先核对单位收入、单位成本和销管费用，再重算预测误差桥。",
}
def dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def make_fact(period, metric, value, kind, formula, refs, sources, stamp, run_id=None):
    identity = [period,metric,value,kind,formula,refs,sources,stamp,run_id]
    return dict(fact_id="F-"+hashlib.sha256(dump(identity).encode()).hexdigest()[:12],
                label=LABELS.get(metric,metric),period=period,metric=metric,value=value,
                kind=kind,formula=formula,unit="辆" if metric=="Q" else "元",as_of=stamp,
                record_ids=refs,source_ids=sources,run_id=run_id)


def fact_packet(db, analysis):
    stamp = analysis["as_of"]
    q = {(r['period'],r['metric']):r for r in snapshot(db,'quarterly_records',stamp)}
    months = [r for r in snapshot(db,'monthly_records',stamp) if r['scope']=='GROUP' and r['metric']=='Q']
    facts = []
    if months:
        r=months[-1]
        facts.append(make_fact(r['period'],'Q',r['value'],'actual','月度集团交付公告',
                               [r['record_id']],[r['source_id']],stamp))
    live = sorted([r for r in analysis['runs'] if r['origin']=='Live' and r['model']=='ma3' and r['scenario']=='base'],
                  key=lambda r:r['period'])
    for run in live:
        for k in ['Q','Revenue','OP']:
            if k not in run['values']:
                continue
            if k=='Q' and all(e['kind']=='actual' for e in run['monthly']):
                continue
            formula = {'Q':'已知月份实际加未知月份三月均值','Revenue':'Q × P + Ro',
                       'OP':'Q × (P − C) + Ro − Co − RD − SG + OI'}[k]
            facts.append(make_fact(run['period'],k,run['values'][k],'forecast',formula,
                                   run['input_record_ids'],run['source_ids'],stamp,run['run_id']))
    bridges = [b for b in analysis['bridges'] if b['kind']=='quarter_change']
    if bridges:
        b = bridges[-1]
        for k in ['GP','RD','SG','OI','OP']:
            a,c=q.get((b['from_period'],k)),q.get((b['period'],k))
            if a and c:
                facts.append(make_fact(b['period'],k,c['value']-a['value'],'qoq_change',
                                       f"{b['period']} − {b['from_period']}",[a['record_id'],c['record_id']],
                                       sorted({a['source_id'],c['source_id']}),stamp))
    ids={s for f in facts for s in f['source_ids']}
    source_rows = [dict(r) for r in db.execute('SELECT * FROM source_manifest ORDER BY source_id') if r['source_id'] in ids]
    for s in source_rows:
        if s['available_at']>stamp:
            raise ValueError(f"Future source in brief: {s['source_id']}")
    return dict(as_of=stamp,accounting_basis='US_GAAP',scope='GROUP',facts=facts,sources=source_rows,
                assumptions=[{'run_id':r['run_id'], **r['assumptions']} for r in live])


def draft_template(packet):
    facts=packet['facts']
    live=[f for f in facts if f['kind']=='forecast']
    latest=live[-1]['period'] if live else ''
    selected=[f['fact_id'] for f in facts if f['kind']=='actual'
              or (f['period']==latest and f['kind']=='forecast' and f['metric'] in ('Q','OP'))
              or (f['kind']=='qoq_change' and f['metric'] in ('GP','SG','OP'))]
    return dict(fact_ids=selected,question_id='expense',action_id='finance')


def render_brief(packet, draft):
    # Only approved fields, references and questions are accepted. The renderer
    # supplies type, magnitude, direction and unit directly from the fact table.
    if set(draft) != {'fact_ids','question_id','action_id'}:
        raise ValueError('草稿含未约定字段')
    refs = draft['fact_ids']
    facts={f['fact_id']:f for f in packet['facts']}
    if not isinstance(refs,list) or len(refs)>7 or len(set(refs))!=len(refs) or any(i not in facts for i in refs):
        raise ValueError('事实引用无效、重复或超出七项')
    if (facts and not refs) or draft['question_id'] not in QUESTIONS or draft['action_id'] not in ACTIONS:
        raise ValueError('缺少有效问题、核对动作或事实')
    day=datetime.fromtimestamp(packet['as_of'],CST).date()
    lines=[f'# 蔚来经营简报 · {day}', '','集团口径 · US GAAP · 三个月均值基准','']
    if not refs:
        lines+=['截至该日，资料库中尚无可引用的经营事实。','']
    for i in refs:
        f=facts[i]
        sign=f['kind']=='qoq_change'
        kind={'actual':'已披露','qoq_change':'实际环比变动','forecast':'个人基准预测'}[f['kind']]
        number = f"{f['value']:,.0f} 辆" if f['unit']=='辆' else format(f['value']/1e8,'+.2f' if sign else '.2f')+' 亿元'
        lines.append(f"- {f['period']} {f['label']}（{kind}）为 **{number}**。〔{i}〕")
    lines += ['',f"**待核实**　{QUESTIONS[draft['question_id']]}",'',
              f"**下次更新**　{ACTIONS[draft['action_id']]}",'',
              '事实编号可在同目录 facts.json 中追溯至原始记录、公式和公告。']
    return '\n'.join(lines)+'\n'


def validate_draft(packet, proposed):
    template = draft_template(packet)
    try:
        text = render_brief(packet,proposed)
        status,errors = 'passed',[]
    except (ValueError,KeyError,TypeError) as exc:
        text = render_brief(packet,template)
        status,errors='template_fallback',[str(exc)]
    return text,dict(rule_status=status,errors=errors,human_status='pending',
                     checks=['结构白名单','事实编号与唯一性','数值、期间、单位与方向由事实表渲染','限定待核实问题与下次动作'])


def main():
    parser=argparse.ArgumentParser(description='核对外部 AI 选择的事实编号与问题，输出简报及审核记录')
    parser.add_argument('packet',type=Path)
    parser.add_argument('draft',type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    packet=json.loads(args.packet.read_text())
    raw=args.draft.read_text()
    try:
        draft = json.loads(raw)
    except ValueError:
        draft = None
    report,audit=validate_draft(packet,draft)
    args.out.mkdir(parents=True,exist_ok=True)
    (args.out/'original-draft.txt').write_text(raw)
    (args.out/'brief.md').write_text(report)
    (args.out/'review.json').write_text(dump(audit)+'\n')
    print(audit['rule_status'])


if __name__=='__main__':
    main()
