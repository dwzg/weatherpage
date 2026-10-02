"""The German weather service's open station archive, for training only.

The Deutscher Wetterdienst publishes ten-minute observations from several
hundred stations, free and without a key, going back decades: temperature,
humidity and station pressure from one instrument set, and the rain gauge
standing beside it. That is exactly this balcony's three measurements plus the
one it lacks, at the same kind of point, through every season — the training
set a single summer of balcony readings could never be.

Nothing here runs in the app. ``ml/train.py`` calls it in CI, where it is the
only part of the project that reaches outside the box.

Which stations are used is never printed and never written to a file. They
are the ones nearest the balcony, so their names — and above all their
distances — would put back into a public log the location the
``STATION_LATITUDE`` / ``STATION_LONGITUDE`` secrets exist to keep out of it.
Counts are safe to report; identities are not.
"""

from __future__ import annotations

import csv
import io
import math
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

CLIMATE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"


@dataclass(frozen=True)
class Product:
    """One of the service's observation products, and where its files live."""

    resolution: str  #: "10_minutes" or "hourly"
    directory: str
    stem: str
    listing: str     #: the station list's file name
    column: str = ""  #: the value an hourly product is read for

    @property
    def base(self) -> str:
        return f"{CLIMATE}/{self.resolution}/{self.directory}"

    @property
    def prefix(self) -> str:
        return "10minutenwerte" if self.resolution == "10_minutes" else "stundenwerte"


PRODUCTS = {
    # The sensors and the gauge, every ten minutes: what the models learn from.
    "air": Product("10_minutes", "air_temperature", "TU", "zehn_min_tu_Beschreibung_Stationen.txt"),
    "rain": Product("10_minutes", "precipitation", "nieder", "zehn_min_rr_Beschreibung_Stationen.txt"),
    # What the sky, the air and the weather did, every hour: the labels for
    # cloud (octas), fog (visibility in metres) and thunder (present-weather
    # codes). Observers made them until 2022; instruments since, which still
    # report cloud and visibility and no longer report thunder.
    "cloud": Product("hourly", "cloudiness", "N", "N_Stundenwerte_Beschreibung_Stationen.txt", "V_N"),
    "visibility": Product("hourly", "visibility", "VV", "VV_Stundenwerte_Beschreibung_Stationen.txt",
                          "V_VV"),
    "weather": Product("hourly", "weather_phenomena", "WW", "WW_Stundenwerte_Beschreibung_Stationen.txt",
                       "WW"),
}

#: The interval every reading closes. DWD stamps a ten-minute value with the
#: end of the ten minutes it covers, in UTC.
STEP = timedelta(minutes=10)

MISSING = -999.0


@dataclass(frozen=True)
class Station:
    id: str
    latitude: float
    longitude: float
    start: date
    end: date

    def km_from(self, latitude: float, longitude: float) -> float:
        """Great-circle distance, which is all choosing the nearest needs."""
        p1, p2 = math.radians(latitude), math.radians(self.latitude)
        dp, dl = p2 - p1, math.radians(self.longitude - longitude)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 6371.0 * 2 * math.asin(math.sqrt(a))


@dataclass
class Observations:
    """One station's ten-minute record, in UTC, oldest first.

    ``air`` holds ``(instant, temperature, humidity, pressure)``, only where
    all three were measured; ``rain`` maps each instant to the millimetres in
    the ten minutes ending there. A gap is an absent key, never a zero — an
    hour the gauge was down is not an hour it stayed dry.
    """

    air: list[tuple[datetime, float, float, float]]
    rain: dict[datetime, float]


# ── Fetching ───────────────────────────────────────────────────────────────


