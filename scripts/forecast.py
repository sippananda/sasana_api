#!/usr/bin/env python3
"""Forecast the 2:00 pm API for the next days from the CAMS haze model.

For planning only. This forecast never feeds the school closure rule.
The rule uses the real DOE readings at 2:00 pm (scripts/collect.py).

How it works, for each forecast day and each CAMS grid cell:
  1. Take the CAMS PM2.5 forecast (hourly, from Open-Meteo).
     Average the 24 hours that end at 2:00 pm. The DOE API is a
     24-hour running average, so one hour alone is not comparable.
  2. Multiply by a calibration factor k. CAMS reads low in peat haze.
     k = median of (observed PM2.5 / CAMS PM2.5) over the newest haze days
     on record (up to 10 days, looking back up to 60 days).
     Days ahead use a mix of this factor and the newest real ratio.
     The same day starts from the newest real hourly reading instead
     (see station_pm).
  3. Convert PM2.5 to API with the DOE breakpoints.
  4. Average the four stations. Three stations share the KL cell, so
     the average is (3 x API_KL + API_KS) / 4.

The observed PM2.5 of a station is found from its API with the inverse
DOE formula. This assumes PM2.5 sets the API, which is true in haze.

Writes docs/data/forecast.json (for the page) and appends one line to
docs/data/forecast_history.jsonl (to check the forecast later).
All times are Malaysia time (UTC+8). Uses only the Python standard library.
"""
import json
import math
import os
import statistics
import sys
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
FORECAST_JSON = DATA / "forecast.json"
HISTORY_JSONL = DATA / "forecast_history.jsonl"

# Open-Meteo endpoints. The env variables exist only for testing.
AQ_URL = os.environ.get("OPEN_METEO_AQ_URL", "https://air-quality-api.open-meteo.com/v1/air-quality")
WX_URL = os.environ.get("OPEN_METEO_WX_URL", "https://api.open-meteo.com/v1/forecast")
USER_AGENT = "Mozilla/5.0 (compatible; SasanaAirQualityMonitor/1.0; school haze monitor)"

KEY = "%Y-%m-%dT%H:00"


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- DOE formula

# DOE breakpoints: PM2.5 (24-hour, ug/m3) to API.
# Source: DOE "API Calculation", the version on the APIMS portal
# (eqms.doe.gov.my/Documents/APIMS/API_Calculation.pdf). Used since 18 Oct 2026.
# An older DOE version (doe.gov.my, 2021) has 12.1-75.5 for API 51-100 and one
# band 75.5-150.4 for API 101-200. The hourly DOE readings of 8-18 Oct fit the
# APIMS version at all four stations; the 2021 version needs hourly peaks
# above 300 ug/m3 and does not fit Kuala Selangor at all.
# Each knot: (PM2.5, API). Inside a band the line is the DOE formula. The small
# gaps between bands (12.0-12.1, 50.4-50.5, ...) are joined by a straight line,
# so the conversion is monotonic in both directions.
KNOTS = [
    (0.0, 0.0),
    (12.0, 50.0), (12.1, 51.0),
    (50.4, 100.0), (50.5, 101.0),
    (55.4, 150.0), (55.5, 151.0),
    (150.4, 200.0), (150.5, 201.0),
    (250.4, 300.0), (250.5, 301.0),
    (350.4, 400.0), (350.5, 401.0),
    (500.4, 500.0),
]


def _interp(v, pairs):
    """Straight-line interpolation through (x, y) pairs; the last segment continues past the end."""
    for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
        if v <= x1:
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    (x0, y0), (x1, y1) = pairs[-2], pairs[-1]
    return y1 + (y1 - y0) * (v - x1) / (x1 - x0)


def api_from_pm(x):
    """API from a 24-hour PM2.5 value (ug/m3)."""
    if x is None:
        return None
    return _interp(max(0.0, float(x)), KNOTS)


def pm_from_api(a):
    """Inverse: PM2.5 (ug/m3) from an API value. Assumes PM2.5 sets the API."""
    if a is None:
        return None
    return _interp(max(0.0, float(a)), [(api, pm) for pm, api in KNOTS])


# ---------------------------------------------------------------- time

def now_local(cfg):
    forced = os.environ.get("MONITOR_NOW")
    if forced:
        return datetime.fromisoformat(forced)
    tz = timezone(timedelta(hours=cfg["timezone_offset_hours"]))
    return datetime.now(tz).replace(tzinfo=None)


