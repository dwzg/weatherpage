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
def fetch_ha(entity_id):
    url = f"{HA_URL}/api/history/period/{START}"
    params = {"end_time": END, "filter_entity_id": entity_id, "minimal_response": ""}
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data and isinstance(data, list) and len(data) > 0:
        return {
            s["last_changed"][:19]: float(s["state"])
            for s in data[0]
            if s["state"] not in ("unknown", "unavailable")
        }
    return {}

print("Fetching data from Home Assistant...")
temps     = fetch_ha(ENTITIES["temperature"])
hums      = fetch_ha(ENTITIES["humidity"])
pressures = fetch_ha(ENTITIES["pressure"])

raw_timestamps = sorted(set(temps) & set(hums) & set(pressures))
print(f"  Temperature readings: {len(temps)}")
print(f"  Humidity readings:    {len(hums)}")
print(f"  Pressure readings:    {len(pressures)}")
print(f"  Overlapping raw:      {len(raw_timestamps)}")

if not raw_timestamps:
    print("ERROR: No overlapping readings found. Check entity IDs and time range.")
    sys.exit(1)

# ── Resample to 5-minute grid ──────────────────────────────────
start_dt = datetime.fromisoformat(raw_timestamps[0])
end_dt   = datetime.fromisoformat(raw_timestamps[-1])
start_dt = start_dt.replace(second=0, microsecond=0)
while start_dt.minute % 5 != 0:
    start_dt += timedelta(minutes=1)

# Pre-index raw timestamps as datetime objects for faster lookup
raw_dts = sorted(datetime.fromisoformat(ts) for ts in raw_timestamps)
raw_map = {ts: ts for ts in raw_timestamps}  # Keep original ts string

grid_readings = []
skipped_ranges = []
slot = start_dt
gap_start = None
while slot <= end_dt:
    slot_str = slot.strftime("%Y-%m-%dT%H:%M:%S")
    best_ts = None
    best_dist = timedelta(minutes=5)  # Use closest reading within 5 min
    for ts, orig_ts in raw_map.items():
        dt = datetime.fromisoformat(ts)
        dist = abs(dt - slot)
        if dist < best_dist:
            best_dist = dist
            best_ts = orig_ts
    if best_ts:
        grid_readings.append((slot_str, best_ts))
        if gap_start:
            skipped_ranges.append(f"{gap_start.strftime('%H:%M')}-{slot.strftime('%H:%M')}")
            gap_start = None
    else:
        if not gap_start:
            gap_start = slot
    slot += timedelta(minutes=5)

print(f"  Resampled to 5-min grid: {len(grid_readings)} slots")
if skipped_ranges:
    print(f"  Gaps (no HA data): {', '.join(skipped_ranges[:10])}")

# ── POST to weatherpage ────────────────────────────────────────
print(f"\nBackfilling {len(grid_readings)} readings...")
count = 0
session = requests.Session()
session.headers.update({"X-API-Key": WP_API_KEY, "Content-Type": "application/json"})

for i, (slot_str, ts) in enumerate(grid_readings):
    payload = {
        "temperature": temps[ts],
        "humidity": hums[ts],
        "pressure": pressures[ts],
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