def fetch(url: str, attempts: int = 5, timeout: int = 120) -> bytes:
    """GET with backoff: opendata.dwd.de is reliable, but a weekly job is long."""
    delay = 5
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def cached(url: str, cache: Path, *, refresh: bool = False, name: str | None = None) -> bytes:
    """A file from the archive, kept in ``cache`` between runs.

    The historical files never change once published, so they are fetched
    once. The ``akt`` files are rewritten daily and ``refresh`` re-fetches them.
    """
    path = cache / (name or url.rsplit("/", 1)[-1])
    if path.exists() and not refresh:
        return path.read_bytes()
    data = fetch(url)
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def stations(product: str, cache: Path) -> list[Station]:
    """Every station that carries ``product``, from the service's own list.

    The list is fixed-width, and a station's name can hold spaces and commas
    ("Arolsen-Landau, Bad"), so the columns come from the dashed rule under
    the header rather than from splitting on whitespace. Only the leading
    numeric columns are read; the name never leaves this function.
    """
    spec = PRODUCTS[product]
    text = cached(f"{spec.base}/recent/{spec.listing}", cache, refresh=True).decode("latin-1")
    lines = text.splitlines()
    rule = next(i for i, line in enumerate(lines) if line.startswith("-----"))
    out = []
    for line in lines[rule + 1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        try:
            out.append(Station(
                id=parts[0],
                start=datetime.strptime(parts[1], "%Y%m%d").date(),
                end=datetime.strptime(parts[2], "%Y%m%d").date(),
                latitude=float(parts[4]),
                longitude=float(parts[5]),
            ))
        except ValueError:
            continue
    return out


def nearest(
    candidates: list[Station], latitude: float, longitude: float, *,
    since: date, current: date,
) -> list[Station]:
    """Stations whose record spans ``since`` to ``current``, nearest first."""
    usable = [s for s in candidates if s.start <= since and s.end >= current]
    return sorted(usable, key=lambda s: s.km_from(latitude, longitude))


def files(product: str, station_id: str, cache: Path, since: date) -> list[str]:
    """URLs of the station's files covering ``since`` onwards, oldest first.

    The historical archive is split into decade-ish zips whose names carry
    their date range; the ``akt`` zip carries roughly the last 500 days and is
    refreshed daily. Ranges overlap, which :func:`read_zip` callers resolve by
    letting the later file win.
    """
    spec = PRODUCTS[product]
    listing = cached(f"{spec.base}/historical/", cache, refresh=True,
                     name=f"listing-{product}.html").decode("latin-1")
    pattern = re.compile(
        rf'href="({spec.prefix}_{spec.stem}_{station_id}_(\d{{8}})_(\d{{8}})_hist\.zip)"'
    )
    hist = sorted(
        (m.group(2), m.group(1)) for m in pattern.finditer(listing)
        if datetime.strptime(m.group(3), "%Y%m%d").date() >= since
    )
    urls = [f"{spec.base}/historical/{name}" for _, name in hist]
    urls.append(f"{spec.base}/recent/{spec.prefix}_{spec.stem}_{station_id}_akt.zip")
    return urls


# ── Parsing ────────────────────────────────────────────────────────────────


def read_zip(data: bytes, columns: tuple[str, ...]) -> dict[datetime, tuple[float, ...]]:
    """``instant -> values`` from one of the service's zipped products.

    Values of -999 are the archive's "not measured"; they come back as NaN so
    a caller can tell a missing humidity from a missing row.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(n for n in archive.namelist() if n.startswith("produkt"))
        text = archive.read(name).decode("latin-1")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = [h.strip() for h in next(reader)]
    stamp = header.index("MESS_DATUM")
    wanted = [header.index(c) for c in columns]
    out: dict[datetime, tuple[float, ...]] = {}
    for row in reader:
        if len(row) <= max(wanted):
            continue
        raw = row[stamp].strip()
        # Ten-minute files stamp to the minute, hourly files to the hour.
        instant = datetime(
            int(raw[0:4]), int(raw[4:6]), int(raw[6:8]), int(raw[8:10]),
            int(raw[10:12]) if len(raw) >= 12 else 0, tzinfo=UTC,
        )
        values = []
        for i in wanted:
            v = float(row[i])
            values.append(math.nan if v == MISSING else v)
        out[instant] = tuple(values)
    return out


def has_barometer(station_id: str, cache: Path, share: float) -> bool:
    """Whether the station's recent air file reports pressure for ``share`` of its rows.

    Plenty of the service's ten-minute stations measure temperature and
    humidity with no barometer at all. Checking the small recent file first
    means such a station costs one download instead of its whole history and
    its gauge, which on the first real run was 13 stations' worth of both.
    The file is the one :func:`load` would fetch anyway, so it is cached for it.
    """
    spec = PRODUCTS["air"]
    url = f"{spec.base}/recent/{spec.prefix}_{spec.stem}_{station_id}_akt.zip"
    try:
        rows = read_zip(cached(url, cache, refresh=True), ("PP_10",))
    except urllib.error.HTTPError:
        return False
    present = sum(1 for (pressure,) in rows.values() if not math.isnan(pressure))
    return bool(rows) and present / len(rows) >= share


def load(station_id: str, cache: Path, since: date, rain: bool = True) -> Observations:
    """One station's air and rain record from ``since`` on, from the archive.

    ``rain=False`` skips the gauge, for a station only needed for a label it
    does not provide — thunder, at a station further out than the gauges used.
    """
    since_instant = datetime(since.year, since.month, since.day, tzinfo=UTC)

    air: dict[datetime, tuple[float, ...]] = {}
    for url in files("air", station_id, cache, since):
        air.update(read_zip(cached(url, cache, refresh=url.endswith("_akt.zip")),
                            ("TT_10", "RF_10", "PP_10")))
    return Observations(
        air=[
            (instant, t, min(h, 100.0), p)
            for instant, (t, h, p) in sorted(air.items())
            if instant >= since_instant and not (math.isnan(t) or math.isnan(h) or math.isnan(p))
        ],
        rain=load_rain(station_id, cache, since) if rain else {},
    )


def load_rain(station_id: str, cache: Path, since: date) -> dict[datetime, float]:
    """Only the gauge: what the verification scores against, needing no sensors."""
    since_instant = datetime(since.year, since.month, since.day, tzinfo=UTC)
    rain: dict[datetime, tuple[float, ...]] = {}
    for url in files("rain", station_id, cache, since):
        rain.update(read_zip(cached(url, cache, refresh=url.endswith("_akt.zip")),
                             ("RWS_10",)))
    return {
        instant: amount
        for instant, (amount,) in rain.items()
        if instant >= since_instant and not math.isnan(amount)
    }


def load_hourly(product: str, station_id: str, cache: Path, since: date) -> dict[datetime, float]:
    """One hourly observation series from ``since`` on: UTC hour -> value.

    Cloud cover comes in octas (-1 where fog hid the sky), visibility in
    metres, weather as the present-weather code (-1 where none was reported).
    A missing observation is an absent key, as everywhere else here.
    """
    spec = PRODUCTS[product]
    since_instant = datetime(since.year, since.month, since.day, tzinfo=UTC)
    out: dict[datetime, tuple[float, ...]] = {}
    for url in files(product, station_id, cache, since):
        try:
            data = cached(url, cache, refresh=url.endswith("_akt.zip"))
        except urllib.error.HTTPError as error:
            # The station lists name stations whose recent file is gone —
            # closed, or observing something else now. No file, no record.
            if error.code == 404:
                continue
            raise
        out.update(read_zip(data, (spec.column,)))
    return {
        instant: value
        for instant, (value,) in out.items()
        if instant >= since_instant and not math.isnan(value)
    }