# ---------------------------------------------------------------- network

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as err:
        raise RuntimeError(f"could not read {url.split('?')[0]}: {err}") from None


def hourly_series(payload, field):
    """{hour_key: value} from an Open-Meteo answer. Missing values are None."""
    h = payload.get("hourly") if isinstance(payload, dict) else None
    if not h or "time" not in h or field not in h:
        raise RuntimeError(f"unexpected answer from Open-Meteo (no hourly {field})")
    out = {}
    for t, v in zip(h["time"], h[field]):
        out[t[:13] + ":00"] = None if v is None else float(v)
    return out


def fetch_cams(cell, past_days, forecast_days):
    q = urllib.parse.urlencode({
        "latitude": cell["latitude"], "longitude": cell["longitude"],
        "hourly": "pm2_5", "timezone": "Asia/Kuala_Lumpur",
        "past_days": past_days, "forecast_days": forecast_days,
    })
    payload = get_json(f"{AQ_URL}?{q}")
    grid = (payload.get("latitude"), payload.get("longitude"))
    return hourly_series(payload, "pm2_5"), grid


def fetch_weather(school, forecast_days):
    q = urllib.parse.urlencode({
        "latitude": school["latitude"], "longitude": school["longitude"],
        "hourly": "wind_speed_10m,wind_direction_10m",
        "daily": "precipitation_sum",
        "models": "ecmwf_ifs025", "timezone": "Asia/Kuala_Lumpur",
        "past_days": 0, "forecast_days": forecast_days,
    })
    payload = get_json(f"{WX_URL}?{q}")
    wind = hourly_series(payload, "wind_speed_10m")
    wdir = hourly_series(payload, "wind_direction_10m")
    d = payload.get("daily") or {}
    rain = {}
    for t, v in zip(d.get("time", []), d.get("precipitation_sum", [])):
        rain[t[:10]] = None if v is None else float(v)
    return wind, wdir, rain


# ---------------------------------------------------------------- data

