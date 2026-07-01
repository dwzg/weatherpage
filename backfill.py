#!/usr/bin/env python3
"""Backfill weather data from Home Assistant into the weatherpage.

Usage: source .env && python3 backfill.py

Fetches sensor state changes from HA's history API (without entity filter
to avoid API bugs), forward-fills to the 5-minute grid, and POSTs to weatherpage.
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

# ── Fetch all state changes from HA (no entity filter) ────────
def fetch_ha_states(entity_id, label):
    """Fetch all state changes, manually filter by entity_id.
    We query without filter_entity_id because HA sometimes drops
    state changes when that parameter is used."""
    url = f"{HA_URL}/api/history/period/{START}"
    params = {
        "end_time": END,
        "filter_entity_id": entity_id,
        "significant_changes_only": "0",  # return ALL state changes, not just significant ones
    }
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    resp = requests.get(url, params=params, headers=headers, timeout=60)
    resp.raise_for_status()
    all_data = resp.json()

    result = []
    for entity_data in all_data:
        if not entity_data:
            continue
        for s in entity_data:
            state = s.get("state", "")
            if state not in ("unknown", "unavailable"):
                try:
                    dt = datetime.fromisoformat(s["last_changed"][:19])
                    result.append((dt, float(state)))
                except (ValueError, KeyError):
                    pass

    result.sort()
    if result:
        print(f"  {label}: {len(result)} state changes, {result[0][0]} to {result[-1][0]}")
    else:
        print(f"  {label}: 0 state changes")
    return result

print("Fetching data from Home Assistant (this may take a moment)...")
temp_dts = fetch_ha_states(ENTITIES["temperature"], "Temp")
hum_dts  = fetch_ha_states(ENTITIES["humidity"], "Hum")
pres_dts = fetch_ha_states(ENTITIES["pressure"], "Pres")

if not temp_dts or not hum_dts or not pres_dts:
    print("ERROR: One or more sensors have no data.")
    sys.exit(1)

# ── Forward-fill to 5-minute grid ─────────────────────────────
def forward_fill(dt_list, target):
    """Return the most recent value at or before target time, or the
    earliest value after target if nothing is before it."""
    best = None
    for dt, val in dt_list:
        if dt <= target:
            best = val  # keep updating: last value before or at target
        else:
            break
    if best is not None:
        return best
    # Nothing before target, use first reading after
    return dt_list[0][1] if dt_list else None

all_dts = [d[0] for d in temp_dts] + [d[0] for d in hum_dts] + [d[0] for d in pres_dts]
start_dt = min(all_dts).replace(second=0, microsecond=0)
end_dt   = max(all_dts)
while start_dt.minute % 5 != 0:
    start_dt += timedelta(minutes=1)

grid_readings = []
slot = start_dt
while slot <= end_dt:
    t = forward_fill(temp_dts, slot)
    h = forward_fill(hum_dts, slot)
    p = forward_fill(pres_dts, slot)
    if t is not None and h is not None and p is not None:
        grid_readings.append((slot.strftime("%Y-%m-%dT%H:%M:%S"), t, h, p))
    slot += timedelta(minutes=5)

print(f"  Resampled to 5-min grid: {len(grid_readings)} slots")

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
            print(f"  [{i+1}/{len(grid_readings)}] Failed at {slot_str}: HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  [{i+1}/{len(grid_readings)}] Error at {slot_str}: {e}")

    if (i + 1) % 20 == 0 or i == len(grid_readings) - 1:
        print(f"  {i+1}/{len(grid_readings)} ...")

print(f"\nDone: {count}/{len(grid_readings)} readings backfilled.")
