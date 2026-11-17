"""Station parsing, the shipped list, the day-old cache of Renfe's live one, and name/code lookup."""

import json
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from renfe import stations
from renfe.client import RsvClient
from renfe.errors import ApiShapeError, BlockedError, UsageError

from fakes import NEW_STATION, STATIONS, FakeRenfe

EVERY = stations.parse(STATIONS)
REAL_SHIPPED = stations.bundled  # the real list; conftest gives every test a fake one


@pytest.mark.parametrize("query,code", [
    ("Madrid", "MADRI"),           # the whole-city group wins a plain city name
    ("madrid", "MADRI"),
    ("MADRI", "MADRI"),            # codes
    ("60000", "60000"),
    ("barcelona sants", "71801"),  # punctuation and case don't matter
    ("Barcelona-Sants", "71801"),
    ("Atocha", "60000"),           # word match: the main station beats the commuter one
    ("zaragoza", "04040"),
    ("Cercanias", "18000"),        # accents don't matter
])
def test_resolve(query, code):
    assert stations.resolve(query, EVERY).code == code


def test_resolve_unknown():
    with pytest.raises(UsageError, match="no station matches"):
        stations.resolve("Narnia", EVERY)


def test_resolve_ambiguous_lists_candidates():
    twins = stations.parse([
        {"_u_i_c_station_code": "11111", "name": "Villa Norte", "agglomeration_type": "commercial"},
        {"_u_i_c_station_code": "22222", "name": "Villa Sur", "agglomeration_type": "commercial"},
    ])
    with pytest.raises(UsageError, match="several stations") as exc:
        stations.resolve("villa", twins)
    assert "11111" in exc.value.message and "22222" in exc.value.message


def test_find_orders_best_first():
    assert [s.code for s in stations.find("madrid", EVERY)][:2] == ["MADRI", "60000"]


def test_parse_rejects_bad_shapes():
    with pytest.raises(ApiShapeError):
        stations.parse({"not": "a list"})
    with pytest.raises(ApiShapeError):
        stations.parse([{"name": "no code"}])


def test_load_caches_for_a_day(tmp_path):
    fake = FakeRenfe()
    cache = tmp_path / "stations.json"
    with RsvClient(transport=httpx.MockTransport(fake), spacing=0) as c:
        first = stations.load(c, path=cache)
        second = stations.load(c, path=cache)
    assert first == second == EVERY
    assert fake.paths().count("/catalog/masterdata/v1/stations") == 1
    saved = json.loads(cache.read_text())
    assert set(saved["stations"][0]) <= {"_u_i_c_station_code", "name", "province", "agglomeration_type",
                                         "main_station"}


def test_load_refetches_when_stale_or_asked(tmp_path):
    fake = FakeRenfe()
    cache = tmp_path / "stations.json"
    cache.write_text(json.dumps({"fetched_at": time.time() - 2 * 86400, "stations": STATIONS[:1]}))
    with RsvClient(transport=httpx.MockTransport(fake), spacing=0) as c:
        assert len(stations.load(c, path=cache)) == len(STATIONS)
        stations.load(c, path=cache, refresh=True)
    assert fake.paths().count("/catalog/masterdata/v1/stations") == 2


def test_corrupt_cache_is_ignored(tmp_path):
    cache = tmp_path / "stations.json"
    cache.write_text("{not json")
    with RsvClient(transport=httpx.MockTransport(FakeRenfe()), spacing=0) as c:
        assert stations.load(c, path=cache) == EVERY


def test_empty_station_list_is_an_api_change_and_not_cached(tmp_path):
    fake = FakeRenfe(**{"/catalog/masterdata/v1/stations": lambda r: httpx.Response(200, json=[])})
    cache = tmp_path / "stations.json"
    with RsvClient(transport=httpx.MockTransport(fake), spacing=0) as c, pytest.raises(ApiShapeError):
        stations.load(c, path=cache)
    assert not cache.exists()


def test_cache_from_the_future_is_stale(tmp_path):
    fake = FakeRenfe()
    cache = tmp_path / "stations.json"
    cache.write_text(json.dumps({"fetched_at": time.time() + 86400 * 365, "stations": STATIONS[:1]}))
    with RsvClient(transport=httpx.MockTransport(fake), spacing=0) as c:
        assert len(stations.load(c, path=cache)) == len(STATIONS)


def test_the_shipped_list_has_the_main_stations():
    every = REAL_SHIPPED()
    codes = [s.code for s in every]
    assert len(codes) > 1000 and len(set(codes)) == len(codes)
    assert {"MADRI", "BARCE", "60000", "71801", "17000"} <= set(codes)
    assert stations.resolve("Madrid", every).code == "MADRI"


def test_the_shipped_file_is_what_the_update_script_writes():
    """Sorted, one station per line, no hand edits: an update's diff shows only real changes."""
    text = (Path(stations.__file__).parent / stations.BUNDLE).read_text(encoding="utf-8")
    doc = json.loads(text)
    assert stations.bundle_text(doc["stations"], date.fromisoformat(doc["updated"])) == text


def test_bundle_text_keeps_what_parse_reads():
    text = stations.bundle_text(STATIONS, date(2026, 11, 17))
    doc = json.loads(text)
    assert doc["updated"] == "2026-11-17" and len(text.splitlines()) == len(STATIONS) + 2
    assert sorted(stations.parse(doc["stations"]), key=lambda s: s.code) == sorted(EVERY, key=lambda s: s.code)
    assert all(value not in (None, False, "") for item in doc["stations"] for value in item.values())
    with pytest.raises(ApiShapeError):
        stations.bundle_text([], date(2026, 11, 17))


def lookup(query, handler):
    with RsvClient(transport=httpx.MockTransport(handler), spacing=0) as c:
        return stations.lookup(query, c)


def test_lookup_in_the_shipped_list_sends_nothing():
    fake = FakeRenfe()
    assert lookup("Atocha", fake).code == "60000"
    assert fake.paths() == []


def test_lookup_asks_renfe_when_the_shipped_list_has_no_match():
    fake = FakeRenfe()
    assert lookup("apeadero nuevo", fake).code == NEW_STATION["_u_i_c_station_code"]
    with pytest.raises(stations.NoMatch):
        lookup("Narnia", fake)
    assert fake.paths().count("/catalog/masterdata/v1/stations") == 1  # the second miss used the day cache


def test_an_ambiguous_query_is_not_sent_to_renfe(monkeypatch):
    twins = stations.parse([{"_u_i_c_station_code": "11111", "name": "Villa Norte"},
                            {"_u_i_c_station_code": "22222", "name": "Villa Sur"}])
    monkeypatch.setattr(stations, "bundled", lambda: twins)
    fake = FakeRenfe()
    with pytest.raises(UsageError, match="several stations"):
        lookup("villa", fake)
    assert fake.paths() == []


def test_a_failed_live_lookup_reports_renfe_failure_not_no_match():
    fake = FakeRenfe(**{"/catalog/masterdata/v1/stations": lambda r: httpx.Response(429, json={})})
    with pytest.raises(BlockedError):
        lookup("Narnia", fake)
