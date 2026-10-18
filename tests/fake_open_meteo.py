"""A small local copy of the two Open-Meteo endpoints, for tests only.

CAMS PM2.5 is invented: a flat value per cell for the past days, then a
value that falls day by day in the forecast. The cell is told apart by the
longitude in the request. Day 6 of the forecast has no values (null), as
on the real service.
Run:  python3 tests/fake_open_meteo.py 8766
"""
import json
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

TODAY = datetime(2026, 10, 17)          # the fake "today", Malaysia time
PM = {"101.6": 90.0, "101.2": 60.0}      # past PM2.5 per cell, by longitude
FALL = 12.0                              # PM2.5 falls this much per forecast day
EMPTY_FROM_DAY = 5                       # forecast day index with no values (0 = today)


def hours(past_days, forecast_days):
    start = TODAY - timedelta(days=past_days)
    n = (past_days + forecast_days) * 24
    return [start + timedelta(hours=i) for i in range(n)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        past = int(q.get("past_days", ["0"])[0])
        fdays = int(q.get("forecast_days", ["1"])[0])
        ts = hours(past, fdays)
        times = [t.strftime("%Y-%m-%dT%H:%M") for t in ts]
        if url.path.endswith("/air-quality"):
            lon = q["longitude"][0]
            base = PM[lon]
            vals = []
            for t in ts:
                d = (t.date() - TODAY.date()).days
                if d >= EMPTY_FROM_DAY:
                    vals.append(None)
                elif d < 0:
                    vals.append(base)
                else:
                    vals.append(max(5.0, base - FALL * (d + 1)))
            body = {"latitude": 3.2, "longitude": float(lon),
                    "hourly": {"time": times, "pm2_5": vals}}
        elif url.path.endswith("/forecast"):
            body = {"latitude": 3.3, "longitude": 101.5,
                    "hourly": {"time": times,
                               "wind_speed_10m": [5.0 + t.hour / 10 for t in ts],
                               "wind_direction_10m": [200 for _ in ts],
                               "precipitation": [0.5 if t.hour == 15 else 0.0 for t in ts]},
                    "daily": {"time": sorted({t.strftime("%Y-%m-%d") for t in ts}),
                              "precipitation_sum": [4.0] * len({t.date() for t in ts})}}
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    print(f"fake Open-Meteo on http://127.0.0.1:{port}")
    serve(port).serve_forever()
