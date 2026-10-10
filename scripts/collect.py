#!/usr/bin/env python3
"""Collect DOE air quality readings and work out the school closure status.

Runs on GitHub Actions. Uses only the Python standard library.

What it does on every run:
  1. Reads the last 24 hours of hourly API from the DOE APIMS website,
     for the four stations in config.json.
  2. Fills gaps for the last few days (the DOE site keeps about one week).
  3. Saves all readings in docs/data/hourly.csv.
  4. Works out the 2:00 pm average for each day and applies the school rule.
  5. Writes docs/data/daily.csv and docs/data/status.json for the web page.

All times are Malaysia time (UTC+8).
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MONITOR_DATA_DIR", ROOT / "docs" / "data"))
CONFIG = Path(os.environ.get("MONITOR_CONFIG", ROOT / "config.json"))
HOURLY_CSV = DATA / "hourly.csv"
DAILY_CSV = DATA / "daily.csv"
STATUS_JSON = DATA / "status.json"

# DOE APIMS public endpoints. The env variables exist only for testing.
TABLE_URL = os.environ.get(
    "APIMS_TABLE_URL",
    "https://eqms.doe.gov.my/api3/publicportalapims/apitablehourly")
CURRENT_URL = os.environ.get(
    "APIMS_CURRENT_URL",
    "https://eqms.doe.gov.my/api3/publicmapproxy/PUBLIC_DISPLAY/"
    "CAQM_MCAQM_Current_Reading/MapServer/0/query")
USER_AGENT = "Mozilla/5.0 (compatible; SasanaAirQualityMonitor/1.0; school haze monitor)"

RETRY_WAIT = float(os.environ.get("APIMS_RETRY_WAIT", "5"))   # seconds

KEY = "%Y-%m-%dT%H:00"      # one key per hour, Malaysia time


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- time

def now_local(cfg):
    """Current Malaysia time, without timezone info. MONITOR_NOW is for tests."""
    forced = os.environ.get("MONITOR_NOW")
    if forced:
        return datetime.fromisoformat(forced)
    tz = timezone(timedelta(hours=cfg["timezone_offset_hours"]))
    return datetime.now(tz).replace(tzinfo=None)


# ---------------------------------------------------------------- network

def get_json(url, tries=3, timeout=30):
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
            last = err
            log(f"  try {attempt} failed: {err}")
            if attempt < tries:
                time.sleep(RETRY_WAIT * attempt)
    raise RuntimeError(f"could not read {url}: {last}")


def num(value):
    """API value as int when whole, float otherwise, None when missing."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return int(f) if f == int(f) else f


def fetch_window(end, state_id, wanted):
    """Hourly API for the 24 hours ending at `end`, for one state.

    Returns [(station_id, hour_key, api), ...] for the wanted stations only.
    """
    query = urllib.parse.urlencode({
        "stateid": state_id,
        "datetime": end.strftime("%Y-%m-%d %H:00:00"),
    })
    payload = get_json(f"{TABLE_URL}?{query}")
    rows = payload.get("api_table_hourly") if isinstance(payload, dict) else None
    if rows is None:
        raise RuntimeError("unexpected answer from DOE (no api_table_hourly)")
    out = []
    for row in rows:
        sid = row.get("STATION_ID")
        api = num(row.get("API"))
        stamp = row.get("DATETIME")
        if sid not in wanted or api is None or not stamp:
            continue
        hour = datetime.fromisoformat(stamp[:19]).strftime(KEY)
        out.append((sid, hour, api))
    return out


def fetch_current(wanted):
    """Fallback: the newest single reading per station from the DOE map service.

    This service stores Malaysia clock time in a field that is labelled UTC,
    so the number is read as UTC and used as Malaysia time.
    """
    ids = ",".join(f"'{s}'" for s in sorted(wanted))
    query = urllib.parse.urlencode({
        "where": f"STATION_ID IN ({ids})",
        "outFields": "STATION_ID,DATETIME,API",
        "returnGeometry": "false",
        "f": "json",
    })
    payload = get_json(f"{CURRENT_URL}?{query}")
    out = []
    for feat in payload.get("features", []):
        a = feat.get("attributes", {})
        sid, api, ms = a.get("STATION_ID"), num(a.get("API")), a.get("DATETIME")
        if sid not in wanted or api is None or not ms:
            continue
        wall = datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)
        out.append((sid, wall.strftime(KEY), api))
    return out


# ---------------------------------------------------------------- storage

