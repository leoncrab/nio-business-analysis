"""Rebuild national CAAM manufacturer totals from the monthly source transcriptions."""
import csv
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
METRICS = {
    "汽车产量": "AutoProduction", "汽车销量": "AutoSales",
    "新能源汽车产量": "NEVProduction", "新能源汽车销量": "NEVSales",
}


def main():
    sources, figures = [], []
    for path in sorted((DATA / "raw/industry").glob("*.json")):
        raw = path.read_bytes()
        item = json.loads(raw)
        period = item["period"]
        if item["original_unit"] != "万辆" or path.stem != period:
            raise ValueError(f"Unexpected period or units for {path.name}")
        publication = item["publication"]
        source = f"MIIT-M-{period}"
        sources.append([source, item["title"], item["url"], publication[:10],
                        item["timezone"], path.relative_to(ROOT).as_posix(),
                        sha256(raw).hexdigest(), publication if "T" in publication else ""])
        quantities = {}
        for label, metric in METRICS.items():
            original = item["monthly_figures"][label]
            units = Decimal(original) * 10000
            if units <= 0 or units != units.to_integral_value():
                raise ValueError(f"{path.name}: invalid quantity for {label}")
            quantities[metric] = units
            figures.append([period, metric, int(units), original, item["original_unit"], source,
                            f"正文首段 / {label} / 当月数", "CAAM_NATIONAL"])
        if quantities["NEVSales"] > quantities["AutoSales"] or quantities["NEVProduction"] > quantities["AutoProduction"]:
            raise ValueError(f"{path.name}: NEV exceeds all powertrains")
    for filename, fields, rows in (
        ("industry.csv", "period metric value raw_value raw_unit source_id locator scope", figures),
        ("industry_sources.csv", "source_id title url published_date timezone path sha256 published_at_local", sources),
    ):
        temporary = (DATA / filename).with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(fields.split())
            writer.writerows(rows)
        temporary.replace(DATA / filename)
    print(f"{len(sources)} monthly releases, {len(figures)} national industry observations")


if __name__ == "__main__":
    main()