def read_hourly(ids):
    """{hour_key: {station_id: api}} from hourly.csv."""
    import csv
    data = {}
    if not HOURLY_CSV.exists():
        return data
    with open(HOURLY_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            hour = row.get("datetime_myt")
            if not hour:
                continue
            vals = {}
            for sid in ids:
                try:
                    vals[sid] = float(row[sid])
                except (KeyError, TypeError, ValueError):
                    pass
            if vals:
                data[hour] = vals
    return data


def trailing24(series, day, hh):
    """Mean of the 24 CAMS hours ending at hh:00 on `day`. None with too few values."""
    return trailing24_at(series, datetime.combine(day, datetime.min.time()).replace(hour=hh))


def trailing24_at(series, end):
    """Mean of the 24 CAMS hours ending at the hour `end`. None with too few values."""
    vals = []
    for back in range(24):
        v = series.get((end - timedelta(hours=back)).strftime(KEY))
        if v is not None:
            vals.append(v)
    if len(vals) < 20:
        return None
    return sum(vals) / len(vals)


# ---------------------------------------------------------------- calibration

def calibrate(cell, cams, hourly, cfg, now):
    """Calibration factors for one cell.

    ratio = observed PM2.5 (from the DOE API, inverse formula) / CAMS 24-hour PM2.5.

    Haze factor (used for the forecast): the median ratio of the newest
    `calibration_haze_days` haze days (observed API at or above `haze_day_api`),
    looking back up to `calibration_lookback_days`. Older haze days are kept on
    purpose: after a clean spell, the factor is still ready for the next haze.
    The range is the smallest and largest ratio of those days.

    Clean factor (for information): the median ratio of the clean days in the
    last `calibration_clean_days` days. CAMS often reads high on clean days.

    With fewer than `min_calibration_days` haze days on record (only at the very
    start), the factor comes from all days of the last `calibration_clean_days`.
    """
    fc = cfg["forecast"]
    hh = cfg["reading_hour"]
    rows = []
    for back in range(fc["calibration_lookback_days"], -1, -1):
        day = (now - timedelta(days=back)).date()
        stamp = datetime.combine(day, datetime.min.time()).replace(hour=hh)
        if stamp > now:
            continue
        obs = hourly.get(stamp.strftime(KEY), {})
        apis = [obs[s] for s in cell["stations"] if s in obs]
        if not apis:
            continue
        model = trailing24(cams, day, hh)
        if not model:
            continue
        obs_api = sum(apis) / len(apis)
        obs_pm = sum(pm_from_api(a) for a in apis) / len(apis)
        rows.append({"date": day.isoformat(), "obs_api": round(obs_api, 1),
                     "obs_pm": round(obs_pm, 1), "cams_pm": round(model, 1),
                     "ratio": round(obs_pm / model, 3), "haze": obs_api >= fc["haze_day_api"]})

    recent_from = (now - timedelta(days=fc["calibration_clean_days"])).date().isoformat()
    recent = [r for r in rows if r["date"] >= recent_from]
    haze_all = [r for r in rows if r["haze"]]
    haze = haze_all[-fc["calibration_haze_days"]:]
    clean = [r for r in recent if not r["haze"]]

    if len(haze) >= fc["min_calibration_days"]:
        used, basis = haze, "haze days"
    elif recent:
        used, basis = recent, "all days"
    else:
        used, basis = [], "none"
    used_dates = {r["date"] for r in used}
    shown = [dict(r, used=r["date"] in used_dates) for r in rows
             if r["date"] in used_dates or r["date"] >= recent_from]

    out = {"basis": basis, "n": len(used), "n_haze": len(haze_all),
           "first": used[0]["date"] if used else None,
           "last": used[-1]["date"] if used else None,
           "k_clean": round(statistics.median([r["ratio"] for r in clean]), 3) if clean else None,
           "n_clean": len(clean), "days": shown}
    if not used:
        out.update(k=1.0, min=0.7, max=1.8, spread=fc["spread_limits"][1])
    else:
        ratios = [r["ratio"] for r in used]
        k = statistics.median(ratios)
        # Day-to-day scatter, as a share of the factor. It sets the forecast range.
        lim = fc["spread_limits"]
        spread = statistics.stdev(ratios) / k if len(ratios) >= 3 else lim[1]
        out.update(k=round(k, 3), min=round(min(ratios), 3), max=round(max(ratios), 3),
                   spread=round(min(max(spread, lim[0]), lim[1]), 3))
    return out


# ---------------------------------------------------------------- forecast

def find_anchor(hourly, cams, cfg, now):
    """The newest real hourly reading, as a starting point for the forecast.

    Returns None when no hour with all four stations is found in the last
    `anchor_max_age_hours`. Otherwise, for each station:
      obs_pm  observed 24-hour PM2.5 now (inverse DOE formula from the API)
      ratio   obs_pm / CAMS 24-hour PM2.5 of its cell at the same hour
    """
    fc = cfg["forecast"]
    ids = [s["id"] for s in cfg["stations"]]
    hour = now.replace(minute=0, second=0, microsecond=0)
    for back in range(fc["anchor_max_age_hours"] + 1):
        t = hour - timedelta(hours=back)
        vals = hourly.get(t.strftime(KEY), {})
        if not all(sid in vals for sid in ids):
            continue
        out = {"time": t.strftime(KEY), "stations": {}}
        for s in cfg["stations"]:
            c_now = trailing24_at(cams[s["cell"]], t)
            if not c_now:
                return None
            obs_pm = pm_from_api(vals[s["id"]])
            out["stations"][s["id"]] = {"api": vals[s["id"]], "obs_pm": obs_pm, "cams_pm": c_now,
                                        "ratio": obs_pm / c_now}
        out["avg_api"] = policy.round_half_up(sum(vals[i] for i in ids) / len(ids))
        return out
    return None


def confidence(offset, cal, cams_ok, method=None, lead_hours=None):
    """offset: 1 = the first forecast day."""
    if not cams_ok:
        return "low"
    if method == "from latest reading" and lead_hours is not None and lead_hours <= 9:
        return "high"
    enough = min(c["n_haze"] for c in cal.values()) >= 7
    if offset <= 2 and enough:
        return "high"
    if offset <= 4:
        return "medium"
    return "low"


def station_pm(s, target, model, cal, anchor, fc):
    """Forecast 24-hour PM2.5 for one station: (mid, low, high, method, weight).

    Same day (the target is less than 24 hours after the newest reading):
      start from the real reading and add the change CAMS expects until the
      target hour, scaled by the haze factor:  obs + k x (CAMS_target - CAMS_now).
      Most of the 24 hours of the target average are then already measured.
    Later days: the factor is a mix of the newest real ratio of the station and
      the haze factor. The newest ratio counts less the further ahead the day is:
      weight = exp(-hours ahead / latest_ratio_hours).
    Without a recent reading: the haze factor alone.
    """
    c = cal[s["cell"]]
    m = model[s["cell"]]
    sp = c["spread"]
    if m is None:
        return None, None, None, None, None
    if anchor:
        a = anchor["stations"][s["id"]]
        t0 = datetime.fromisoformat(anchor["time"])
        lead = (target - t0).total_seconds() / 3600
        if lead < 24:
            delta = m - a["cams_pm"]
            vals = [max(0.0, a["obs_pm"] + c["k"] * f * delta) for f in (1, 1 - sp, 1 + sp)]
            return vals[0], min(vals), max(vals), "from latest reading", None
        w = math.exp(-lead / fc["latest_ratio_hours"])
        r = min(max(a["ratio"], fc["ratio_limits"][0]), fc["ratio_limits"][1])
        k_eff = w * r + (1 - w) * c["k"]
        return m * k_eff, m * k_eff * (1 - sp), m * k_eff * (1 + sp), "mixed factor", round(w, 2)
    return m * c["k"], m * c["k"] * (1 - sp), m * c["k"] * (1 + sp), "haze factor", 0.0


def build(cfg, now, cams, grids, cal, wind, wdir, rain, anchor=None):
    fc = cfg["forecast"]
    hh = cfg["reading_hour"]
    rule = cfg["rule"]
    stations = cfg["stations"]

    # The first forecast day is today when the 2 pm reading is still to come.
    first = now.date() if now.hour < hh else now.date() + timedelta(days=1)
    days = []
    for i in range(fc["forecast_days"] + 1):
        day = first + timedelta(days=i)
        if len(days) >= fc["forecast_days"]:
            break
        target = datetime.combine(day, datetime.min.time()).replace(hour=hh)
        model = {name: trailing24(cams[name], day, hh) for name in cams}
        cams_ok = all(v is not None for v in model.values())
        if not cams_ok and i > 0:
            # CAMS ends about 5 days ahead. Days without data are left out.
            continue
        api_mid, api_low, api_high = {}, {}, {}
        method = weight = None
        for s in stations:
            pm, lo, hi, method, weight = station_pm(s, target, model, cal, anchor, fc)
            api_mid[s["id"]] = None if pm is None else policy.round_half_up(api_from_pm(pm))
            api_low[s["id"]] = None if lo is None else policy.round_half_up(api_from_pm(lo))
            api_high[s["id"]] = None if hi is None else policy.round_half_up(api_from_pm(hi))

        def avg(d):
            vals = [v for v in d.values() if v is not None]
            return policy.round_half_up(sum(vals) / len(vals)) if len(vals) == len(d) else None
        mid, low, high = avg(api_mid), avg(api_low), avg(api_high)
        lead = None
        if anchor:
            lead = round((target - datetime.fromisoformat(anchor["time"])).total_seconds() / 3600)
        if method == "from latest reading" and mid is not None:
            # Hours still to come can differ from CAMS. Keep the range at least +-5.
            low, high = min(low, mid - 5), max(high, mid + 5)
        stamp = target.strftime(KEY)
        days.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%a"),
            "cams_pm25_24h": {n: None if v is None else round(v, 1) for n, v in model.items()},
            "api_2pm": api_mid,
            "avg_2pm": {"mid": mid, "low": low, "high": high},
            "method": method,
            "latest_ratio_weight": weight,
            "hours_after_latest_reading": lead,
            "weather_2pm": {
                "wind_kmh": None if wind.get(stamp) is None else round(wind[stamp], 1),
                "wind_from_deg": None if wdir.get(stamp) is None else round(wdir[stamp]),
                "rain_mm_day": None if rain.get(day.isoformat()) is None else round(rain[day.isoformat()], 1),
            },
            "flags": {
                "at_or_above_streak": mid is not None and mid >= rule["close_from_streak"],
                "at_or_above_single_day": mid is not None and mid >= rule["close_from_single_day"],
            },
            "confidence": confidence(i + 1, cal, cams_ok, method, lead),
        })
    return {
        "issued": now.strftime("%Y-%m-%dT%H:%M") + "+08:00",
        "reading_hour": hh,
        "cells": {n: {"latitude": g[0], "longitude": g[1],
                      "stations": [s["id"] for s in stations if s["cell"] == n]}
                  for n, g in grids.items()},
        "calibration": {
            "haze_days": fc["calibration_haze_days"],
            "lookback_days": fc["calibration_lookback_days"],
            "clean_days": fc["calibration_clean_days"],
            "haze_day_api": fc["haze_day_api"],
            **{n: {k: v for k, v in c.items() if k != "days"} for n, c in cal.items()},
            # One row per day and cell: what CAMS said and what DOE measured.
            "days": [dict(r, cell=n) for n, c in cal.items() for r in c["days"]],
        },
        "anchor": None if not anchor else {
            "time": anchor["time"], "avg_api": anchor["avg_api"],
            "stations": {sid: {"api": v["api"], "ratio": round(v["ratio"], 3)}
                         for sid, v in anchor["stations"].items()}},
        "days": days,
        "credit": "Contains modified Copernicus Atmosphere Monitoring Service information (CAMS, ECMWF). "
                  "Weather: ECMWF open data. Both via Open-Meteo.",
    }


