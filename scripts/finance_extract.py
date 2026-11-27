"""Extract NIO's quarterly RMB column and next-quarter outlook from archived HKEX filings.

Usage: python -m pip install pypdf==6.21.4
       python scripts/finance_extract.py
Verify the hash against releases.json before interpreting the PDF. Add a new dated
file and pin its hash after reviewing the statement header and outlook in the PDF.
"""
from __future__ import annotations

import csv
from hashlib import sha256
import json
import logging
from pathlib import Path
import re

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw" / "financials"
FIELDS = {
    "quarterly": "period metric value raw_value raw_unit source_id locator",
    "guidance": "period metric low high source_id locator",
    "finance_sources": "source_id title url published_date timezone path sha256 published_at_local",
}
NEGATE = {"Cost", "Cv", "Co", "RD", "SG"}
LABELS = {
    "Total revenues": "Revenue",
    "Total cost of sales": "Cost",
    "Gross profit": "GP",
    "Research and development": "RD",
    "Selling, general and administrative": "SG",
}
QUARTERS = ["first", "second", "third", "fourth"]
QUARTER_END = ["March 31", "June 30", "September 30", "December 31"]
# The pre-listing periods use a later comparative column, with its actual
# filing date. Do not attribute these records to a 2021 publication.
COMPARATIVES = {
    "2021Q4": ("2021Q3", 1),
    "2022Q1": ("2021Q1", 0),
    "2022Q2": ("2021Q2", 0),
}


def as_integer(cell):
    return int(cell.replace(",", "").replace("(", "-").replace(")", ""))


def balance(values):
    """Rounded disclosures in these filings reconcile exactly in thousands."""
    expected = {
        "Revenue": values["Rv"] + values["Ro"],
        "Cost": values["Cv"] + values["Co"],
        "GP": values["Revenue"] - values["Cost"],
        "OP": values["GP"] - values["RD"] - values["SG"] + values["OI"],
    }
    for name, calculated in expected.items():
        if calculated != values[name]:
            raise ValueError(f"{name} does not reconcile: {values[name]} vs {calculated}")


def financial_rows(pages, period, source, column=2):
    year, quarter = map(int, period.split("Q"))
    table_pages = [(n, p) for n, p in enumerate(pages, 1)
                   if "Three Months Ended" in p and "Cost of sales:" in p
                   and "Research and development" in p and "Comprehensive" in p]
    if len(table_pages) != 1:
        raise ValueError(f"{source}: ambiguous quarterly statement")
    page_no, page = table_pages[0]
    header = page.split("Revenues:")[0]
    ends = re.findall(r"(?:Mar(?:ch)? 31|June 30|September 30|December 31)", header)
    years = re.findall(r"20\d\d", header)
    if (len(ends) != 4 or ends[column] != QUARTER_END[quarter-1] or len(years) != 4
            or years[column] != str(year) or "thousands" not in header
            or "RMB" not in header or "US$" not in header):
        raise ValueError(f"{source}: unrecognized dates, currency or unit")
    section = ""
    rows = []
    checks = {}
    for line in page.splitlines():
        line = " ".join(line.split())
        if line in {"Revenues:", "Cost of sales:", "Operating expenses:"}:
            section = line.rstrip(":")
            continue
        match = re.fullmatch(r"(.*?)\s+((?:\(?[\d,]+\)?\s+){3}\(?[\d,]+\)?)", line)
        if not match:
            continue
        label, numbers = match.groups()
        metric = LABELS.get(label)
        if label in {"Vehicle sales", "Other sales"}:
            metric = {("Revenues", "Vehicle sales"): "Rv", ("Revenues", "Other sales"): "Ro",
                      ("Cost of sales", "Vehicle sales"): "Cv", ("Cost of sales", "Other sales"): "Co"}[(section, label)]
        elif label.startswith("Other operating"):
            metric = "OI"
        elif label.lower().endswith("from operations"):
            metric = "OP"
        if metric is None:
            continue
        raw = as_integer(numbers.split()[column])
        amount = raw * (-1 if metric in NEGATE else 1)
        if metric in checks:
            raise ValueError(f"{source}: duplicate {metric}")
        checks[metric] = amount
        rows.append([period, metric, amount * 1000, raw, "RMB_thousands", source,
                     f"PDF p.{page_no}, Three Months Ended {ends[column]} {year}, RMB, {section} / {label}"])
    balance(checks)
    return rows