def read_hourly(ids):
    data = {}
    if not HOURLY_CSV.exists():
        return data
    with open(HOURLY_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            hour = row.get("datetime_myt")
            if not hour:
                continue
            vals = {sid: num(row.get(sid)) for sid in ids}
            data[hour] = {k: v for k, v in vals.items() if v is not None}
    return data


def write_text_if_changed(path, text):
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def hourly_csv_text(data, ids):
    lines = ["datetime_myt," + ",".join(ids)]
    for hour in sorted(data):
        if not data[hour]:
            continue
        cells = ["" if data[hour].get(s) is None else str(data[hour][s]) for s in ids]
        lines.append(hour + "," + ",".join(cells))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- daily

def fmt(x):
    """Average with up to 2 decimals and no trailing zeros."""
    if x is None:
        return ""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def build_days(hourly, cfg, now):
    """One entry per calendar day, from the first 2 pm reading to today."""
    ids = [s["id"] for s in cfg["stations"]]
    rule = cfg["rule"]
    hh = cfg["reading_hour"]
    late = cfg["collector"]["late_after_hour"]

    reading_days = sorted(k[:10] for k in hourly
                          if k.endswith(f"T{hh:02d}:00") and hourly[k])
    today = now.date()
    start = datetime.strptime(rule["start_date"], "%Y-%m-%d").date()
    first = min(start, today)
    if reading_days:
        first = min(first, datetime.strptime(reading_days[0], "%Y-%m-%d").date())

    days = []
    d = first
    while d <= today:
        key = f"{d.isoformat()}T{hh:02d}:00"
        vals = hourly.get(key, {})
        values = {sid: vals.get(sid) for sid in ids}
        used = sum(v is not None for v in values.values())
        avg = policy.average(list(values.values()), rule["min_stations"])
        entry = {
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "values": values,
            "stations_used": used,
            "average": avg,
        }
        if d == today:
            if now.hour < hh:
                state = "before"
            elif avg is not None:
                state = "done"
            elif now.hour < late:
                state = "waiting"
            else:
                state = "late"
            entry["reading_state"] = state
        days.append(entry)
        d += timedelta(days=1)
    return days


def build_status(hourly, cfg, now):
    ids = [s["id"] for s in cfg["stations"]]
    rule = cfg["rule"]
    days = build_days(hourly, cfg, now)

    today = days[-1]
    # Today enters the rule only when its reading is in, or when it is clearly late.
    final = days if today["reading_state"] in ("done", "late") else days[:-1]
    evaluated = policy.evaluate(final, rule)

    if final is days:
        t = evaluated[-1]
        today_out = dict(t)
    else:
        prev = evaluated[-1] if evaluated else None
        started = today["date"] >= rule["start_date"]
        if not started:
            status_today, streak = None, None
        elif prev is None or prev["decision_next_day"] is None:
            status_today, streak = policy.OPEN, 0
        else:
            status_today = prev["decision_next_day"]
            carried = prev["streak"] or 0
            streak = carried if (status_today == policy.OPEN
                                 and prev["school_status"] == policy.OPEN) else 0
        text = ("The 2:00 pm reading is not taken yet."
                if today["reading_state"] == "before"
                else "Waiting for the 2:00 pm reading from DOE.")
        today_out = dict(today, school_status=status_today, decision_next_day=None,
                         streak=streak, reason="pending", reason_text=text)
    today_out["reading_state"] = today["reading_state"]
    today_out["next_date"] = (now.date() + timedelta(days=1)).isoformat()
    today_out["next_weekday"] = (now.date() + timedelta(days=1)).strftime("%a")

    filled = sorted(k for k in hourly if hourly[k])
    newest = filled[-1] if filled else None
    latest = None
    if newest:
        vals = {sid: hourly[newest].get(sid) for sid in ids}
        present = [v for v in vals.values() if v is not None]
        latest = {"datetime": newest, "values": vals,
                  "stations_used": len(present),
                  "average": sum(present) / len(present) if present else None}

    since = (now - timedelta(hours=96)).strftime(KEY)
    recent = []
    for hour in filled:
        if hour < since:
            continue
        vals = [hourly[hour].get(sid) for sid in ids]
        present = [v for v in vals if v is not None]
        recent.append({"t": hour, "v": vals,
                       "avg": round(sum(present) / len(present), 2)
                       if len(present) == len(ids) else None})

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M") + "+08:00",
        "school": cfg["school"],
        "reading_hour": cfg["reading_hour"],
        "stale_after_hours": cfg["collector"]["stale_after_hours"],
        "rule": rule,
        "stations": cfg["stations"],
        "latest": latest,
        "today": today_out,
        "daily": evaluated[-30:],
        "hourly": recent,
    }, evaluated


