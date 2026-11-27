"""按披露时点重建集团经营预测。金额均为人民币元，季度格式为 YYYYQn。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import ceil, floor, fsum, isclose
from pathlib import Path
from statistics import median

MODEL_VERSION = "operating-drivers-v1.0"
MODELS = ("ma3", "seasonal", "seasonal_growth")
SCENARIOS = {"base": 0, "weak": -1, "strong": 1}
FIXED = ("Ro", "Co", "RD", "SG", "OI")
FIN_INPUTS = ("Rv", "Cv", *FIXED)
CN = timezone(timedelta(hours=8))
CONFIG = Path(__file__).resolve().parents[1] / "config" / "scenarios.json"


def shift_month(period, offset):
    index = int(period[:4]) * 12 + int(period[5:]) - 1 + offset
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def quarter(month):
    return f"{month[:4]}Q{(int(month[5:]) - 1) // 3 + 1}"


def months(period):
    first = f"{period[:4]}-{(int(period[-1]) - 1) * 3 + 1:02d}"
    return [shift_month(first, i) for i in range(3)]


def load_rows(conn, view, where, cutoff):
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {view} WHERE {where} AND available_at <= ? "
        "ORDER BY available_at, revision, record_id", (cutoff,)
    )]


def latest(rows, cutoff):
    """Caller orders by disclosure time, revision, then record ID."""
    return {(r["period"], r["metric"]): r for r in rows if r["available_at"] <= cutoff}


def op(x):
    return x["Q"] * (x["P"] - x["C"]) + x["Ro"] - x["Co"] - x["RD"] - x["SG"] + x["OI"]


def values(x):
    rv, cv = x["Q"] * x["P"], x["Q"] * x["C"]
    revenue, cost = rv + x["Ro"], cv + x["Co"]
    return {"Q": x["Q"], "Rv": rv, "Cv": cv, **{k: x[k] for k in FIXED},
            "Revenue": revenue, "Cost": cost, "GP": revenue - cost, "OP": op(x)}


def profit_bridge(a, b):
    """Symmetric two-state decomposition; residual tolerance is one RMB cent."""
    avgq = (a["Q"] + b["Q"]) / 2
    avg_margin = ((a["P"] - a["C"]) + (b["P"] - b["C"])) / 2
    effects = {"Q": (b["Q"] - a["Q"]) * avg_margin,
               "P": avgq * (b["P"] - a["P"]), "C": -avgq * (b["C"] - a["C"])}
    for k, sign in (("Ro", 1), ("Co", -1), ("RD", -1), ("SG", -1), ("OI", 1)):
        effects[k] = sign * (b[k] - a[k])
    residual = op(b) - op(a) - fsum(effects.values())
    if not isclose(residual, 0, abs_tol=0.01):
        raise ArithmeticError(f"利润桥残差超出人民币 0.01 元: {residual}")
    return effects, residual


def qty_rows(period, m, q):
    if (period, "Q") in q:
        return [q[period, "Q"]]
    return [m[p, "Q"] for p in months(period)] if all((p, "Q") in m for p in months(period)) else []


def financial_state(period, m, q):
    records = qty_rows(period, m, q)
    qty = sum(r["value"] for r in records)
    if qty <= 0 or any((period, k) not in q for k in FIN_INPUTS):
        return None
    rows = {k: q[period, k] for k in FIN_INPUTS}
    state = {"Q": qty, "P": rows["Rv"]["value"] / qty, "C": rows["Cv"]["value"] / qty,
             **{k: rows[k]["value"] for k in FIXED}}
    return state, records + list(rows.values())


def initial_origins(period, first_month, as_of):
    target = months(period)
    t0 = int(datetime.fromisoformat(target[0] + "-01").replace(tzinfo=CN).timestamp()) - 1
    if t0 <= as_of:
        yield "T0", t0
    for i, p in enumerate(target):
        if p not in first_month:
            break
        when = max(first_month[e] for e in target[:i + 1])
        # E.g., one archive disclosed two missing monthly numbers simultaneously.
        if sum(first_month.get(e, as_of + 1) <= when for e in target) == i + 1:
            yield f"T{i + 1}", when


def forecast_delivery(period, model, actual, config):
    target = months(period)
    known = sorted(p for p in actual if p <= target[-1])
    window = config["delivery_window"]
    recent = known[-window:]
    used = []
    predictions = []
    for p in target:
        if p in actual:
            value, kind = actual[p]["value"], "actual"
            used.append(actual[p])
        elif model == "ma3":
            if len(recent) != window:
                return None
            used.extend(actual[e] for e in recent)
            value, kind = sum(actual[e]["value"] for e in recent) / window, "forecast"
        else:
            lag = shift_month(p, -12)
            if lag not in actual:
                return None
            used.append(actual[lag])
            factor = 1.
            if model == "seasonal_growth":
                lags = [shift_month(e, -12) for e in recent]
                if len(recent) != window or not all(e in actual for e in lags):
                    return None
                denom = sum(actual[e]["value"] for e in lags)
                if denom <= 0:
                    return None
                used.extend(actual[e] for e in recent + lags)
                factor = sum(actual[e]["value"] for e in recent) / denom
            value, kind = actual[lag]["value"] * factor, "forecast"
        predictions.append({"period": p, "value": value, "kind": kind})
    return predictions, used


def shock(pairs, limit, cap, fallback):
    changes, inputs = [], []
    for new, old, rows in pairs:
        if old > 0:
            changes.append(abs(new / old - 1))
            inputs.extend(rows)
        if len(changes) == limit:
            break
    if not changes:
        return fallback, [], "预设压力幅度"
    return min(median(changes), cap), inputs, f"{len(changes)} 组相邻期绝对环比中位数，应用上限"


def shock_sizes(actual, states, config):
    mpairs = ((actual[p]["value"], actual[old]["value"], [actual[p], actual[old]])
              for p in sorted(actual, reverse=True)
              for old in [shift_month(p, -1)] if old in actual)
    qpairs = ((states[p][0]["P"], states[old][0]["P"], states[p][1] + states[old][1])
              for p in sorted(states, reverse=True)
              for old in [quarter(shift_month(months(p)[0], -1))] if old in states)
    return (shock(mpairs, config["monthly_change_pairs"], config["delivery_shock_cap"],
                  config["delivery_fallback_shock"]),
            shock(qpairs, config["financial_change_pairs"], config["unit_revenue_shock_cap"],
                  config["unit_revenue_fallback_shock"]))


def source_refs(records):
    return (sorted({r["record_id"] for r in records}), sorted({r["source_id"] for r in records}))


def progress(guidance, period, when, known):
    refs = {}
    for row in guidance:
        if row["period"] == period and row["available_at"] <= when:
            refs[row["metric"]] = dict(row, guidance_type="approximate_point" if row["low"] == row["high"] else "range")
    subtotal = sum(r["value"] for r in known)
    remaining = 3 - len(known)
    if "Q" in refs:
        rec = refs["Q"]
        rec["known_deliveries"] = subtotal
        rec["unknown_months"] = remaining
        for bound in ("low", "high"):
            needed = max(0, rec[bound] - subtotal)
            rec["remaining_" + bound] = needed
            rec["required_per_month_" + bound] = ceil(needed / remaining) if remaining else None
    return list(refs.values())


def sensitivity(x):
    """All cells keep Ro/Co/RD/SG/OI fixed; P changes the per-unit margin."""
    cells = []
    for qmult in (.9, 1, 1.1):
        for margin_delta in (-1000, 0, 1000):
            alt = dict(x, Q=floor(x["Q"] * qmult + .5), P=x["P"] + margin_delta)
            cells.append({"Q": alt["Q"], "unit_margin": alt["P"] - alt["C"], "OP": op(alt)})
    singles = []
    for key, step in (("Q", 1000), ("P", 1000), ("C", 1000), ("SG", 1e8)):
        alt = dict(x)
        alt[key] += step
        singles.append({"variable": key, "change": step, "op_change": op(alt) - op(x)})
    return {"single": singles, "matrix": cells}


def make_runs(period, origin, when, monthly_rows, quarterly_rows, guidance, config):
    m, q = latest(monthly_rows, when), latest(quarterly_rows, when)
    if (period, "Revenue") in q or (period, "OP") in q:
        return [], [{"period": period, "origin": origin, "reason": "目标季度财务已公开"}]
    target = months(period)
    actual = {p: r for (p, k), r in m.items() if p <= target[-1]}
    known = [actual[p] for p in target if p in actual]
    if origin.startswith("T") and len(known) != int(origin[1:]):
        return [], [{"period": period, "origin": origin, "reason": "归档披露不能重建该时点"}]
    candidates = {p for p, k in q if p < period}
    states = {p: s for p in candidates if (s := financial_state(p, m, q))}
    base_period = max(states, default=None)
    info = progress(guidance, period, when, known)
    # Record the same-quarter remaining totals from complete previous years only.
    prior_totals, prior_rows = [], []
    unknown_indices = [i for i, p in enumerate(target) if p not in actual]
    for p in sorted({quarter(month) for month in actual}):
        if p >= period or p[-1] != period[-1] or not unknown_indices:
            continue
        old = months(p)
        if all(e in actual for e in old):
            matched = [actual[old[i]] for i in unknown_indices]
            prior_rows.extend(matched)
            prior_totals.append({"period": p, "remaining_total": sum(r["value"] for r in matched)})
    (qshock, qrows, qrule), (pshock, prows, prule) = shock_sizes(actual, states, config)
    runs, skipped = [], []
    for model in MODELS:
        result = forecast_delivery(period, model, actual, config)
        if not result:
            skipped.append({"period": period, "origin": origin, "model": model, "reason": "基准历史月份不足"})
            continue
        estimate, delivery_rows = result
        for scenario, sign in SCENARIOS.items():
            pred = [{**e, "value": floor(e["value"] * (1 + sign * qshock) + .5)
                     if e["kind"] == "forecast" else e["value"]} for e in estimate]
            qty = sum(e["value"] for e in pred)
            records = delivery_rows + prior_rows
            if sign:
                records += qrows + prows
            x = {"Q": qty}
            if base_period:
                base, frows = states[base_period]
                records += frows
                x = dict(base, Q=qty, P=base["P"] * (1 + sign * pshock))
            v = values(x) if base_period else dict(x)
            record_ids, source_ids = source_refs(records)
            source_ids = sorted(set(source_ids) | {g["source_id"] for g in info})
            assumptions = {"finance_period": base_period, "delivery_method": model,
                           "delivery_shock": sign * qshock, "unit_revenue_shock": sign * pshock,
                           "delivery_shock_rule": qrule if sign else "无压力调整",
                           "unit_revenue_shock_rule": prule if sign else "无压力调整",
                           "financial_rule": "沿用最近已披露且完整的可比季度驱动；缺资料时仅预测交付"}
            signature = [MODEL_VERSION, config, period, origin, when, model, scenario, record_ids,
                         source_ids, x, pred, info, prior_totals]
            digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
            run = {"run_id": f"{period}-{origin}-{model}-{scenario}-{digest}", "period": period,
                   "origin": origin, "as_of": when, "model": model, "scenario": scenario,
                   "values": v, "drivers": x, "monthly": pred, "input_record_ids": record_ids,
                   "source_ids": source_ids, "assumptions": assumptions, "guidance": info,
                   "history_remaining": prior_totals}
            if origin == "Live" and scenario == "base" and base_period:
                run["sensitivity"] = sensitivity(x)
            runs.append(run)
    return runs, skipped


def actual_versions(period, monthly_rows, quarterly_rows, last_m, last_q):
    """Score the first public actual; retain latest eligible actual for audit."""
    relevant_q = [r for r in quarterly_rows if r["period"] == period]
    first_q = {}
    for r in relevant_q:
        first_q.setdefault(r["metric"], r)
    out = {}
    for k in ("Revenue", "OP"):
        if k in first_q:
            r, new = first_q[k], last_q[period, k]
            out[k] = (r["value"], [r], new["value"], [new])
    target = months(period)
    relevant_m = [r for r in monthly_rows if r["period"] in target]
    first_m = {}
    for r in relevant_m:
        first_m.setdefault(r["period"], r)
    candidates = [[first_q["Q"]]] if "Q" in first_q else []
    if len(first_m) == 3:
        when = max(r["available_at"] for r in first_m.values())
        # Revisions already known upon the last monthly release belong to that truth.
        as_published = latest(relevant_m, when)
        candidates.append([as_published[p, "Q"] for p in target])
    if candidates:
        records = min(candidates, key=lambda rows: max(r["available_at"] for r in rows))
        # Prefer quarterly confirmed data unless a later revision of the monthly series exists.
        revised_options = [[last_q[period, "Q"]]] if "Q" in first_q else []
        if len(first_m) == 3:
            revised_options.append([last_m[p, "Q"] for p in target])
        new = max(revised_options, key=lambda rows: max(r["available_at"] for r in rows))
        out["Q"] = (sum(r["value"] for r in records), records, sum(r["value"] for r in new), new)
    return out


def guidance_result(actual, guidance):
    if guidance is None:
        return None
    if guidance["guidance_type"] == "approximate_point":
        return {"guidance_type": "approximate_point", "delta_to_point": actual - guidance["low"],
                "source_id": guidance["source_id"]}
    return {"guidance_type": "range", "below_low_by": max(0, guidance["low"] - actual),
            "above_high_by": max(0, actual - guidance["high"]),
            "within": guidance["low"] <= actual <= guidance["high"], "source_id": guidance["source_id"]}


def score_runs(runs, by_period, config):
    series = defaultdict(dict)
    for run in runs:
        if run["origin"] != "Live":
            series[run["period"], run["origin"], run["model"]][run["scenario"]] = run
    scores = []
    for (p, origin, model), siblings in series.items():
        base = siblings["base"]
        for k, (actual, rows, revised, rev_rows) in by_period.get(p, {}).items():
            if k not in base["values"] or (origin == "T3" and k == "Q"):
                continue
            if max(r["available_at"] for r in rows) <= base["as_of"]:
                continue
            pred = base["values"][k]
            scenario_values = [r["values"][k] for r in siblings.values()]
            low, high = min(scenario_values), max(scenario_values)
            g = next((item for item in base["guidance"] if item["metric"] == k), None)
            record_ids, source_ids = source_refs(rows)
            revised_ids, revised_sources = source_refs(rev_rows)
            scores.append({
                "period": p, "origin": origin, "model": model, "metric": k, "run_id": base["run_id"],
                "forecast": pred, "actual": actual, "error": actual - pred, "split":
                    "holdout" if p >= config["holdout_start"] else "development",
                "actual_available_at": max(r["available_at"] for r in rows),
                "source_ids": source_ids, "actual_record_ids": record_ids,
                "revised_actual": revised, "revised_record_ids": revised_ids,
                "revised_source_ids": revised_sources, "scenario_low": low, "scenario_high": high,
                "in_scenario": low <= actual <= high, "guidance_comparison": guidance_result(actual, g)})
    return scores


def evaluate(scores):
    common = defaultdict(set)
    benchmarks = {}
    for row in scores:
        key = (row["period"], row["origin"], row["metric"])
        common[key].add(row["model"])
        if row["model"] == "ma3":
            benchmarks[key] = abs(row["error"])
    groups = defaultdict(list)
    failures = []
    for row in scores:
        key = (row["period"], row["origin"], row["metric"])
        row["common_sample"] = len(common[key]) == len(MODELS)
        if row["common_sample"]:
            groups[row["split"], row["origin"], row["model"], row["metric"]].append(row)
            excess = abs(row["error"]) - benchmarks[key]
            if row["model"] == "seasonal_growth" and excess > .01:
                failures.append({"period": row["period"], "origin": row["origin"],
                                 "metric": row["metric"], "run_id": row["run_id"],
                                 "excess_absolute_error_vs_ma3": excess, "error": row["error"]})
    results = []
    for (split, origin, model, metric), items in sorted(groups.items()):
        n = len(items)
        absolute = fsum(abs(s["error"]) for s in items)
        denom = fsum(abs(s["actual"]) for s in items)
        misses = sum((s["forecast"] > 0) != (s["actual"] > 0) for s in items
                     if s["actual"] != 0 and s["forecast"] != 0)
        results.append({"split": split, "origin": origin, "model": model, "metric": metric, "n": n,
                        "periods": sorted(s["period"] for s in items), "mae": absolute / n,
                        "wape": absolute / denom if metric != "OP" and denom else None,
                        "bias": fsum(s["error"] for s in items) / n,
                        "direction_misses": misses if metric == "OP" else None,
                        "scenario_coverage": sum(s["in_scenario"] for s in items) / n,
                        "mean_scenario_width": fsum(s["scenario_high"] - s["scenario_low"]
                                                    for s in items) / n})
    return results, failures


def bridge_entry(kind, period, from_period, run_id, start, end, record_ids, source_ids):
    effects, residual = profit_bridge(start, end)
    return {"kind": kind, "period": period, "from_period": from_period, "run_id": run_id,
            "start_op": op(start), "end_op": op(end), "effects": effects, "residual": residual,
            "from_drivers": start, "to_drivers": end, "source_ids": sorted(set(source_ids)),
            "input_record_ids": sorted(set(record_ids))}


def build_analysis(conn, as_of: int) -> dict:
    """Rebuild historical paper forecasts from eligible source versions and return JSON-ready data."""
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    mrows = load_rows(conn, "monthly_records", "scope='GROUP' AND metric='Q'", as_of)
    qrows = load_rows(conn, "quarterly_records", "scope='GROUP'", as_of)
    guidance = [dict(r) for r in conn.execute(
        "SELECT period, metric, low, high, source_id, available_at FROM guidance_records "
        "WHERE available_at <= ? ORDER BY available_at, source_id", (as_of,)
    )]
    first_m = {}
    for r in mrows:
        first_m.setdefault(r["period"], r["available_at"])
    last_m, last_q = latest(mrows, as_of), latest(qrows, as_of)
    current = quarter(datetime.fromtimestamp(as_of, CN).strftime("%Y-%m"))
    prev = quarter(shift_month(months(current)[0], -1))
    periods = {quarter(r["period"]) for r in mrows} | {r["period"] for r in qrows} | {current, prev}
    runs, skipped, actuals, bridges = [], [], {}, []
    for p in sorted(periods):
        if p > current or p < config["evaluation_start"]:
            continue
        planned = list(initial_origins(p, first_m, as_of))
        if p in (prev, current) and (p, "OP") not in last_q:
            planned.append(("Live", as_of))
        for origin, when in planned:
            new, missing = make_runs(p, origin, when, mrows, qrows, guidance, config)
            runs.extend(new)
            skipped.extend(missing)
        actuals[p] = actual_versions(p, mrows, qrows, last_m, last_q)

    # Actual-to-actual comparisons use versions available at the report cutoff.
    states = {p: s for p in periods if (s := financial_state(p, last_m, last_q))}
    for p in sorted(states):
        preceding = quarter(shift_month(months(p)[0], -1))
        if p < "2023Q1" or preceding not in states:
            continue
        a, arows = states[preceding]
        b, brows = states[p]
        ids, sources = source_refs(arows + brows)
        bridges.append(bridge_entry("quarter_change", p, preceding, None, a, b, ids, sources))

    scores = score_runs(runs, actuals, config)
    performance, failures = evaluate(scores)
    base_runs = {r["run_id"]: r for r in runs if r["scenario"] == "base"}
    first_states = {}
    for s in scores:
        if s["metric"] != "OP":
            continue
        p, when = s["period"], s["actual_available_at"]
        if p not in first_states:
            first_states[p] = financial_state(p, latest(mrows, when), latest(qrows, when))
        if not first_states[p]:
            continue
        b, brows = first_states[p]
        r = base_runs[s["run_id"]]
        ids, sources = source_refs(brows)
        bridges.append(bridge_entry("forecast_error", p, p, r["run_id"], r["drivers"], b,
                                    ids + r["input_record_ids"], sources + r["source_ids"]))
    return {"model_version": MODEL_VERSION, "as_of": as_of, "config": config, "runs": runs,
            "scores": scores, "performance": performance, "failures": failures,
            "bridges": bridges, "skipped": skipped}
