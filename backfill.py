#!/usr/bin/env python3
"""Backfill weather data from Home Assistant into the weatherpage.

Usage:
    HA_TOKEN=xxx WP_API_KEY=xxx python3 backfill.py

Fetches sensor data from HA's history API, resamples to 5-minute grid,
and POSTs to the weatherpage.
"""

import os
import sys
from datetime import datetime, timedelta
import requests

# ── Outage window (inclusive) ──────────────────────────────────
START = "2026-06-30T15:20:00"
END   = "2026-07-01T09:10:00"

# ── Configuration ──────────────────────────────────────────────
HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
WP_URL = "https://weather.wtzg.de/api/weather"

HA_TOKEN = os.environ.get("HA_TOKEN")
WP_API_KEY = os.environ.get("WP_API_KEY")

if not HA_TOKEN:
    print("Set HA_TOKEN environment variable (HA long-lived access token)")
    sys.exit(1)
if not WP_API_KEY:
    print("Set WP_API_KEY environment variable (weatherpage API key)")
    sys.exit(1)

ENTITIES = {
    "temperature": "sensor.tasmota_bme280_temperature",
    "humidity":    "sensor.tasmota_bme280_humidity",
    "pressure":    "sensor.tasmota_bme280_seapressure",
}

# ── Fetch data from HA ─────────────────────────────────────────
def fetch_ha(entity_id, label):
    url = f"{HA_URL}/api/history/period/{START}"
    params = {"end_time": END, "filter_entity_id": entity_id}
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data and isinstance(data, list) and len(data) > 0:
        states = data[0]
        times = [s["last_changed"] for s in states]
        result = {
            s["last_changed"][:19]: float(s["state"])
            for s in states
            if s["state"] not in ("unknown", "unavailable")
        }
        print(f"  {label}: {len(result)} readings, range {min(times)[:16]} to {max(times)[:16]}")
        return result
    print(f"  {label}: 0 readings")
    return {}

print("Fetching data from Home Assistant...")
temps     = fetch_ha(ENTITIES["temperature"], "Temp")
hums      = fetch_ha(ENTITIES["humidity"], "Hum")
pressures = fetch_ha(ENTITIES["pressure"], "Pres")

# Pre-index each sensor's data as (datetime, value) pairs for nearest-neighbor lookup
def to_dt_list(data):
    return sorted((datetime.fromisoformat(ts), val) for ts, val in data.items())

temp_dts = to_dt_list(temps)
hum_dts  = to_dt_list(hums)
pres_dts = to_dt_list(pressures)
print(f"  Temperature readings: {len(temps)}")
print(f"  Humidity readings:    {len(hums)}")
print(f"  Pressure readings:    {len(pressures)}")

if not temp_dts or not hum_dts or not pres_dts:
    print("ERROR: One or more sensors have no data. Check entity IDs and time range.")
    sys.exit(1)

# ── Resample each sensor independently to 5-minute grid ──────
def nearest(dt_list, target, max_dist=timedelta(minutes=5)):
    """Return (value, distance) from dt_list closest to target, or (None, None)."""
    best_val = None
    best_dist = max_dist
    for dt, val in dt_list:
        dist = abs(dt - target)
        if dist < best_dist:
            best_dist = dist
            best_val = val
    return best_val, best_dist

# Also find global nearest (no distance limit) for fallback
def nearest_any(dt_list, target):
    best_val = None
    best_dist = None
    for dt, val in dt_list:
        dist = abs(dt - target)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_val = val
    return best_val, best_dist

# Determine the overlapping time range
all_dts = [d[0] for d in temp_dts] + [d[0] for d in hum_dts] + [d[0] for d in pres_dts]
start_dt = min(all_dts)
end_dt   = max(all_dts)
start_dt = start_dt.replace(second=0, microsecond=0)
while start_dt.minute % 5 != 0:
    start_dt += timedelta(minutes=1)

grid_readings = []
skipped_ranges = []
stale_notes = []
slot = start_dt
gap_start = None
while slot <= end_dt:
    t, td = nearest(temp_dts, slot)
    h, hd = nearest(hum_dts, slot)
    p, pd = nearest(pres_dts, slot)

    # If any sensor lacks a close reading, fall back to nearest globally
    if t is None: t, td = nearest_any(temp_dts, slot)
    if h is None: h, hd = nearest_any(hum_dts, slot)
    if p is None: p, pd = nearest_any(pres_dts, slot)

    if t is not None and h is not None and p is not None:
        grid_readings.append((slot.strftime("%Y-%m-%dT%H:%M:%S"), t, h, p))
        if td and td > timedelta(minutes=5):
            stale_notes.append(f"temp at {slot.strftime('%H:%M')} is {td.seconds//60}min stale")
        if hd and hd > timedelta(minutes=5):
            stale_notes.append(f"hum at {slot.strftime('%H:%M')} is {hd.seconds//60}min stale")
        if pd and pd > timedelta(minutes=5):
            stale_notes.append(f"pres at {slot.strftime('%H:%M')} is {pd.seconds//60}min stale")
        if gap_start:
            skipped_ranges.append(f"{gap_start.strftime('%H:%M')}-{slot.strftime('%H:%M')}")
            gap_start = None
    else:
        if not gap_start:
            gap_start = slot
    slot += timedelta(minutes=5)

print(f"  Resampled to 5-min grid: {len(grid_readings)} slots")
if skipped_ranges:
    print(f"  Gaps (no sensor data at all): {', '.join(skipped_ranges[:15])}")
if stale_notes:
    print(f"  Stale readings (used nearest available):")
    for note in stale_notes[:10]:
        print(f"    {note}")
    if len(stale_notes) > 10:
        print(f"    ... and {len(stale_notes)-10} more")

# ── POST to weatherpage ────────────────────────────────────────
print(f"\nBackfilling {len(grid_readings)} readings...")
count = 0
session = requests.Session()
session.headers.update({"X-API-Key": WP_API_KEY, "Content-Type": "application/json"})

for i, (slot_str, t, h, p) in enumerate(grid_readings):
    payload = {
        "temperature": t,
        "humidity": h,
        "pressure": p,
        "timestamp": slot_str,
    }
    try:
        resp = session.post(WP_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            count += 1
        else:
            print(f"  [{i+1}/{len(grid_readings)}] Failed at {slot_str}: HTTP {resp.status_code} {resp.text.strip()}")
    except requests.RequestException as e:
        print(f"  [{i+1}/{len(grid_readings)}] Error at {slot_str}: {e}")

    if (i + 1) % 10 == 0 or i == len(grid_readings) - 1:
        print(f"  {i+1}/{len(grid_readings)} ...")

print(f"\nDone: {count}/{len(grid_readings)} readings backfilled.")
