#!/usr/bin/env python3
"""Backfill weather data from Home Assistant into the weatherpage.

Usage:
    HA_TOKEN=xxx WP_API_KEY=xxx python3 backfill.py

Fetches sensor data from HA's history API and POSTs it to the weatherpage.
Edit START/END below to match the outage window.
"""

import os
import sys
import requests

# ── Outage window (inclusive) ──────────────────────────────────
START = "2026-06-30T15:20:00"
END   = "2026-07-01T09:10:00"

# ── Configuration ──────────────────────────────────────────────
HA_URL = "http://homeassistant.local:8123"
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

# Align by timestamp (all three sensors must have a reading)
timestamps = sorted(set(temps) & set(hums) & set(pressures))
print(f"  Temperature readings: {len(temps)}")
print(f"  Humidity readings:    {len(hums)}")
print(f"  Pressure readings:    {len(pressures)}")
print(f"  Overlapping:          {len(timestamps)}")

if not timestamps:
    print("ERROR: No overlapping readings found. Check entity IDs and time range.")
    sys.exit(1)

# ── POST to weatherpage ────────────────────────────────────────
print(f"\nBackfilling {len(timestamps)} readings...")
count = 0
for ts in timestamps:
    payload = {
        "temperature": temps[ts],
        "humidity": hums[ts],
        "pressure": pressures[ts],
        "timestamp": ts,
    }
    try:
        resp = requests.post(
            WP_URL,
            json=payload,
            headers={"X-API-Key": WP_API_KEY, "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            count += 1
        else:
            print(f"  Failed at {ts}: HTTP {resp.status_code} {resp.text.strip()}")
    except requests.RequestException as e:
        print(f"  Error at {ts}: {e}")

    if count % 50 == 0 and count > 0:
        print(f"  ... {count}/{len(timestamps)}")

print(f"\nDone: {count}/{len(timestamps)} readings backfilled.")
