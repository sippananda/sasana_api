"""Tests of the CAMS forecast script against a local copy of Open-Meteo."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fake_open_meteo  # noqa: E402
import forecast  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 2 pm readings for the calibration: three haze days and one clean day.
HOURLY = """datetime_myt,CA15W,CA19B,CA20B,CA18B
2026-10-13T14:00,90,77,88,74
2026-10-14T14:00,164,156,163,92
2026-10-15T14:00,163,169,181,160
2026-10-16T14:00,167,174,170,153
2026-10-17T09:00,159,169,169,93
"""


class Formula(unittest.TestCase):
    def test_breakpoints(self):
        self.assertAlmostEqual(forecast.api_from_pm(12.0), 50.0, places=1)
        self.assertAlmostEqual(forecast.api_from_pm(75.5), 100.1, places=1)
        self.assertAlmostEqual(forecast.api_from_pm(113), 150.6, places=0)
        self.assertAlmostEqual(forecast.api_from_pm(135), 179.6, places=0)

    def test_inverse(self):
        for pm in (5, 30, 75.5, 100, 140, 200, 300, 400):
            api = forecast.api_from_pm(pm)
            self.assertAlmostEqual(forecast.pm_from_api(api), pm, places=1)


class Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = fake_open_meteo.serve(0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        (self.data / "hourly.csv").write_text(HOURLY)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, now):
        env = dict(os.environ, MONITOR_NOW=now, MONITOR_DATA_DIR=str(self.data),
                   NO_PROXY="127.0.0.1", no_proxy="127.0.0.1",
                   OPEN_METEO_AQ_URL=f"http://127.0.0.1:{self.port}/v1/air-quality",
                   OPEN_METEO_WX_URL=f"http://127.0.0.1:{self.port}/v1/forecast")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "forecast.py")],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads((self.data / "forecast.json").read_text()), r.stdout

    def test_morning_run(self):
        out, log = self.run_script("2026-10-17T07:30")
        self.assertEqual(out["issued"], "2026-10-17T07:30+08:00")
        self.assertEqual([d["date"] for d in out["days"]],
                         ["2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"])
        # Calibration uses the three haze days only (observed API 100 or above).
        kl = out["calibration"]["KL"]
        self.assertEqual(kl["basis"], "haze days")
        self.assertEqual(kl["n"], 3)
        self.assertEqual(kl["n_haze"], 3)
        # Median ratio of the KL haze days: observed PM / 90.
        ratios = sorted(r["ratio"] for r in out["calibration"]["days"]
                        if r["cell"] == "KL" and r["haze"])
        self.assertAlmostEqual(kl["k"], ratios[1], places=3)
        self.assertAlmostEqual(kl["min"], ratios[0], places=3)
        # Day 1: CAMS KL 24-h mean of (10 h at 90, 14 h at 78) = 83, times k.
        d1 = out["days"][0]
        self.assertAlmostEqual(d1["cams_pm25_24h"]["KL"], 83.0, places=0)
        self.assertEqual(d1["api_2pm"]["CA15W"], d1["api_2pm"]["CA19B"])
        mid = d1["avg_2pm"]["mid"]
        self.assertEqual(mid, round((3 * d1["api_2pm"]["CA15W"] + d1["api_2pm"]["CA18B"]) / 4 + 1e-9))
        self.assertLessEqual(d1["avg_2pm"]["low"], mid)
        self.assertGreaterEqual(d1["avg_2pm"]["high"], mid)
        # The forecast falls day by day.
        mids = [d["avg_2pm"]["mid"] for d in out["days"]]
        self.assertEqual(mids, sorted(mids, reverse=True))
        self.assertEqual(d1["confidence"], "medium")       # fewer than 7 haze days
        self.assertEqual(out["days"][4]["confidence"], "low")
        self.assertEqual(d1["weather_2pm"]["rain_mm_day"], 4.0)
        self.assertEqual(d1["weather_2pm"]["wind_from_deg"], 200)
        self.assertIn("CAMS", out["credit"])
        # One line in the history file.
        lines = (self.data / "forecast_history.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["days"][0][0], "2026-10-17")

    def test_evening_run_starts_tomorrow(self):
        out, _ = self.run_script("2026-10-17T19:30")
        # Day 5 of CAMS (22 Oct) has no data, so only four days remain.
        self.assertEqual([d["date"] for d in out["days"]],
                         ["2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"])

    def test_haze_factor_kept_after_clean_spell(self):
        # Haze 35 to 31 days ago, then 20 clean days. The haze days must still set the factor.
        rows = ["datetime_myt,CA15W,CA19B,CA20B,CA18B"]
        for d in range(12, 17):          # 12 to 16 Sep: haze
            rows.append(f"2026-09-{d:02d}T14:00,170,165,168,150")
        for d in range(27, 31):          # 27 to 30 Sep: clean
            rows.append(f"2026-09-{d:02d}T14:00,60,55,58,50")
        for d in range(1, 17):           # 1 to 16 Oct: clean
            rows.append(f"2026-10-{d:02d}T14:00,60,55,58,50")
        (self.data / "hourly.csv").write_text("\n".join(rows) + "\n")
        out, _ = self.run_script("2026-10-17T07:30")
        kl = out["calibration"]["KL"]
        self.assertEqual(kl["basis"], "haze days")
        self.assertEqual(kl["n"], 5)
        self.assertEqual((kl["first"], kl["last"]), ("2026-09-12", "2026-09-16"))
        self.assertGreater(kl["k"], 1.0)             # haze: observed above CAMS (flat 90)
        self.assertLess(kl["k_clean"], 1.0)          # clean: observed below CAMS
        self.assertEqual(kl["n_clean"], 14)
        self.assertEqual(out["calibration"]["lookback_days"], 60)

    def test_only_newest_ten_haze_days(self):
        rows = ["datetime_myt,CA15W,CA19B,CA20B,CA18B"]
        for d in range(1, 17):           # 16 haze days, 1 to 16 Oct
            rows.append(f"2026-10-{d:02d}T14:00,{120 + d},{120 + d},{120 + d},{120 + d}")
        (self.data / "hourly.csv").write_text("\n".join(rows) + "\n")
        out, _ = self.run_script("2026-10-17T07:30")
        kl = out["calibration"]["KL"]
        self.assertEqual(kl["n"], 10)
        self.assertEqual(kl["n_haze"], 16)
        self.assertEqual((kl["first"], kl["last"]), ("2026-10-07", "2026-10-16"))

    def test_too_few_haze_days_uses_all(self):
        (self.data / "hourly.csv").write_text(
            "datetime_myt,CA15W,CA19B,CA20B,CA18B\n2026-10-15T14:00,80,70,75,60\n2026-10-16T14:00,160,150,155,120\n")
        out, _ = self.run_script("2026-10-17T07:30")
        self.assertEqual(out["calibration"]["KL"]["basis"], "all days")
        self.assertEqual(out["calibration"]["KL"]["n"], 2)

    def test_no_history_gives_k_one(self):
        (self.data / "hourly.csv").unlink()
        out, _ = self.run_script("2026-10-17T07:30")
        self.assertEqual(out["calibration"]["KL"]["k"], 1.0)
        self.assertEqual(out["calibration"]["KL"]["n"], 0)
        self.assertTrue(out["days"])


if __name__ == "__main__":
    unittest.main()
