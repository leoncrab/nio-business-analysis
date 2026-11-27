import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from nio.model import build_analysis, forecast_delivery, months, op, profit_bridge, shift_month


def epoch(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        for tbl in ["monthly_records", "quarterly_records"]:
            self.db.execute(f"""CREATE TABLE {tbl} (
                record_id TEXT, period TEXT, metric TEXT, value REAL, scope TEXT,
                source_id TEXT, available_at INTEGER, revision INTEGER)""")
        self.db.execute("""CREATE TABLE guidance_records (
            period TEXT, metric TEXT, low REAL, high REAL, source_id TEXT, available_at INTEGER)""")
        for idx in range(36):
            m = shift_month("2022-01", idx)
            self.add("monthly_records", m, "Q", 100 + idx, epoch(shift_month(m, 1) + "-03"))
        for year in (2022, 2023, 2024):
            for number in (1, 2, 3, 4):
                p = f"{year}Q{number}"
                qty = sum(self.db.execute(
                    "SELECT value FROM monthly_records WHERE period=?", (m,)
                ).fetchone()[0] for m in months(p))
                when = epoch(shift_month(months(p)[-1], 3) + "-12")
                rv, cv = 10000 * qty, 8500 * qty
                vals = {"Q": qty, "Rv": rv, "Cv": cv, "Ro": 2000, "Co": 1500, "RD": 600,
                        "SG": 400, "OI": -20, "Revenue": rv + 2000, "OP": rv - cv - 520}
                for k, v in vals.items():
                    self.add("quarterly_records", p, k, v, when)
        self.db.execute("INSERT INTO guidance_records VALUES (?,?,?,?,?,?)",
                        ("2024Q1", "Q", 380, 410, "g1", epoch("2024-02-10")))
        self.as_of = epoch("2025-02-20")

    def tearDown(self):
        self.db.close()

    def add(self, table, p, k, v, t, rev=1, scope="GROUP"):
        self.db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)", (
            f"{table}/{scope}/{p}/{k}/{rev}", p, k, v, scope, f"{p}/{rev}", t, rev))

    def test_reproduction_and_revisions(self):
        before = build_analysis(self.db, self.as_of)
        self.add("quarterly_records", "2024Q1", "OP", 9000000, epoch("2025-03-01"), 2)
        # Retroactive changes arriving after the designated freeze cannot alter its snapshot.
        self.assertEqual(before, build_analysis(self.db, self.as_of))
        after = build_analysis(self.db, epoch("2025-03-10"))
        past = [r for r in before["runs"] if r["origin"] != "Live"]
        new_past = [r for r in after["runs"] if r["origin"] != "Live"]
        self.assertEqual(past, new_past)
        revised = next(r for r in after["scores"] if r["period"] == "2024Q1" and r["metric"] == "OP")
        self.assertNotEqual(revised["actual"], revised["revised_actual"])
        self.assertEqual(revised["revised_actual"], 9000000)

    def test_release_clocks_and_missing_financials(self):
        res = build_analysis(self.db, epoch("2024-02-05"))
        live = next(r for r in res["runs"] if (r["period"], r["origin"], r["model"], r["scenario"])
                    == ("2024Q1", "Live", "ma3", "base"))
        self.assertEqual(live["guidance"], [])
        self.assertEqual(live["assumptions"]["finance_period"], "2023Q3")
        self.assertEqual([m["kind"] for m in live["monthly"]], ["actual", "forecast", "forecast"])
        # February data and the future financial report cannot enter any model input.
        for record_id in live["input_record_ids"]:
            table = record_id.split("/")[0]
            available = self.db.execute(
                f"SELECT available_at FROM {table} WHERE record_id=?", (record_id,)
            ).fetchone()[0]
            self.assertLessEqual(available, live["as_of"])
        self.assertFalse(any(s["period"] == "2024Q1" for s in res["scores"]))

    def test_guidance_and_stress_freeze_observed_months(self):
        res = build_analysis(self.db, epoch("2024-03-04"))
        r = [r for r in res["runs"] if (r["period"], r["origin"], r["model"])
             == ("2024Q1", "Live", "ma3")]
        self.assertEqual(len(r), 3)
        self.assertEqual(len({r0["monthly"][0]["value"] for r0 in r}), 1)
        self.assertEqual(len({r0["monthly"][1]["value"] for r0 in r}), 1)
        self.assertEqual(len({r0["monthly"][2]["value"] for r0 in r}), 3)
        g = r[0]["guidance"][0]
        self.assertEqual(g["remaining_low"], 380 - 124 - 125)
        self.assertEqual(g["required_per_month_low"], g["remaining_low"])

    def test_updated_approximate_point_is_not_a_range(self):
        self.db.execute("INSERT INTO guidance_records VALUES (?,?,?,?,?,?)",
                        ("2024Q1", "Q", 380, 380, "g2", epoch("2024-03-01")))
        out = build_analysis(self.db, self.as_of)
        r = next(r for r in out["scores"] if r["period"] == "2024Q1" and r["metric"] == "Q"
                 and r["origin"] == "T2")
        self.assertNotIn("within", r["guidance_comparison"])
        self.assertEqual(r["guidance_comparison"]["source_id"], "g2")
        self.assertEqual(r["guidance_comparison"]["delta_to_point"], r["actual"] - 380)

    def test_comparable_scoring_and_error_bridges(self):
        out = build_analysis(self.db, self.as_of)
        self.assertTrue(out["performance"])
        self.assertFalse(any(s["origin"] == "T3" and s["metric"] == "Q" for s in out["scores"]))
        quarters = {}
        for row in out["performance"]:
            key = (row["origin"], row["metric"], row["split"])
            quarters.setdefault(key, []).append(row["periods"])
            if row["metric"] == "OP":
                self.assertIsNone(row["wape"])
        for group in quarters.values():
            self.assertEqual(len(group), 3)
            self.assertTrue(all(p == group[0] for p in group))
        errors = {r["run_id"]: r["error"] for r in out["scores"] if r["metric"] == "OP"}
        bridges = [b for b in out["bridges"] if b["kind"] == "forecast_error"]
        self.assertTrue(bridges)
        for b in bridges:
            self.assertAlmostEqual(sum(b["effects"].values()), errors[b["run_id"]], places=2)

    def test_symmetric_bridge_and_zero_quantity(self):
        a = dict(Q=0, P=281232.54, C=232750.20, Ro=1e7, Co=2e7, RD=8e7, SG=1e8, OI=-1210)
        b = dict(a, Q=87803, P=285000.11, C=232500.38, OI=3907, SG=2e8)
        forward, residual = profit_bridge(a, b)
        reverse, _ = profit_bridge(b, a)
        self.assertLess(abs(residual), .01)
        self.assertEqual(set(forward), set(a))
        for key in a:
            self.assertAlmostEqual(forward[key], -reverse[key])
        self.assertAlmostEqual(sum(forward.values()), op(b) - op(a), places=2)

    def test_insufficient_history_and_brand_isolation(self):
        self.db.execute("DELETE FROM monthly_records WHERE period <> '2024-01'")
        self.add("monthly_records", "2024-01", "Q", 77777, epoch("2024-02-03"), scope="NIO")
        out = build_analysis(self.db, epoch("2024-02-05"))
        self.assertFalse(out["runs"])
        self.assertTrue(out["skipped"])
        self.assertIsNone(forecast_delivery("2024Q1", "seasonal", {}, {"delivery_window": 3}))


if __name__ == "__main__":
    unittest.main()
