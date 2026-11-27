"""Regenerate monthly.csv from the source manifest and immutable local copies.

Install pypdf==6.21.4 for PDF text extraction.
Run from any directory. No online request is made.
"""
import calendar
import csv
import logging
from decimal import Decimal
from hashlib import sha256
from html import unescape
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

DATA = Path(__file__).resolve().parents[1] / "data"
FIELDS = ["period", "metric", "value", "raw_value", "raw_unit", "source_id", "locator", "scope"]


def pdf_pages(path):
    from pypdf import PdfReader

    for number, page in enumerate(PdfReader(path).pages, start=1):
        yield f"PDF p.{number}", " ".join(page.extract_text().split())


def deliveries(path, period):
    year, month = period.split("-")
    month_name = calendar.month_name[int(month)]
    month_total = re.compile(
        rf"([\d,]+) (?:smart electric )?vehicles (?:were delivered |globally )?in {month_name} {year}\b",
        re.IGNORECASE,
    )
    brand_total = re.compile(r"([\d,]+) vehicles from [^.,;]{0,85}?\b(NIO|ONVO|FIREFLY)\b", re.IGNORECASE)
    pages = pdf_pages(path) if path.suffix == ".pdf" else [("IR 摘录末行", path.read_text(encoding="utf-8"))]
    found = {}
    for page, content in pages:
        # Group is matched to a named calendar month. Avoid cumulative and quarter totals.
        for hit in month_total.finditer(content):
            value = int(hit[1].replace(",", ""))
            if "GROUP" in found and found["GROUP"][0] != value:
                raise ValueError(f"{period}: conflicting monthly group totals")
            found.setdefault("GROUP", (value, hit[1], f"{page} · {month_name} {year} monthly deliveries"))
        if period >= "2024-09":
            for scope_match in brand_total.finditer(content):
                raw_value, scope = scope_match.groups()
                value = int(raw_value.replace(",", ""))
                scope = scope.upper()
                if scope in found and found[scope][0] != value:
                    raise ValueError(f"{period}: conflicting {scope} figures")
                found.setdefault(scope, (value, raw_value, f"{page} · deliveries by brand ({scope})"))
    if "GROUP" not in found:
        raise ValueError(f"{period}: missing stated group total")
    # April 2025 explicitly gives just two of the three brand counts.
    if len(found) > 1:
        brand_sum = sum(row[0] for key, row in found.items() if key != "GROUP")
        if brand_sum > found["GROUP"][0] or period != "2025-04" and brand_sum != found["GROUP"][0]:
            raise ValueError(f"{period}: brand reconciliation failed")
    for scope, (value, raw_value, locator) in found.items():
        yield "Q", value, raw_value, "辆", locator, scope


def market(path, period):
    month = int(period[-2:])
    content = re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", path.read_text(encoding="utf-8"))))
    patterns = [
        ("PassengerRetail", rf"{month}月(?:1[-—]\d+日，)?全国乘用车市场零售", "CN_PASSENGER_RETAIL", "全国乘用车市场回顾 · 零售"),
        ("NEVRetail", rf"{month}月新能源乘用车(?:市场)?零售", "CN_PASSENGER_RETAIL", "全国乘用车市场回顾 · 新能源零售"),
        ("PassengerWholesale", rf"{month}月全国乘用车厂商批发", "CN_PASSENGER_WHOLESALE", "全国乘用车市场回顾 · 厂商批发"),
    ]
    for metric, pattern, scope, locator in patterns:
        match = re.search(pattern + r"([\d.]+)万辆", content)
        if not match:
            raise ValueError(f"{period}: monthly total absent for {metric}")
        yield metric, int(Decimal(match[1]) * 10000), match[1], "万辆", locator, scope


def main():
    with (DATA / "monthly_sources.csv").open(encoding="utf-8", newline="") as handle:
        sources = list(csv.DictReader(handle))
    rows = []
    for source in sources:
        path = DATA.parent / source["path"]
        if sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Source hash mismatch: {path.name}")
        period = path.stem[-7:]
        reader = market if source["source_id"].startswith("CPCA-") else deliveries
        for metric, value, raw, unit, locator, scope in reader(path, period):
            rows.append([period, metric, value, raw, unit, source["source_id"], locator, scope])
    with NamedTemporaryFile("w", encoding="utf-8", newline="", dir=DATA, delete=False) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        writer.writerows(sorted(rows, key=lambda row: (row[0], row[1], row[-1], row[5])))
        pending = Path(handle.name)
    pending.replace(DATA / "monthly.csv")
    print(f"{len(rows)} observations from {len(sources)} releases")


if __name__ == "__main__":
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    main()
