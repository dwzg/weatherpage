#!/usr/bin/env python3
"""Backfill weather data from Home Assistant into the weatherpage.

Usage: source .env && python3 backfill.py

Queries each 5-minute slot individually to avoid HA's per-query result limit.
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
    print("Set HA_TOKEN environment variable")
    sys.exit(1)
if not WP_API_KEY:
    print("Set WP_API_KEY environment variable")
    sys.exit(1)

ENTITIES = {
    "temperature": "sensor.tasmota_bme280_temperature",
    "humidity":    "sensor.tasmota_bme280_humidity",
    "pressure":    "sensor.tasmota_bme280_seapressure",
}

# ── Build 5-minute grid ───────────────────────────────────────
start_dt = datetime.fromisoformat(START)
end_dt   = datetime.fromisoformat(END)
# Round start up to next 5-min mark
start_dt = start_dt.replace(second=0, microsecond=0)
while start_dt.minute % 5 != 0:
    start_dt += timedelta(minutes=1)

slots = []
slot = start_dt
while slot <= end_dt:
    slots.append(slot)
    slot += timedelta(minutes=5)

print(f"Grid: {len(slots)} slots from {start_dt} to {end_dt}")

# ── Fetch state at each slot ──────────────────────────────────
# Query HA's history API with a tiny window around each slot.
# The API returns the last state change before the window plus
# any changes within it. We take the last state from the result.
def get_state_at(entity_id, dt):
    """Get the sensor value at a specific datetime."""
    # Use a 1-second window — just enough to get the state at that moment
    ts = dt.isoformat()
    ts_end = (dt + timedelta(seconds=1)).isoformat()
    resp = requests.get(
        f"{HA_URL}/api/history/period/{ts}",
        params={
            "end_time": ts_end,
            "filter_entity_id": entity_id,
        },
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data or not data[0]:
        return None
    # Last entry is the most recent state at or before our timestamp
    for s in reversed(data[0]):
        state = s.get("state", "")
        if state not in ("unknown", "unavailable"):
            try:
                return float(state)
            except ValueError:
                pass
    return None

# Cache per entity: if the state didn't change, reuse it
cache = {"temperature": (None, None), "humidity": (None, None), "pressure": (None, None)}

print("Fetching state at each slot (this may take a minute)...")
readings = []
for i, slot in enumerate(slots):
    t = get_state_at(ENTITIES["temperature"], slot)
    h = get_state_at(ENTITIES["humidity"], slot)
    p = get_state_at(ENTITIES["pressure"], slot)
    if t is not None and h is not None and p is not None:
        readings.append((slot.strftime("%Y-%m-%dT%H:%M:%S"), t, h, p))
    if (i + 1) % 20 == 0 or i == len(slots) - 1:
        print(f"  {i+1}/{len(slots)} slots checked, {len(readings)} found ...")

print(f"Found data for {len(readings)}/{len(slots)} slots")

# ── POST to weatherpage ────────────────────────────────────────
if not readings:
    print("No data to backfill!")
    sys.exit(1)

print(f"\nBackfilling {len(readings)} readings...")
count = 0
session = requests.Session()
session.headers.update({"X-API-Key": WP_API_KEY, "Content-Type": "application/json"})

for i, (slot_str, t, h, p) in enumerate(readings):
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
            print(f"  [{i+1}/{len(readings)}] Failed at {slot_str}: HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  [{i+1}/{len(readings)}] Error at {slot_str}: {e}")

    if (i + 1) % 20 == 0 or i == len(readings) - 1:
        print(f"  {i+1}/{len(readings)} ...")

print(f"\nDone: {count}/{len(readings)} readings backfilled.")