def daily_csv_text(evaluated, cfg):
    ids = [s["id"] for s in cfg["stations"]]
    names = [s["short"].lower().replace(" ", "_") for s in cfg["stations"]]
    head = ["date", "weekday"] + names + ["stations_used", "average",
                                          "school_status", "decision_next_day",
                                          "days_in_a_row", "reason"]
    lines = [",".join(head)]
    for r in evaluated:
        cells = [r["date"], r["weekday"]]
        cells += ["" if r["values"].get(s) is None else str(r["values"][s]) for s in ids]
        cells += [str(r["stations_used"]), fmt(r["average"]),
                  r["school_status"] or "", r["decision_next_day"] or "",
                  "" if r["streak"] is None else str(r["streak"]), r["reason"]]
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main

def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    ids = [s["id"] for s in cfg["stations"]]
    wanted = set(ids)
    states = sorted({s["state_id"] for s in cfg["stations"]})
    hh = cfg["reading_hour"]
    now = now_local(cfg)
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    log(f"Malaysia time now: {now:%Y-%m-%d %H:%M}")

    hourly = read_hourly(ids)
    before = hourly_csv_text(hourly, ids)

    def merge(rows):
        for sid, hour, api in rows:
            hourly.setdefault(hour, {})[sid] = api
        return len(rows)

    # 1. The newest 24 hours.
    got_current = 0
    table_ok = False
    for state in states:
        try:
            n = merge(fetch_window(this_hour, state, wanted))
            log(f"state {state}: {n} readings up to {this_hour:%H}:00")
            got_current += n
            table_ok = True
        except RuntimeError as err:
            log(f"state {state}: FAILED - {err}")
    if got_current == 0:
        log("hourly table gave nothing. Trying the DOE map service.")
        try:
            got_current = merge(fetch_current(wanted))
            log(f"map service: {got_current} readings")
        except RuntimeError as err:
            log(f"map service: FAILED - {err}")

    # 2. Fill gaps in the 2 pm readings of the last days.
    #    Skipped when the hourly table did not answer in step 1.
    backfill = cfg["collector"]["backfill_days"] if table_ok else 0
    for back in range(1, backfill + 1):
        day = (now - timedelta(days=back)).replace(hour=hh, minute=0, second=0, microsecond=0)
        have = hourly.get(day.strftime(KEY), {})
        if all(s in have for s in ids):
            continue
        for state in states:
            if all(s["id"] in have for s in cfg["stations"] if s["state_id"] == state):
                continue
            try:
                n = merge(fetch_window(day, state, wanted))
                log(f"backfill {day:%Y-%m-%d} state {state}: {n} readings")
            except RuntimeError as err:
                log(f"backfill {day:%Y-%m-%d} state {state}: FAILED - {err}")

    # 3. Save and compute.
    after = hourly_csv_text(hourly, ids)
    data_changed = write_text_if_changed(HOURLY_CSV, after) or before != after

    status, evaluated = build_status(hourly, cfg, now)
    write_text_if_changed(DAILY_CSV, daily_csv_text(evaluated, cfg))

    # Rewrite status.json only when something other than the clock changed.
    old = None
    if STATUS_JSON.exists():
        try:
            old = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
        except ValueError:
            old = None
    same = old is not None and {k: v for k, v in old.items() if k != "generated_at"} == \
        json.loads(json.dumps({k: v for k, v in status.items() if k != "generated_at"}))
    if not same:
        STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
        STATUS_JSON.write_text(json.dumps(status, indent=1) + "\n", encoding="utf-8")
        data_changed = True

    t = status["today"]
    newest = status["latest"]["datetime"] if status["latest"] else "none"
    summary = (f"{got_current} readings from DOE "
               f"({'hourly table' if table_ok else 'map service' if got_current else 'no source'}). "
               f"Newest reading {newest}. "
               f"Today {t['date']}: 2 pm reading {t['reading_state']}, "
               f"average {fmt(t['average']) or '-'}, school {t['school_status']}, "
               f"next day {t['decision_next_day'] or 'not decided'}. "
               f"{'Data changed' if data_changed else 'No change'}.")
    log(f"today {t['date']}: reading={t['reading_state']} average={fmt(t['average']) or '-'} "
        f"status={t['school_status']} next_day={t['decision_next_day']}")
    log("data changed" if data_changed else "no change")

    # On GitHub, show the result on the run page without opening the log.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = "notice" if got_current else "error"
        print(f"::{level} title=Haze collector::{summary}", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(f"### Haze collector\n\n{summary}\n")

    if got_current == 0:
        log("ERROR: no new readings from DOE in this run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
