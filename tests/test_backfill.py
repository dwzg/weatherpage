"""Tests for the backfill helper's pure logic."""

from datetime import datetime

import pytest

import backfill


class TestSlots:
    def test_rounds_the_start_up_to_the_grid(self):
        slots = backfill.slots_between(
            datetime(2026, 7, 16, 22, 23), datetime(2026, 7, 16, 22, 40))
        assert slots[0] == datetime(2026, 7, 16, 22, 25)

    def test_is_inclusive_of_the_end(self):
        slots = backfill.slots_between(
            datetime(2026, 7, 16, 22, 25), datetime(2026, 7, 16, 22, 40))
        assert slots[-1] == datetime(2026, 7, 16, 22, 40)
        assert len(slots) == 4

    def test_every_slot_is_on_the_grid(self):
        slots = backfill.slots_between(
            datetime(2026, 7, 16, 1, 3), datetime(2026, 7, 16, 5, 47))
        assert all(s.minute % 5 == 0 and s.second == 0 for s in slots)

    def test_empty_when_the_window_holds_no_slot(self):
        assert backfill.slots_between(
            datetime(2026, 7, 16, 22, 26), datetime(2026, 7, 16, 22, 27)) == []


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for Home Assistant's history endpoint."""

    def __init__(self, states):
        self.states = states
        self.calls = []

    def get(self, url, params, headers, timeout):
        self.calls.append(params["filter_entity_id"])
        entity = params["filter_entity_id"]
        return FakeResponse(self.states.get(entity, [[]]))


class TestStateAt:
    MOMENT = datetime(2026, 7, 16, 22, 25)

    def read(self, states, entity="sensor.x"):
        session = FakeSession(states)
        return backfill.state_at(session, "http://ha", "token", entity, self.MOMENT)

    def test_reads_the_latest_usable_state(self):
        assert self.read({"sensor.x": [[{"state": "20.1"}, {"state": "21.5"}]]}) == 21.5

    def test_skips_unavailable_states(self):
        """Home Assistant reports these while a sensor is offline."""
        states = {"sensor.x": [[{"state": "21.5"}, {"state": "unavailable"}, {"state": "unknown"}]]}
        assert self.read(states) == 21.5

    def test_skips_non_numeric_states(self):
        assert self.read({"sensor.x": [[{"state": "20.0"}, {"state": "n/a"}]]}) == 20.0

    def test_none_when_nothing_usable(self):
        assert self.read({"sensor.x": [[{"state": "unavailable"}]]}) is None

    def test_none_when_the_series_is_empty(self):
        assert self.read({"sensor.x": [[]]}) is None

    def test_none_on_an_error_response(self):
        class Failing(FakeSession):
            def get(self, *a, **kw):
                return FakeResponse(None, status=500)
        assert backfill.state_at(Failing({}), "http://ha", "t", "sensor.x", self.MOMENT) is None


class TestReadingAt:
    MOMENT = datetime(2026, 7, 16, 22, 25)

    def test_complete_reading(self):
        session = FakeSession({e: [[{"state": "1.5"}]] for e in backfill.ENTITIES.values()})
        reading = backfill.reading_at(session, "http://ha", "t", self.MOMENT)
        assert reading == {"temperature": 1.5, "humidity": 1.5, "pressure": 1.5,
                           "timestamp": "2026-07-16T22:25:00"}

    def test_partial_reading_is_dropped(self):
        """A reading missing a metric would be stored as a hole, not a gap."""
        states = {e: [[{"state": "1.5"}]] for e in backfill.ENTITIES.values()}
        states[backfill.ENTITIES["pressure"]] = [[{"state": "unavailable"}]]
        assert backfill.reading_at(FakeSession(states), "http://ha", "t", self.MOMENT) is None


class TestArgs:
    def test_requires_a_window(self):
        with pytest.raises(SystemExit):
            backfill.parse_args([])

    def test_parses_a_window(self):
        args = backfill.parse_args(["--start", "2026-07-16T22:25", "--end", "2026-07-17T20:10"])
        assert args.start == "2026-07-16T22:25"
        assert args.dry_run is False

    def test_rejects_a_reversed_window(self, monkeypatch):
        # Set the credentials so the failure is the window, not a missing token.
        monkeypatch.setenv("HA_TOKEN", "token")
        monkeypatch.setenv("WP_API_KEY", "key")
        assert backfill.main(["--start", "2026-07-17T00:00", "--end", "2026-07-16T00:00"]) == 1

    def test_requires_credentials(self, monkeypatch):
        monkeypatch.delenv("HA_TOKEN", raising=False)
        assert backfill.main(["--start", "2026-07-16T00:00", "--end", "2026-07-17T00:00"]) == 1
