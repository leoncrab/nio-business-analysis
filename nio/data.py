"""Load source-backed observations into SQLite, preserving disclosed versions."""
import calendar
import csv
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CST = ZoneInfo("Asia/Shanghai")


def close_of_date(day, zone):
    next_day = date.fromisoformat(day) + timedelta(days=1)
    return int(datetime.combine(next_day, time.min, ZoneInfo(zone)).timestamp())


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def publication_clock(source):
    local = source.get("published_at_local") or ""
    if not local:
        return close_of_date(source["published_date"], source["timezone"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", local):
        raise ValueError(f"Invalid publication timestamp: {local}")
    if local[:10] != source["published_date"]:
        raise ValueError(f"Publication date mismatch: {local}")
    stamp = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(source["timezone"]))
    return int(stamp.timestamp()) + (60 if len(local)==16 else 0)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def integer(value):
    n = Decimal(value)
    if not n.is_finite() or n != n.to_integral_value():
        raise ValueError(f"Expected a whole standardized amount, got {value}")
    return int(n)


def append_only(db, table, key, values):
    """Idempotent replay, but fail if an existing identity silently changes."""
    cols = tuple(values)
    existing = db.execute(f"SELECT {','.join(cols)} FROM {table} WHERE {key}=?", (values[key],)).fetchone()
    if existing is not None:
        if tuple(existing) != tuple(values.values()):
            raise ValueError(f"Conflicting immutable identity in {table}: {values[key]}")
        return
    db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(values.values()))


def normalized_actual(r, freq, source_day):
    period, metric = r["period"], r["metric"]
    year = int(period[:4])
    month = int(period[-2:]) if freq == "monthly" else 3 * int(period[-1])
    end = date(year, month, calendar.monthrange(year, month)[1])
    if end > date.fromisoformat(source_day):
        raise ValueError(f"Period ends after publication: {period}, {source_day}")
    if freq == "monthly":
        allowed = {"Q":{"GROUP","NIO","ONVO","FIREFLY"},
                   "PassengerRetail":{"CN_PASSENGER_RETAIL"},
                   "NEVRetail":{"CN_PASSENGER_RETAIL"},
                   "PassengerWholesale":{"CN_PASSENGER_WHOLESALE"}}
        for series in ("AutoProduction","AutoSales","NEVProduction","NEVSales"):
            allowed[series] = {"CAAM_NATIONAL"}
        if r["scope"] not in allowed.get(metric,set()):
            raise ValueError(f"Invalid monthly scope for {metric}: {r['scope']}")
        scales = {"辆":1} if metric=="Q" else {"万辆":10000,"辆":1}
    else:
        if metric not in {"Q","Rv","Ro","Cv","Co","Cost","Revenue","GP","RD","SG","OI","OP"}:
            raise ValueError(f"Invalid quarterly metric: {metric}")
        scales = {"vehicles":1} if metric=="Q" else {"RMB_thousands":1000}
    unit = r["raw_unit"]
    if unit not in scales:
        raise ValueError(f"Unexpected unit for {metric}: {unit}")
    sign = -1 if metric in {"Cv","Co","Cost","RD","SG"} else 1
    expected = Decimal(r["raw_value"].replace(",","")) * scales[unit] * sign
    value = integer(r["value"])
    if value != expected or ((freq=="monthly" or metric=="Q") and value<0):
        raise ValueError(f"Invalid normalized {metric}: {value} from {r['raw_value']} {unit}")
    return value


