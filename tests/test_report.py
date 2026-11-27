import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nio.__main__ import build
from nio.data import close_of_date
from nio.report import validate_draft


class ReleaseTests(unittest.TestCase):
    def test_archive_and_reference_checks(self):
        stamp = close_of_date("2026-11-27","Asia/Shanghai") - 1
        with TemporaryDirectory() as tmp:
            path = build(stamp,Path(tmp))
            self.assertEqual(path,build(stamp,Path(tmp)))
            packet = json.loads((path/"facts.json").read_text())
            draft = json.loads((path/"draft.json").read_text())
            report = (path/"brief.md").read_text()
            self.assertEqual(validate_draft(packet,draft)[0],report)
            for bad in [dict(draft,fact_ids=["invented"]),dict(draft,claim="收入翻倍"),None]:
                text,review = validate_draft(packet,bad)
                self.assertEqual(text,report)
                self.assertEqual(review["rule_status"],"template_fallback")
            with sqlite3.connect(Path(tmp)/"nio.sqlite") as db:
                versions=dict(db.execute("SELECT source_id,available_at FROM source_manifest"))
                for cutoff,inputs in db.execute("""SELECT DISTINCT v.as_of,m.inputs_json
                        FROM forecast_vintage v JOIN model_assumptions m USING(run_id)"""):
                    parsed=json.loads(inputs)
                    self.assertEqual(set(parsed["source_ids"]),set(parsed["sha256"]))
                    self.assertTrue(all(versions[s]<=cutoff for s in parsed["source_ids"]))

    def test_empty_information_set(self):
        with TemporaryDirectory() as tmp:
            path=build(close_of_date("2020-01-01","Asia/Shanghai")-1,Path(tmp))
            self.assertEqual(json.loads((path/"facts.json").read_text())["facts"],[])
            self.assertIn("尚无", (path/"brief.md").read_text())
