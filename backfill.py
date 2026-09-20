#!/usr/bin/env python3
"""Replay a window of Home Assistant history into the weather app.

Use it to fill a gap left by an outage of the relay or the app.

    source .env
    python3 backfill.py --start 2026-07-16T22:25 --end 2026-07-17T20:10
    python3 backfill.py --start ... --end ... --dry-run

Each 5-minute slot is queried individually. That is more requests than asking
for the whole window at once, but Home Assistant caps the rows a single
history query returns, and a capped response silently loses the middle of a
long window. The slots are fetched concurrently so the extra requests cost
wall-clock time rather than patience.

Needs HA_TOKEN and WP_API_KEY in the environment; HA_URL and WP_URL have
defaults that can be overridden the same way.
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter, Retry

GRID_MINUTES = 5
UNAVAILABLE = {"unknown", "unavailable", "none", ""}

ENTITIES = {
    "temperature": "sensor.tasmota_bme280_temperature",
    "humidity": "sensor.tasmota_bme280_humidity",
    "pressure": "sensor.tasmota_bme280_seapressure",
}


def build_session(retries: int = 3) -> requests.Session:
    """A session that retries transient failures instead of losing a slot."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def slots_between(start: datetime, end: datetime) -> list[datetime]:
    """Every grid timestamp in ``[start, end]``, rounded up to the grid."""
    cursor = start.replace(second=0, microsecond=0)
    if cursor.minute % GRID_MINUTES:
        cursor += timedelta(minutes=GRID_MINUTES - cursor.minute % GRID_MINUTES)

    out = []
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(minutes=GRID_MINUTES)
    return out


def state_at(session: requests.Session, ha_url: str, token: str,
             entity_id: str, moment: datetime) -> float | None:
    """The sensor's value at ``moment``, or ``None`` if it had none.

    A one-second window is enough: the history API returns the last state
    change before the window as well as any inside it.
    """
    response = session.get(
        f"{ha_url}/api/history/period/{moment.isoformat()}",
        params={
            "end_time": (moment + timedelta(seconds=1)).isoformat(),
            "filter_entity_id": entity_id,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if response.status_code != 200:
        return None

    series = response.json()
    if not series or not series[0]:
        return None

    # Walk backwards: the last usable state is the one in effect at `moment`.
    for entry in reversed(series[0]):
        state = str(entry.get("state", "")).strip().lower()
        if state not in UNAVAILABLE:
            try:
                return float(state)
            except ValueError:
                continue
    return None


def reading_at(session, ha_url, token, moment) -> dict | None:
    """A complete reading for one slot, or ``None`` if any sensor is missing."""
    values = {
        name: state_at(session, ha_url, token, entity, moment)
        for name, entity in ENTITIES.items()
    }
    if any(value is None for value in values.values()):
        return None
    return {**values, "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%S")}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True,
                        help="window start, ISO-8601 local time (e.g. 2026-07-16T22:25)")
    parser.add_argument("--end", required=True, help="window end, inclusive")
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL", "http://homeassistant.local:8123"))
    parser.add_argument("--url", default=os.environ.get("WP_URL", "https://weather.wtzg.de/api/weather"),
                        help="the app's ingestion endpoint")
    parser.add_argument("--workers", type=int, default=6,
                        help="concurrent history requests (default: 6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, but do not post anything")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    ha_token = os.environ.get("HA_TOKEN")
    api_key = os.environ.get("WP_API_KEY")
    if not ha_token:
        print("Set HA_TOKEN in the environment", file=sys.stderr)
        return 1
    if not api_key and not args.dry_run:
        print("Set WP_API_KEY in the environment (or pass --dry-run)", file=sys.stderr)
        return 1

    try:
        start = datetime.fromisoformat(args.start)
        end = datetime.fromisoformat(args.end)
    except ValueError as exc:
        print(f"Could not parse the window: {exc}", file=sys.stderr)
        return 1
    if end < start:
        print("--end is before --start", file=sys.stderr)
        return 1

    slots = slots_between(start, end)
    if not slots:
        print("The window contains no grid slots.", file=sys.stderr)
        return 1
    print(f"Window: {len(slots)} slots from {slots[0]} to {slots[-1]}")

    session = build_session()
    readings: list[dict] = []
    print(f"Fetching history ({args.workers} concurrent requests)...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = pool.map(
            lambda moment: reading_at(session, args.ha_url, ha_token, moment), slots
        )
        for done, reading in enumerate(results, start=1):
            if reading:
                readings.append(reading)
            if done % 50 == 0 or done == len(slots):
                print(f"  {done}/{len(slots)} slots checked, {len(readings)} found")

    print(f"Found data for {len(readings)}/{len(slots)} slots")
    if not readings:
        print("Nothing to backfill.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run — nothing posted. First and last readings:")
        for reading in (readings[0], readings[-1]):
            print(f"  {reading}")
        return 0

    session.headers.update({"X-API-Key": api_key, "Content-Type": "application/json"})
    posted = 0
    failures: list[str] = []
    print(f"\nPosting {len(readings)} readings to {args.url} ...")
    for done, reading in enumerate(readings, start=1):
        try:
            response = session.post(args.url, json=reading, timeout=15)
            if response.status_code == 200:
                posted += 1
            else:
                failures.append(f"{reading['timestamp']}: HTTP {response.status_code} {response.text[:120]}")
        except requests.RequestException as exc:
            failures.append(f"{reading['timestamp']}: {exc}")
        if done % 50 == 0 or done == len(readings):
            print(f"  {done}/{len(readings)} posted")

    print(f"\nDone: {posted}/{len(readings)} readings backfilled.")
    if failures:
        print(f"{len(failures)} failed:", file=sys.stderr)
        for failure in failures[:20]:
            print(f"  {failure}", file=sys.stderr)
    return 0 if posted else 1


if __name__ == "__main__":
    sys.exit(main())