def operating_rows(pages, period, source):
    year, quarter = map(int, period.split("Q"))
    compact = [" ".join(p.split()) for p in pages]
    delivery = re.compile(r"(?:Vehicle deliveries|Deliveries of vehicles) were ([\d,]+) "
                          rf"in the {QUARTERS[quarter-1]} quarter of {year}", re.I)
    for n, p in enumerate(compact, 1):
        match = delivery.search(p)
        if match:
            count = as_integer(match[1])
            row = [period, "Q", count, count, "vehicles", source, f"PDF p.{n}, Operating Highlights, deliveries"]
            break
    else:
        raise ValueError(f"{source}: no quarterly delivery count")
    q_next = quarter % 4 + 1
    y_next = year + int(quarter == 4)
    target = f"{y_next}Q{q_next}"
    text = " ".join(compact)
    outlook = re.search(rf"Business Outlook For the {QUARTERS[q_next-1]} quarter of {y_next}, "
                        r"the Company expects:(.*?)This business outlook", text, re.I)
    if not outlook:
        raise ValueError(f"{source}: no outlook for {target}")
    bounds = {
        "Q": re.search(r"vehicles to be between ([\d,]+) and ([\d,]+)", outlook[1], re.I),
        "Revenue": re.search(r"Total revenues to be between RMB\s*([\d,]+) million"
                             r"[^)]*\) and RMB\s*([\d,]+) million", outlook[1], re.I),
    }
    guidance = []
    for metric, found in bounds.items():
        if not found:
            raise ValueError(f"{source}: missing {metric} range")
        low, high = map(as_integer, found.groups())
        multiplier = 1000000 if metric == "Revenue" else 1
        page_no = next(n for n, p in enumerate(compact, 1) if found[0][:25].lower() in p.lower())
        guidance.append([target, metric, low * multiplier, high * multiplier, source,
                         f"PDF p.{page_no}, Business Outlook, {target}, {metric}"])
    return row, guidance


def comparative_quantity(pages, period, source):
    for n, page in enumerate(pages, 1):
        table = re.findall(r"((?:20\d\d\s*Q[1-4]\s+){4})Deliveries\s+"
                           r"([\d,]+\s+[\d,]+\s+[\d,]+\s+[\d,]+)", page)
        for header, cells in table:
            periods = [label.replace(" ", "") for label in re.findall(r"20\d\d *Q[1-4]", header)]
            if period in periods:
                count = as_integer(cells.split()[periods.index(period)])
                return [period, "Q", count, count, "vehicles", source,
                        f"PDF p.{n}, Operating Highlights, quarterly deliveries history, {period}"]
    raise ValueError(f"{source}: historical delivery column {period} missing")


def updated_outlook(page, period, source):
    text = " ".join(page.split())
    match = re.search(r"expects to deliver (approximately )?([\d,]+)(?: to ([\d,]+))? vehicles "
                      r"in the (\w+) quarter of (20\d\d)", text)
    if not match or period != f"{match[5]}Q{QUARTERS.index(match[4])+1}":
        raise ValueError(f"{source}: unrecognized updated guidance")
    low, high = as_integer(match[2]), as_integer(match[3] or match[2])
    kind = "approximate point" if match[1] else "range"
    return [period, "Q", low, high, source, f"PDF p.1, updated delivery outlook, {kind}"]


def main():
    releases = json.loads((RAW / "releases.json").read_text("utf-8"))
    outputs = {table: [] for table in FIELDS}
    for release in releases:
        period = release["period"]
        path = RAW / release["file"]
        digest = sha256(path.read_bytes()).hexdigest()
        if digest != release["sha256"]:
            raise ValueError(f"Source snapshot changed: {path.name}")
        pages = [p.extract_text(extraction_mode="layout") for p in PdfReader(path).pages]
        source = f"HKEX-FIN-{period}"
        if release.get("kind") == "delivery_outlook_update":
            source = f"HKEX-OUTLOOK-{period}"
            outputs["guidance"].append(updated_outlook(pages[0], period, source))
        else:
            quantity, guidance = operating_rows(pages, period, source)
            outputs["quarterly"].append(quantity)
            outputs["quarterly"].extend(financial_rows(pages, period, source))
            outputs["guidance"].extend(guidance)
            if period in COMPARATIVES:
                prior, col = COMPARATIVES[period]
                outputs["quarterly"].append(comparative_quantity(pages, prior, source))
                outputs["quarterly"].extend(financial_rows(pages, prior, source, col))
        outputs["finance_sources"].append([source, release["title"], release["url"],
                                          release["published_date"], release["timezone"],
                                          path.relative_to(ROOT).as_posix(), digest,
                                          release["published_at_local"]])
    for table, records in outputs.items():
        path = DATA / f"{table}.csv"
        temp = path.with_suffix(".tmp")
        with temp.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(FIELDS[table].split())
            writer.writerows(records)
        temp.replace(path)
    print(" / ".join(f"{len(records)} {name}" for name, records in outputs.items()))


if __name__ == "__main__":
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    main()