def load(db, root=ROOT):
    db.row_factory = sqlite3.Row
    db.executescript((root / "sql/schema.sql").read_text())
    metrics = json.loads((root / "config/metrics.json").read_text())
    issues = []
    with db:
        source_lists = ["monthly_sources", "finance_sources"]
        if (root / "data/industry_sources.csv").exists():
            source_lists.append("industry_sources")
        for fname in source_lists:
            for s in read_csv(root / f"data/{fname}.csv"):
                src = (root / s["path"]).resolve()
                if not src.is_relative_to(root.resolve()) or digest(src) != s["sha256"]:
                    raise ValueError(f"Source checksum failed: {s['source_id']}")
                if not s["url"].startswith("https://") or not s["title"].strip():
                    raise ValueError(f"Invalid source URL/title: {s['source_id']}")
                s["published_at_local"] = s.get("published_at_local","")
                s["available_at"] = publication_clock(s)
                fields = ["source_id","title","url","published_date","timezone","published_at_local","available_at","path","sha256"]
                append_only(db, "source_manifest", "source_id", {k:s[k] for k in fields})
        for name, (label, unit, definition) in metrics.items():
            append_only(db, "metric_dictionary", "metric", dict(metric=name,label=label,unit=unit,definition=definition))
        dates = {r[0]:r[1] for r in db.execute("SELECT source_id,available_at FROM source_manifest")}
        source_days = dict(db.execute("SELECT source_id,published_date FROM source_manifest"))
        for freq in ["monthly","quarterly"]:
            rows = read_csv(root / f"data/{freq}.csv")
            if freq=="monthly" and "industry_sources" in source_lists:
                rows.extend(read_csv(root / "data/industry.csv"))
            for r in sorted(rows, key=lambda r:(dates[r["source_id"]],r["source_id"])):
                metric, scope = r["metric"], r.get("scope","GROUP")
                pattern = r"\d{4}-(0[1-9]|1[0-2])" if freq=="monthly" else r"\d{4}Q[1-4]"
                if not re.fullmatch(pattern,r["period"]) or not r["locator"].strip() or not r["raw_unit"].strip():
                    raise ValueError(f"Invalid period or missing locator/unit: {r}")
                if metric not in metrics:
                    raise ValueError(f"Add {metric} to config/metrics.json")
                value = normalized_actual(r,freq,source_days[r["source_id"]])
                rec_id = "/".join((freq,scope,r["period"],metric,r["source_id"]))
                row = {k:r[k] for k in ["period","metric","raw_value","raw_unit","source_id","locator"]}
                row.update(record_id=rec_id,value=value,scope=scope,revision=dates[r["source_id"]])
                append_only(db, f"{freq}_actual","record_id",row)
        for r in read_csv(root / "data/guidance.csv"):
            record = (r["period"],r["metric"],integer(r["low"]),integer(r["high"]),r["source_id"],r["locator"])
            if not re.fullmatch(r"\d{4}Q[1-4]",record[0]) or not record[-1]:
                raise ValueError(f"Invalid guidance: {record}")
            old = db.execute("SELECT * FROM company_guidance WHERE period=? AND metric=? AND source_id=?",
                             (record[0],record[1],record[4])).fetchone()
            if old is None:
                db.execute("INSERT INTO company_guidance VALUES(?,?,?,?,?,?)",record)
            elif tuple(old) != record:
                raise ValueError(f"Conflicting guidance: {record}")
        issues.extend(validate(db))
    return issues


def snapshot(db, view, as_of):
    if view not in {"monthly_records","quarterly_records"}:
        raise ValueError("Unknown view")
    return [dict(r) for r in db.execute(f"""
      SELECT * FROM (SELECT r.*, ROW_NUMBER() OVER (
        PARTITION BY period,scope,metric ORDER BY available_at DESC,revision DESC
      ) AS rank FROM {view} r WHERE available_at<=?) WHERE rank=1 ORDER BY period,scope,metric
    """,(as_of,))]


def validate(db):
    # Detailed financial tables are printed in RMB thousands. A 3-term sum
    # can differ by 2,000 RMB due to rounding of independently printed items.
    notes = []
    releases = db.execute("SELECT DISTINCT period,available_at FROM quarterly_records ORDER BY available_at,period")
    for period, at in releases:
        # A restatement can change a single line of a financial statement.
        # Reconcile the combined statement actually visible at that instant.
        v = {r["metric"]: r["value"] for r in snapshot(db,"quarterly_records",at) if r["period"]==period}
        source = f"as_of={at}"
        equations = [('Revenue',{'Rv':1,'Ro':1}),('Cost',{'Cv':1,'Co':1}),
                     ('GP',{'Revenue':1,'Cost':-1}),('OP',{'GP':1,'RD':-1,'SG':-1,'OI':1})]
        for lhs,terms in equations:
            if lhs not in v or not terms.keys()<=v.keys():
                raise ValueError(f"{period} {source} {lhs} 缺少完整分项")
            residual = v[lhs]-sum(v[k]*w for k,w in terms.items())
            if abs(residual) > 500*(len(terms)+1):
                raise ValueError(f"{period} {source}: {lhs} residual={residual} RMB")
        if 'Q' in v:
            y, q = period.split('Q')
            months = {f"{y}-{m:02}" for m in range(3*int(q)-2,3*int(q)+1)}
            group = [r["value"] for r in snapshot(db,"monthly_records",at)
                     if r["period"] in months and r["scope"]=="GROUP" and r["metric"]=="Q"]
            if len(group)==3 and sum(group)!=v['Q']:
                raise ValueError(f"{period}: monthly deliveries disagree with {source}")
    brand_sets = defaultdict(dict)
    for r in db.execute("SELECT period,source_id,scope,value FROM monthly_actual WHERE metric='Q'"):
        brand_sets[r['period'],r['source_id']][r['scope']] = r['value']
    for (period,source),values in brand_sets.items():
        if 'GROUP' not in values:
            continue
        reported = sum(n for k,n in values.items() if k!='GROUP')
        expected = {'NIO','ONVO'} if '2024-09'<=period<'2025-04' else {'NIO','ONVO','FIREFLY'}
        if reported>values['GROUP'] or (expected<=values.keys() and reported!=values['GROUP']):
            raise ValueError(f"{period}: brand deliveries disagree with {source}")
    return notes
