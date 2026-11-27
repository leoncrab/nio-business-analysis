"""Source-backed regressions and isolated, temporary import failures."""
import csv
from datetime import datetime
import hashlib
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from nio.data import ROOT, close_of_date, load, snapshot


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = sqlite3.connect(":memory:")
        cls.notes = load(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_import_archive_twice(self):
        tables = ("source_manifest", "monthly_actual", "quarterly_actual", "company_guidance")
        def counts():
            return [self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables]
        before = counts()
        self.assertEqual(load(self.db), self.notes)
        self.assertEqual(before, counts())

    def test_monthly_disclosures_and_early_release_clock(self):
        rows = self.db.execute("""SELECT period,value FROM monthly_records
            WHERE scope='GROUP' AND metric='Q' AND period BETWEEN '2021-01' AND '2026-10'
            ORDER BY period""").fetchall()
        expected = [f"{2021 + i // 12}-{1 + i % 12:02}" for i in range(70)]
        self.assertEqual(expected, [row["period"] for row in rows])
        self.assertEqual(sum(r["value"] for r in rows[:12]), 91429)
        self.assertEqual(sum(r["value"] for r in rows[-10:]), 334547)
        # This month was actually announced on Aug 2, rather than Aug 1.
        when = close_of_date("2021-08-02", "Asia/Shanghai")
        for time, present in ((when-1, False), (when, True)):
            obs = snapshot(self.db, "monthly_records", time)
            self.assertEqual(any(r["period"] == "2021-07" for r in obs), present)

    def test_market_scope_and_missing_brand(self):
        rows = self.db.execute("""SELECT metric,scope,value FROM monthly_actual WHERE period='2026-10'""")
        facts = {(r["metric"],r["scope"]):r["value"] for r in rows}
        self.assertEqual(facts["PassengerRetail", "CN_PASSENGER_RETAIL"], 1_643_000)
        self.assertEqual(facts["NEVRetail", "CN_PASSENGER_RETAIL"], 1_109_000)
        self.assertEqual(facts["PassengerWholesale", "CN_PASSENGER_WHOLESALE"], 2_633_000)
        self.assertEqual(facts["Q", "GROUP"], sum(facts["Q",k] for k in ("NIO", "ONVO", "FIREFLY")))
        disclosed = dict(self.db.execute("SELECT scope,value FROM monthly_actual WHERE period='2025-04' AND metric='Q'"))
        self.assertEqual(disclosed, {"GROUP":23900, "NIO":19269, "ONVO":4400})

    def test_hkex_minute_and_industry_second_boundaries(self):
        cases = [
            ("HKEX-M-2026-10", "2026-11-01T18:23:00+08:00"),
            ("CPCA-2026-10", "2026-11-20T16:46:56+08:00"),
        ]
        for source_id, cutoff in cases:
            with self.subTest(source=source_id):
                when = int(datetime.fromisoformat(cutoff).timestamp())
                for as_of, present in ((when - 1, False), (when, True)):
                    rows = snapshot(self.db, "monthly_records", as_of)
                    self.assertEqual(any(r["source_id"] == source_id for r in rows), present)

    def test_accounting_bridge_against_hkex_disclosures(self):
        def values(period):
            return dict(self.db.execute(
                "SELECT metric,value FROM quarterly_actual WHERE period=? ORDER BY revision", (period,)))
        a, b = values("2026Q1"), values("2026Q2")
        self.assertEqual([a["OP"], b["OP"]], [-308811000, -347219000])
        contributions = [sign * (b[key] - a[key]) for key, sign in (("GP",1), ("RD",-1), ("SG",-1), ("OI",1))]
        self.assertEqual(contributions, [1047353000,-259894000,-927210000,101343000])
        self.assertEqual(sum(contributions), b["OP"] - a["OP"])

    def test_incomplete_financial_quarter_not_published_as_actual(self):
        cutoff = close_of_date("2026-11-27", "Asia/Shanghai") - 1
        rows = snapshot(self.db, "quarterly_records", cutoff)
        self.assertFalse(any(r["period"] >= "2026Q3" for r in rows))
        self.assertEqual(max(r["period"] for r in rows), "2026Q2")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for name in ("sql", "config"):
            shutil.copytree(ROOT / name, self.root / name)
        for name in ("finance_sources", "monthly_sources", "monthly", "quarterly", "guidance"):
            with (ROOT / "data" / f"{name}.csv").open(encoding="utf-8") as source:
                header = source.readline()
            (self.data / f"{name}.csv").write_text(header, encoding="utf-8")
        self.src = {"source_id":"v1", "title":"Fixture", "url":"https://example.org/mock",
                    "timezone":"Asia/Shanghai", "published_date":"2024-02-01", "path":"data/v1.txt"}
        self.row = {"period":"2024-01", "metric":"Q", "value":50, "raw_value":"50", "raw_unit":"辆",
                    "source_id":"v1", "scope":"GROUP", "locator":"Page 1, Deliveries"}
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def append(self, table, row):
        path = self.data / f"{table}.csv"
        with path.open(encoding="utf-8") as source:
            fields = next(csv.reader(source))
        with path.open("a", encoding="utf-8", newline="") as output:
            csv.DictWriter(output, fields).writerow(row)

    def add_release(self, source, row):
        file = self.root / source["path"]
        file.write_text(str(row["raw_value"]), encoding="utf-8")
        self.append("monthly_sources", dict(source,sha256=hashlib.sha256(file.read_bytes()).hexdigest()))
        self.append("monthly", row)

    def test_version_and_boundary_including_late_revision(self):
        self.add_release(self.src, self.row)
        load(self.db, self.root)
        self.add_release(dict(self.src, source_id="v2", path="data/v2.txt", published_date="2024-06-07"),
                         dict(self.row, source_id="v2", value=51, raw_value="51"))
        load(self.db, self.root)
        when = close_of_date("2024-06-07", "Asia/Shanghai")
        self.assertEqual(snapshot(self.db,"monthly_records",when-1)[0]["value"], 50)
        self.assertEqual(snapshot(self.db,"monthly_records",when)[0]["value"], 51)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM monthly_actual").fetchone()[0], 2)

    def test_hash_mismatch_does_not_publish_partial_import(self):
        self.add_release(self.src, self.row)
        load(self.db, self.root)
        bad = dict(self.src, source_id="v2", path="data/v2.txt", published_date="2024-03-01")
        self.add_release(bad, dict(self.row,source_id="v2",period="2024-02"))
        (self.root / bad["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            load(self.db, self.root)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM source_manifest").fetchone()[0], 1)

    def test_industry_unit_error_is_rejected(self):
        bad = dict(self.row, metric="PassengerRetail", scope="CN_PASSENGER_RETAIL",
                   raw_value="154.4", raw_unit="万辆", value=1544)
        self.add_release(self.src, bad)
        with self.assertRaises(ValueError):
            load(self.db,self.root)

    def test_industry_scope_error_is_rejected(self):
        self.add_release(self.src, dict(self.row,metric="PassengerRetail",scope="GROUP"))
        with self.assertRaises(ValueError):
            load(self.db,self.root)

    def test_future_actual_is_rejected(self):
        self.add_release(self.src, dict(self.row,period="2024-03"))
        with self.assertRaises(ValueError):
            load(self.db,self.root)


if __name__ == "__main__":
    unittest.main()
