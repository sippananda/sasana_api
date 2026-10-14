#!/usr/bin/env python3
"""Collect the hourly reading of one nearby low-cost sensor from aqicn.org.

For information only. This data never feeds the school closure rule.

The sensor is listed in config.json under "sensor". aqicn gives index
values on a US EPA style scale, not concentrations in ug/m3. This script
saves the raw index values. Conversion to ug/m3 is done later, after the
sensor has been compared with the DOE stations.

Needs the secret WAQI_TOKEN (a free token from aqicn.org/api).
Without the token the script does nothing and ends without error.

Saves docs/data/sensor.csv, one row per hour, Malaysia time.
Uses only the Python standard library.
"""
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MONITOR_DATA_DIR", ROOT / "docs" / "data"))
CONFIG = Path(os.environ.get("MONITOR_CONFIG", ROOT / "config.json"))
SENSOR_CSV = DATA / "sensor.csv"

FEED_URL = os.environ.get("WAQI_FEED_URL", "https://api.waqi.info/feed/{id}/")
KEY = "%Y-%m-%dT%H:00"   # one key per hour, Malaysia time

# Column name in sensor.csv -> field name in the aqicn "iaqi" block.
FIELDS = [
    ("pm25_aqi", "pm25"),
    ("pm10_aqi", "pm10"),
    ("pm1_aqi", "pm1"),
    ("rh_percent", "h"),
    ("temp_c", "t"),
    ("wind_ms", "w"),
]
COLUMNS = ["datetime_myt"] + [c for c, _ in FIELDS]


def log(msg):
    print(msg, flush=True)


def fetch(sensor_id, token):
    """Return the aqicn feed for one station.

    The URL holds the token, so it is never printed.
    """
    url = FEED_URL.format(id=urllib.parse.quote(sensor_id)) + "?" + \
        urllib.parse.urlencode({"token": token})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        raise RuntimeError(f"could not read the aqicn feed: {type(err).__name__}") from None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        msg = payload.get("data") if isinstance(payload, dict) else None
        raise RuntimeError(f"aqicn answered with an error: {msg}")
    return payload["data"]


def value(x):
    """Number as int when whole, float otherwise, empty string when missing."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return ""
    return int(f) if f == int(f) else round(f, 1)


def parse(data):
    """Turn one aqicn feed into (hour_key, row). Times are Malaysia time."""
    stamp = (data.get("time") or {}).get("s")
    if not stamp:
        raise RuntimeError("aqicn feed has no time")
    tz = (data.get("time") or {}).get("tz", "")
    if tz and tz != "+08:00":
        raise RuntimeError(f"unexpected time zone in aqicn feed: {tz}")
    hour = datetime.fromisoformat(stamp[:19]).strftime(KEY)
    iaqi = data.get("iaqi") or {}
    row = {col: value((iaqi.get(name) or {}).get("v")) for col, name in FIELDS}
    row["datetime_myt"] = hour
    return hour, row


def read_rows():
    rows = {}
    if SENSOR_CSV.exists():
        with open(SENSOR_CSV, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("datetime_myt"):
                    rows[r["datetime_myt"]] = {c: r.get(c, "") for c in COLUMNS}
    return rows


def csv_text(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for key in sorted(rows):
        w.writerow(rows[key])
    return buf.getvalue()


def main():
    token = os.environ.get("WAQI_TOKEN", "").strip()
    if not token:
        log("No WAQI_TOKEN. Sensor collection skipped.")
        return 0
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    sensor = cfg.get("sensor")
    if not sensor:
        log("No sensor in config.json. Sensor collection skipped.")
        return 0

    log(f"Reading sensor {sensor['id']} ({sensor['name']}) from aqicn")
    data = fetch(sensor["id"], token)
    hour, row = parse(data)
    if row["pm25_aqi"] == "":
        raise RuntimeError("aqicn feed has no PM2.5 value")
    log(f"  {hour}: PM2.5 index {row['pm25_aqi']}, humidity {row['rh_percent']} %")

    rows = read_rows()
    rows[hour] = {c: row.get(c, "") for c in COLUMNS}
    text = csv_text(rows)
    old = SENSOR_CSV.read_text(encoding="utf-8") if SENSOR_CSV.exists() else None
    if text != old:
        DATA.mkdir(parents=True, exist_ok=True)
        SENSOR_CSV.write_text(text, encoding="utf-8")
        log(f"  saved {SENSOR_CSV.name} ({len(rows)} hours)")
    else:
        log("  no new reading")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as err:
        log(f"Sensor collection failed: {err}")
        sys.exit(1)
