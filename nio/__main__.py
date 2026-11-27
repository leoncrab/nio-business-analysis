"""One-command offline rebuild of the SQLite model, dashboard and dated reports."""
import argparse
import json
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from .data import ROOT, close_of_date, load, append_only
from .model import build_analysis
from .report import dump, draft_template, fact_packet, validate_draft


def persist(db, analysis, packet):
    hashes = dict(db.execute("SELECT source_id,sha256 FROM source_manifest"))
    for r in analysis['runs']:
        refs=dict(record_ids=r['input_record_ids'],source_ids=r['source_ids'],
                  sha256={s:hashes[s] for s in r['source_ids']})
        assumptions=dict(drivers=r['drivers'],config=analysis['config'],assumptions=r['assumptions'])
        append_only(db,'model_assumptions','run_id',
                    dict(run_id=r['run_id'],model_version=analysis['model_version'],
                         inputs_json=dump(refs),parameters_json=dump(assumptions)))
        for metric,value in r['values'].items():
            row = (r['run_id'],r['period'],r['as_of'],r['origin'],r['model'],r['scenario'],metric,value)
            old=db.execute('SELECT * FROM forecast_vintage WHERE run_id=? AND metric=?',(r['run_id'],metric)).fetchone()
            if old is None:
                db.execute('INSERT INTO forecast_vintage VALUES(?,?,?,?,?,?,?,?)',row)
            elif tuple(old)!=row:
                raise ValueError(f"Forecast identity conflict: {r['run_id']} / {metric}")
    for f in packet['facts']:
        row={k:f[k] for k in ['fact_id','as_of','run_id','metric','period','kind','unit','value','formula']}
        row.update(record_ids=dump(f['record_ids']),source_ids=dump(f['source_ids']))
        append_only(db,'analysis_facts','fact_id',row)


def write_atomic(path, text):
    with tempfile.NamedTemporaryFile(dir=path.parent,mode='w',encoding='utf-8',delete=False) as file:
        file.write(text)
    os.replace(file.name,path)


def build(as_of, out):
    from dashboard.render import render
    out.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(out/'nio.sqlite')
    try:
        notes=load(db)
        with db:
            analysis=build_analysis(db,as_of)
            packet=fact_packet(db,analysis)
            persist(db,analysis,packet)
            draft=draft_template(packet)
            brief,audit=validate_draft(packet,draft)
            audit.update(generator='deterministic_template',quality_notes=notes)
            payload={**analysis,'brief':brief,'facts':packet['facts'],'audit':audit}
            for name,view in [('monthly','monthly_records'),('quarterly','quarterly_records'),('sources','source_manifest')]:
                payload[name]=[dict(r) for r in db.execute(f'SELECT * FROM {view} WHERE available_at<=? ORDER BY available_at, source_id',(as_of,))]
            html=render(payload)
        # Stage all artifacts before exposing them as one immutable output directory.
        import hashlib
        import shutil
        from datetime import datetime
        from .data import CST
        key=hashlib.sha256((dump(analysis)+dump(packet)+brief+html+dump(audit)).encode()).hexdigest()[:12]
        folder=out/(str(datetime.fromtimestamp(as_of,CST).date())+'-'+key)
        if not folder.exists():
            temp=Path(tempfile.mkdtemp(dir=out,prefix='.stage-'))
            try:
                files={'dashboard.html':html,'brief.md':brief,'facts.json':dump(packet),
                       'draft.json':dump(draft),'review.json':dump(audit),'analysis.json':dump(analysis)}
                for name,content in files.items():
                    (temp/name).write_text(content,encoding='utf-8')
                try:
                    temp.rename(folder)
                except OSError:
                    if not folder.exists():
                        raise
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
        write_atomic(out/'latest.txt',folder.name+'\n')
        return folder
    finally:
        db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of',default='2026-11-27',type=date.fromisoformat,
                        help='信息截止日期（北京时间当日收盘后至 23:59:59）')
    parser.add_argument('--out',type=Path,default=ROOT/'build')
    args=parser.parse_args()
    day=str(args.as_of)
    print(build(close_of_date(day,'Asia/Shanghai')-1,args.out.resolve()))
