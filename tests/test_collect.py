"""End-to-end tests of the collector against a local copy of the DOE endpoints."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_apims  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Collector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fake_apims.serve(0)          # any free port
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        fake_apims.BROKEN["table"] = False

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, now, start_date=None, no_school=None):
        env = dict(os.environ,
                   MONITOR_NOW=now, MONITOR_DATA_DIR=str(self.data),
                   APIMS_RETRY_WAIT="0", NO_PROXY="127.0.0.1", no_proxy="127.0.0.1",
                   APIMS_TABLE_URL=f"http://127.0.0.1:{self.port}/api3/publicportalapims/apitablehourly",
                   APIMS_CURRENT_URL=f"http://127.0.0.1:{self.port}/MapServer/0/query")
        if start_date or no_school:
            cfg = json.loads((ROOT / "config.json").read_text())
            cfg["rule"]["start_date"] = start_date or cfg["rule"]["start_date"]
            cfg["no_school_dates"] = no_school or []
            path = Path(self.tmp.name) / "config.json"
            path.write_text(json.dumps(cfg))
            env["MONITOR_CONFIG"] = str(path)
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "collect.py")],
                             env=env, capture_output=True, text=True)
        status = json.loads((self.data / "status.json").read_text()) \
            if (self.data / "status.json").exists() else None
        return run, status

    def test_first_run_backfills_a_week(self):
        run, st = self.collect("2026-10-10T15:37")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual([d["date"] for d in st["daily"]],
                         [f"2026-10-{n:02d}" for n in range(3, 11)])
        self.assertEqual([d["average"] for d in st["daily"]],
                         [72, 86, 85, 132, 172, 161, 168, 147])
        t = st["today"]
        self.assertEqual((t["reading_state"], t["average"], t["stations_used"]),
                         ("done", 147, 4))
        self.assertEqual((t["school_status"], t["decision_next_day"], t["streak"]),
                         ("open", "open", 0))
        self.assertEqual(st["latest"]["datetime"], "2026-10-10T15:00")
        self.assertEqual(st["latest"]["average"], 144)

    def test_before_two_pm_decision_is_pending(self):
        run, st = self.collect("2026-10-10T13:20")
        t = st["today"]
        self.assertEqual(t["reading_state"], "before")
        self.assertIsNone(t["decision_next_day"])
        self.assertEqual(t["school_status"], "open")
        self.assertEqual(st["daily"][-1]["date"], "2026-10-09")

    def test_rule_in_force_all_week(self):
        run, st = self.collect("2026-10-10T15:37", start_date="2026-10-03")
        t = st["today"]
        self.assertEqual((t["school_status"], t["decision_next_day"]), ("closed", "open"))
        self.assertEqual(st["daily"][-2]["reason"], "streak")
        # and the morning of the same day, before the reading
        self.tmp.cleanup(); self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        run, st = self.collect("2026-10-10T09:10", start_date="2026-10-03")
        self.assertEqual((st["today"]["school_status"], st["today"]["decision_next_day"]),
                         ("closed", None))

    def test_weekend_is_not_a_school_day(self):
        # 10 Oct 2026 is a Saturday. The rule still runs. The next school day is Monday.
        run, st = self.collect("2026-10-10T15:37")
        t = st["today"]
        self.assertEqual((t["school_day"], t["no_school_reason"]), (False, "weekend"))
        self.assertEqual(t["next_date"], "2026-10-11")
        self.assertEqual(t["next_school_date"], "2026-10-12")
        self.assertEqual(t["decision_next_day"], "open")          # rule result for Sunday
        self.assertEqual([d["school_day"] for d in st["daily"]],
                         [False, False, True, True, True, True, True, False])
        daily = (self.data / "daily.csv").read_text().splitlines()
        self.assertIn(",weekend,", daily[-1])
        self.assertIn(",yes,", daily[-2])

    def test_friday_points_to_monday_and_holidays_are_skipped(self):
        run, st = self.collect("2026-10-09T15:10", start_date="2026-10-03")
        t = st["today"]
        self.assertTrue(t["school_day"])
        self.assertEqual((t["school_status"], t["decision_next_day"]), ("open", "closed"))
        self.assertEqual(t["next_school_date"], "2026-10-12")
        run, st = self.collect("2026-10-09T15:10", start_date="2026-10-03",
                               no_school=["2026-10-12", "2026-10-09"])
        self.assertEqual(st["today"]["no_school_reason"], "holiday")
        self.assertEqual(st["today"]["next_school_date"], "2026-10-13")

    def test_no_reading_by_three_pm_is_flagged(self):
        run, st = self.collect("2026-10-11T14:20")
        self.assertEqual(st["today"]["reading_state"], "waiting")
        run, st = self.collect("2026-10-11T15:20")
        t = st["today"]
        self.assertEqual((t["reading_state"], t["reason"]), ("late", "no_data"))
        self.assertEqual(t["decision_next_day"], t["school_status"])

    def test_fallback_when_hourly_table_is_down(self):
        fake_apims.BROKEN["table"] = True
        run, st = self.collect("2026-10-10T15:37")
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(st["latest"]["datetime"], "2026-10-10T15:00")
        self.assertEqual(st["latest"]["values"],
                         {"CA15W": 162, "CA19B": 155, "CA20B": 160, "CA18B": 98})

    def test_second_run_without_new_data_writes_nothing(self):
        self.collect("2026-10-10T15:37")
        stamp = {p.name: p.stat().st_mtime_ns for p in self.data.iterdir()}
        text = (self.data / "status.json").read_text()
        run, _ = self.collect("2026-10-10T15:52")
        self.assertIn("no change", run.stdout)
        self.assertEqual(stamp, {p.name: p.stat().st_mtime_ns for p in self.data.iterdir()})
        self.assertEqual(text, (self.data / "status.json").read_text())


if __name__ == "__main__":
    unittest.main()
