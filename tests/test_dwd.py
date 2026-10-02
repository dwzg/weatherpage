"""The weather service's archive format, checked without the network.

ml/dwd.py needs nothing but the standard library, so these run everywhere.
They pin down the details a silent misreading would hide in: which hour a
value belongs to, what counts as missing, and where each product lives.
"""

from __future__ import annotations

import io
import math
import sys
import urllib.error
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ml import dwd  # noqa: E402


def archive(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("Metadaten_Parameter.txt", "not the data")
        z.writestr("produkt_test_00433.txt", text.encode("latin-1"))
    return buffer.getvalue()


HOURLY = (
    "STATIONS_ID;MESS_DATUM;QN_8;V_N_I; V_N;eor\n"
    "        433;2025033000;    3;   I;   7;eor\n"
    "        433;2025033001;    3;   I;-999;eor\n"
    "        433;2025033002;    3;   I;  -1;eor\n"
)
TEN_MINUTE = (
    "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor\n"
    "        433;202503300010;    3; 1010.6;   4.7;   1.5;  66.5;  -1.0;eor\n"
)
WEATHER = (
    "STATIONS_ID;MESS_DATUM;QN_8;  WW;WW_Text;eor\n"
    "        433;2021070114;    1;  95;Gewitter mit Regen, Hagel oder Schnee;eor\n"
    "        433;2021070115;    1;  -1;Wetter wurde nicht gemeldet;eor\n"
)


class TestReading:
    def test_an_hourly_stamp_is_the_top_of_its_hour_in_utc(self):
        rows = dwd.read_zip(archive(HOURLY), ("V_N",))
        assert datetime(2025, 3, 30, 0, 0, tzinfo=UTC) in rows
        assert rows[datetime(2025, 3, 30, 0, 0, tzinfo=UTC)] == (7.0,)

    def test_a_ten_minute_stamp_keeps_its_minutes(self):
        rows = dwd.read_zip(archive(TEN_MINUTE), ("TT_10", "RF_10", "PP_10"))
        assert rows == {datetime(2025, 3, 30, 0, 10, tzinfo=UTC): (4.7, 66.5, 1010.6)}

    def test_not_measured_is_nan_not_a_number(self):
        rows = dwd.read_zip(archive(HOURLY), ("V_N",))
        assert math.isnan(rows[datetime(2025, 3, 30, 1, tzinfo=UTC)][0])

    def test_a_sky_hidden_by_fog_is_kept(self):
        """-1 octas is an observation — fog — not a gap."""
        rows = dwd.read_zip(archive(HOURLY), ("V_N",))
        assert rows[datetime(2025, 3, 30, 2, tzinfo=UTC)] == (-1.0,)

    def test_weather_codes_read_past_their_text(self):
        rows = dwd.read_zip(archive(WEATHER), ("WW",))
        assert rows[datetime(2021, 7, 1, 14, tzinfo=UTC)] == (95.0,)
        assert rows[datetime(2021, 7, 1, 15, tzinfo=UTC)] == (-1.0,)


class TestLoadingAnHourlySeries:
    def test_gaps_are_absent_and_the_window_is_respected(self, monkeypatch, tmp_path):
        url = "https://example/stundenwerte_N_00433_akt.zip"
        monkeypatch.setattr(dwd, "files", lambda *a: [url])
        monkeypatch.setattr(dwd, "cached", lambda *a, **k: archive(HOURLY))
        series = dwd.load_hourly("cloud", "00433", tmp_path, date(2025, 3, 30))
        assert series == {
            datetime(2025, 3, 30, 0, tzinfo=UTC): 7.0,
            datetime(2025, 3, 30, 2, tzinfo=UTC): -1.0,
        }
        assert dwd.load_hourly("cloud", "00433", tmp_path, date(2025, 3, 31)) == {}


class TestAMissingFile:
    def test_a_file_the_list_promised_but_the_server_lacks_is_no_record(
        self, monkeypatch, tmp_path
    ):
        def gone(url, *a, **k):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(dwd, "files", lambda *a: ["https://example/stundenwerte_WW_00433_akt.zip"])
        monkeypatch.setattr(dwd, "cached", gone)
        assert dwd.load_hourly("weather", "00433", tmp_path, date(2025, 1, 1)) == {}

    def test_any_other_failure_is_not_swallowed(self, monkeypatch, tmp_path):
        def down(url, *a, **k):
            raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)

        monkeypatch.setattr(dwd, "files", lambda *a: ["https://example/stundenwerte_WW_00433_akt.zip"])
        monkeypatch.setattr(dwd, "cached", down)
        with pytest.raises(urllib.error.HTTPError):
            dwd.load_hourly("weather", "00433", tmp_path, date(2025, 1, 1))


class TestWhereEachProductLives:
    def test_ten_minute_and_hourly_files_are_named_their_own_way(self):
        assert dwd.PRODUCTS["air"].base.endswith("/10_minutes/air_temperature")
        assert dwd.PRODUCTS["air"].prefix == "10minutenwerte"
        assert dwd.PRODUCTS["cloud"].base.endswith("/hourly/cloudiness")
        assert dwd.PRODUCTS["cloud"].prefix == "stundenwerte"

    def test_every_hourly_product_names_the_value_it_is_read_for(self):
        for name in ("cloud", "visibility", "weather"):
            assert dwd.PRODUCTS[name].column