# ---------------------------------------------------------------- main

def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    fc = cfg["forecast"]
    now = now_local(cfg)
    ids = [s["id"] for s in cfg["stations"]]
    log(f"Malaysia time now: {now:%Y-%m-%d %H:%M}")

    cells = {}
    for s in cfg["stations"]:
        cells.setdefault(s["cell"], {"stations": []})["stations"].append(s["id"])
    for name, c in fc["cells"].items():
        cells[name].update(latitude=c["latitude"], longitude=c["longitude"])

    cams, grids = {}, {}
    for name, cell in cells.items():
        cams[name], grids[name] = fetch_cams(cell, fc["past_days"], fc["forecast_days"] + 1)
        have = sum(v is not None for v in cams[name].values())
        log(f"CAMS cell {name} at {grids[name]}: {have} hourly values")
    try:
        wind, wdir, rain = fetch_weather(cfg["school"], fc["forecast_days"] + 1)
        log(f"weather: {len(rain)} days")
    except RuntimeError as err:
        log(f"weather: FAILED - {err}. The forecast goes on without weather.")
        wind, wdir, rain = {}, {}, {}

    hourly = read_hourly(ids)
    cal = {name: calibrate(cell, cams[name], hourly, cfg, now) for name, cell in cells.items()}
    for name, c in cal.items():
        log(f"calibration {name}: k={c['k']} range {c['min']}-{c['max']}, "
            f"{c['n']} days used ({c['basis']}), {c['n_haze']} haze days")

    anchor = find_anchor(hourly, cams, cfg, now)
    if anchor:
        log(f"latest reading {anchor['time']}: average {anchor['avg_api']}, ratio "
            + ", ".join(f"{sid} {v['ratio']:.2f}" for sid, v in anchor["stations"].items()))
    else:
        log("no recent reading with all stations. Haze factor alone.")
    out = build(cfg, now, cams, grids, cal, wind, wdir, rain, anchor)
    if not out["days"]:
        raise RuntimeError("no forecast day has CAMS data")

    DATA.mkdir(parents=True, exist_ok=True)
    FORECAST_JSON.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    line = {"issued": out["issued"],
            "k": {n: c["k"] for n, c in cal.items()},
            "anchor": out["anchor"]["time"] if out["anchor"] else None,
            "days": [[d["date"], d["avg_2pm"]["mid"], d["avg_2pm"]["low"], d["avg_2pm"]["high"]]
                     for d in out["days"]]}
    with open(HISTORY_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")

    text = ", ".join(f"{d['weekday']} {d['date'][5:]}: {d['avg_2pm']['mid']} "
                     f"({d['avg_2pm']['low']}-{d['avg_2pm']['high']})" for d in out["days"])
    summary = (f"Forecast issued {out['issued']}. 2 pm average: {text}. "
               + " ".join(f"k_{n}={c['k']} ({c['n']} days, {c['basis']})." for n, c in cal.items()))
    log(summary)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Haze forecast::{summary}", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(f"### Haze forecast\n\n{summary}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as err:
        log(f"Forecast failed: {err}")
        sys.exit(1)
