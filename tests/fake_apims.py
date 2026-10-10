"""A small local copy of the two DOE endpoints, for tests only.

It answers from tests/fixtures/doe_readings_*.json (real values read from DOE).
Run:  python3 tests/fake_apims.py 8765
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIX = Path(__file__).parent / "fixtures" / "doe_readings_2026-10-03_to_10.json"
READINGS = json.loads(FIX.read_text())["readings"]
STATE = {"CA15W": 14, "CA19B": 10, "CA20B": 10, "CA18B": 10}
BROKEN = {"table": False}   # tests can switch the hourly table off


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path.endswith("/apitablehourly"):
            if BROKEN["table"]:
                self.send_error(503)
                return
            end = datetime.strptime(q["datetime"][0], "%Y-%m-%d %H:%M:%S")
            state = int(q["stateid"][0])
            rows = []
            for sid, series in READINGS.items():
                if STATE[sid] != state:
                    continue
                for hour, api in series.items():
                    t = datetime.fromisoformat(hour)
                    if end - timedelta(hours=24) <= t <= end:
                        rows.append({"STATION_ID": sid, "STATE_ID": state,
                                     "DATETIME": t.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "API": api, "PARAM_SYMBOL": "**"})
            body = {"api_table_hourly": rows}
        elif url.path.endswith("/query"):
            feats = []
            for sid, series in READINGS.items():
                hour = max(series)
                t = datetime.fromisoformat(hour).replace(tzinfo=timezone.utc)
                feats.append({"attributes": {"STATION_ID": sid,
                                             "DATETIME": int(t.timestamp() * 1000),
                                             "API": float(series[hour])}})
            body = {"features": feats}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port):
    return HTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765).serve_forever()
