"""Tests of the aqicn sensor collector against a small local fake server."""
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token"


def feed(stamp, pm25):
    return {"status": "ok", "data": {
        "aqi": pm25, "idx": 2871205,
        "time": {"s": stamp, "tz": "+08:00"},
        "iaqi": {"pm25": {"v": pm25}, "pm10": {"v": 52}, "pm1": {"v": 87},
                 "h": {"v": 88}, "t": {"v": 25.4}, "w": {"v": 1}}}}


class Handler(BaseHTTPRequestHandler):
    answer = None
    live_id = None      # when set, only this feed id answers; others are "no such station"
    bounds = None       # answer of the map search
    asked = []

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        Handler.asked.append(url.path)
        if q.get("token") != [TOKEN]:
            body = {"status": "error", "data": "Invalid key"}
        elif url.path.startswith("/bounds"):
            body = {"status": "ok", "data": Handler.bounds or []}
        elif Handler.live_id and url.path != f"/feed/{Handler.live_id}/":
            body = {"status": "error", "data": "no such station"}
        else:
            body = Handler.answer
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class SensorCollector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.live_id, Handler.bounds, Handler.asked = None, None, []
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(json.dumps({"sensor": {
            "id": "A2871205", "name": "Test sensor", "station_name": "Jalan Anggun 3P",
            "lat": 3.304542, "lon": 101.531164}}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_collector(self, token=TOKEN):
        env = dict(os.environ)
        # Keep test runs off the real GitHub run page.
        for name in ("GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            env.pop(name, None)
        env.update({
            "MONITOR_DATA_DIR": str(self.data),
            "WAQI_FEED_URL": f"http://127.0.0.1:{self.server.server_address[1]}/feed/{{id}}/",
            "WAQI_BOUNDS_URL": f"http://127.0.0.1:{self.server.server_address[1]}/bounds",
            "WAQI_TOKEN": token,
            "MONITOR_CONFIG": str(self.config),
        })
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "collect_sensor.py")],
                              env=env, capture_output=True, text=True)

    def rows(self):
        with open(self.data / "sensor.csv", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_saves_one_row_per_hour(self):
        Handler.answer = feed("2026-10-15 07:00:00", 129)
        self.assertEqual(self.run_collector().returncode, 0)
        Handler.answer = feed("2026-10-15 08:00:00", 131)
        self.assertEqual(self.run_collector().returncode, 0)
        Handler.answer = feed("2026-10-15 08:00:00", 132)   # same hour, newer value
        self.assertEqual(self.run_collector().returncode, 0)
        rows = self.rows()
        self.assertEqual([r["datetime_myt"] for r in rows],
                         ["2026-10-15T07:00", "2026-10-15T08:00"])
        self.assertEqual(rows[1]["pm25_aqi"], "132")
        self.assertEqual(rows[0]["temp_c"], "25.4")
        self.assertEqual(rows[0]["rh_percent"], "88")

    def test_no_token_does_nothing(self):
        result = self.run_collector(token="")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.data / "sensor.csv").exists())

    def test_bad_token_fails_without_printing_it(self):
        Handler.answer = feed("2026-10-15 07:00:00", 129)
        result = self.run_collector(token="wrong-secret-value")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid key", result.stdout)
        self.assertNotIn("wrong-secret-value", result.stdout + result.stderr)
        self.assertFalse((self.data / "sensor.csv").exists())

    def test_finds_new_id_when_old_id_is_gone(self):
        Handler.answer = feed("2026-10-18 00:33:13", 93)
        Handler.live_id = "A2872234"
        Handler.bounds = [
            {"lat": 3.31, "lon": 101.54, "uid": -999, "station": {"name": "Other sensor"}},
            {"lat": 3.304542, "lon": 101.531164, "uid": -2872234,
             "station": {"name": "Jalan Anggun 3P"}}]
        result = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("A2872234", result.stdout)
        self.assertEqual(self.rows()[0]["pm25_aqi"], "93")

    def test_gone_and_not_found_fails(self):
        Handler.answer = feed("2026-10-18 00:33:13", 93)
        Handler.live_id = "A0"
        Handler.bounds = [{"lat": 3.31, "lon": 101.54, "uid": -999,
                           "station": {"name": "Other sensor"}}]
        result = self.run_collector()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no such station", result.stdout)
        self.assertFalse((self.data / "sensor.csv").exists())


if __name__ == "__main__":
    unittest.main()
