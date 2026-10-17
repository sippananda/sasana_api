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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MONITOR_DATA_DIR", ROOT / "docs" / "data"))
CONFIG = Path(os.environ.get("MONITOR_CONFIG", ROOT / "config.json"))
SENSOR_CSV = DATA / "sensor.csv"

FEED_URL = os.environ.get("WAQI_FEED_URL", "https://api.waqi.info/feed/{id}/")
BOUNDS_URL = os.environ.get("WAQI_BOUNDS_URL", "https://api.waqi.info/v2/map/bounds")
BOX_DEG = 0.01   # search box half-width, about 1 km
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
COLUMNS = ["datetime_myt"] + [c for c, _ in FIELDS] + ["source"]
MYT = timezone(timedelta(hours=8))


def log(msg):
    print(msg, flush=True)


def get_json(url):
    """Read one aqicn URL. The URL holds the token, so it is never printed."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        raise RuntimeError(f"could not read aqicn: {type(err).__name__}") from None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        msg = payload.get("data") if isinstance(payload, dict) else None
        raise RuntimeError(f"aqicn answered with an error: {msg}")
    return payload["data"]


def fetch(sensor_id, token):
    """Return the aqicn feed for one station."""
    return get_json(FEED_URL.format(id=urllib.parse.quote(sensor_id)) + "?" +
                    urllib.parse.urlencode({"token": token}))


def find_station(sensor, token):
    """Find the sensor on the aqicn map search, by position and name.

    Returns {"id", "aqi", "time"} or None. aqicn sometimes gives a citizen
    sensor a new id, and its feed may refuse requests from GitHub's servers.
    The map search still lists the sensor with its id, index and time.
    """
    lat, lon = sensor.get("lat"), sensor.get("lon")
    if lat is None or lon is None:
        return None
    box = f"{lat - BOX_DEG},{lon - BOX_DEG},{lat + BOX_DEG},{lon + BOX_DEG}"
    found = get_json(BOUNDS_URL + "?" + urllib.parse.urlencode(
        {"latlng": box, "networks": "all", "token": token}))
    want = sensor.get("station_name", "").strip().lower()
    best = None
    for s in found if isinstance(found, list) else []:
        name = ((s.get("station") or {}).get("name") or "").strip().lower()
        uid = s.get("uid")
        if not isinstance(uid, int) or (want and name != want):
            continue
        dist = (float(s.get("lat", 0)) - lat) ** 2 + (float(s.get("lon", 0)) - lon) ** 2
        if best is None or dist < best[0]:
            best = (dist, s)
    if best is None:
        return None
    s = best[1]
    uid = s["uid"]
    # Citizen sensors have a negative uid; their feed id is "A" + the number.
    return {"id": f"A{-uid}" if uid < 0 else f"@{uid}",
            "aqi": s.get("aqi"),
            "time": (s.get("station") or {}).get("time")}


def map_row(station):
    """Row from the map search: overall index only, which is the PM2.5 index
    when PM2.5 is the main pollutant (normal for this sensor)."""
    stamp = station.get("time")
    if not stamp:
        raise RuntimeError("aqicn map search has no time for the sensor")
    utc = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    hour = utc.astimezone(MYT).strftime(KEY)
    row = {col: "" for col, _ in FIELDS}
    row["pm25_aqi"] = value(station.get("aqi"))
    row["datetime_myt"] = hour
    row["source"] = "map"
    return hour, row

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
    row["source"] = "feed"
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
    hour = row = None
    try:
        hour, row = parse(fetch(sensor["id"], token))
    except RuntimeError as err:
        if "no such station" not in str(err):
            raise
        log(f"  feed {sensor['id']}: {err}. Looking for the sensor on the map search.")
        station = find_station(sensor, token)
        if not station:
            raise
        if station["id"] != sensor["id"]:
            log(f"  aqicn now lists the sensor as {station['id']}. Please update config.json.")
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::warning title=Sensor id changed::aqicn now lists the sensor as "
                      f"{station['id']}. Please update config.json.", flush=True)
            try:
                hour, row = parse(fetch(station["id"], token))
            except RuntimeError as err2:
                log(f"  feed {station['id']}: {err2}")
        if row is None:
            log("  using the index from the map search (PM2.5 index only)")
            hour, row = map_row(station)
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
