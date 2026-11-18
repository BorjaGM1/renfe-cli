"""Exercise the network report through the CLI with a synthetic map and GTFS archive."""

import io
import json
import os
import zipfile

import httpx
import pytest
from typer.testing import CliRunner

from fakes import FakeRenfe, live_map, map_train
from renfe import cli, config, delays


def timetable(trips="", stops="", calendar=None, exceptions="daily,20270129,1"):
    tables = {
        "trips": ("service_id,trip_short_name,trip_id", trips),
        "stop_times": ("trip_id,stop_sequence,stop_id,arrival_time,departure_time", stops),
    }
    if calendar is not None:
        tables["calendar"] = ("start_date,end_date,monday,tuesday,wednesday,thursday,friday,saturday,sunday,service_id",
                              calendar)
    if exceptions is not None:
        tables["calendar_dates"] = ("service_id,date,exception_type", exceptions)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for table, (header, rows) in tables.items():
            lines = [header, *rows.splitlines()]
            archive.writestr(table + ".txt", "".join(line + "    \r\n" for line in lines))
    return data.getvalue()


def route(trip, departure="09:00:00", arrival="12:00:00", origin="60000", destination="71801"):
    # Store stops out of sequence and give arrival and departure different values.
    return (f"{trip},2,{destination},{arrival},\n"
            f"{trip},1,{origin},,{departure}")


def train(number="03100", delay=12, expected="2027-01-29T10:32:00", **kwargs):
    return map_train(number, delay, "60000", "04040", expected, **kwargs)


def install(monkeypatch, board, archive=b"", **overrides):
    handlers = {delays.TIMETABLE_URL: lambda request: httpx.Response(200, content=archive), **overrides}
    fake = FakeRenfe(live=board, **handlers)
    monkeypatch.setattr(cli, "make_client", lambda: cli.RsvClient(transport=httpx.MockTransport(fake), spacing=0))
    return fake


def run(*args):
    result = CliRunner().invoke(cli.app, ["delays", *args])
    assert result.exit_code == 0, result.stderr or result.exception
    return json.loads(result.stdout)


def test_lists_every_positive_delay_with_counts_and_one_shared_timetable(monkeypatch):
    board = live_map(train("03100", 12), train("00002", 12), train("00007", 29),
                     train("00003", 0), train("00004", -2), train("00005", "unknown"))
    # Two entries with identical timings still give an unambiguous answer.
    archive = timetable("daily,3100,one\ndaily,03100,two", route("one") + "\n" + route("two"))
    fake = install(monkeypatch, board, archive)
    report = run()
    assert [report[key] for key in ("mapped_trains", "delayed", "on_time", "early", "unknown")] == [6, 3, 1, 1, 1]
    assert report["map_updated"] == "2027-01-29T09:30:00+01:00" and report["notice"] is None
    assert report["retries"] == []
    assert [row["train"] for row in report["trains"]] == ["00007", "00002", "03100"]
    missing, _, row = report["trains"]
    assert missing["schedule_match"] == "unmatched"
    assert missing["scheduled_departure"] is missing["scheduled_arrival"] is None
    assert row["origin"]["code"] == "60000" and row["destination"]["code"] == "71801"
    assert row["next_station"]["expected_arrival"] == "2027-01-29T10:32:00+01:00"
    assert row["schedule_match"] == "exact" and row["delay_minutes"] == 12
    assert row["scheduled_departure"] == "2027-01-29T09:00:00+01:00"
    assert row["scheduled_arrival"] == "2027-01-29T12:00:00+01:00"
    assert [str(r.url) for r in fake.requests] == [config.LIVE_MAP_URL, delays.TIMETABLE_URL]


def test_distinct_schedules_are_ambiguous_and_station_order_matters(monkeypatch):
    trips = "daily,3100,one\ndaily,3100,two\ndaily,3200,reverse"
    stops = "\n".join((route("one"), route("two", arrival="12:30:00"),
                       route("reverse", origin="71801", destination="60000")))
    install(monkeypatch, live_map(train(), train("03200")), timetable(trips, stops))
    rows = run()["trains"]
    assert [row["schedule_match"] for row in rows] == ["ambiguous", "unmatched"]
    assert all(row["scheduled_departure"] is row["scheduled_arrival"] is None for row in rows)


def test_weekly_calendar_with_dated_additions_and_removals(monkeypatch):
    trips = "weekday,3100,base\ncancelled,3100,removed\nextra,3200,added\nweekend,3200,offday"
    dates = "20270101,20270131"
    weekly = "\n".join((dates + ",1,1,1,1,1,0,0,weekday", dates + ",0,0,0,0,1,0,0,cancelled",
                        dates + ",0,0,0,0,0,1,1,weekend"))
    exceptions = "cancelled,20270129,2\nextra,20270129,1"
    stops = "\n".join((route("base"), route("removed", arrival="13:00:00"),
                       route("added"), route("offday", arrival="14:00:00")))
    install(monkeypatch, live_map(train(), train("03200")), timetable(trips, stops, weekly, exceptions))
    assert [row["schedule_match"] for row in run()["trains"]] == ["exact", "exact"]


def test_overnight_trip_uses_previous_service_day(monkeypatch):
    trips = "daily,3100,night"
    archive = timetable(trips, route("night", "23:15:00", "25:30:00"),
                        calendar="20270101,20270131,1,1,1,1,1,1,1,daily", exceptions=None)
    seen = "2027-01-30T00:55:00"
    board = live_map(train(expected="2027-01-30T01:03:00", reported=seen), updated=seen)
    install(monkeypatch, board, archive)
    row, = run()["trains"]
    assert row["scheduled_departure"] == "2027-01-29T23:15:00+01:00"
    assert row["scheduled_arrival"] == "2027-01-30T01:30:00+01:00"


def test_late_train_after_midnight_can_have_both_scheduled_times_yesterday(monkeypatch):
    archive = timetable("daily,3100,late", route("late", "21:00:00", "23:45:00"))
    seen = "2027-01-30T02:45:00"
    board = live_map(train(delay=195, expected=None, reported=seen), updated=seen)
    install(monkeypatch, board, archive)
    row, = run()["trains"]
    assert row["scheduled_departure"] == "2027-01-29T21:00:00+01:00"
    assert row["scheduled_arrival"] == "2027-01-29T23:45:00+01:00"


@pytest.mark.parametrize("junction_departure,match", [("10:06:00", "linked"), ("12:30:00", "unmatched")])
def test_verified_number_pair_requires_a_timed_connection(monkeypatch, junction_departure, match):
    trips = "daily,04366,first\ndaily,04176,last"
    stops = "\n".join((route("first", "08:00:00", "10:00:00", "13200", "11200"),
                       route("last", junction_departure, "14:00:00", "11200", "17000")))
    board = live_map(train("04176", origin="13200", destination="17000"))
    install(monkeypatch, board, timetable(trips, stops))
    row, = run()["trains"]
    assert row["schedule_match"] == match
    if match == "linked":
        assert row["schedule_trains"] == ["04366", "04176"]
        assert row["scheduled_departure"] == "2027-01-29T08:00:00+01:00"
        assert row["scheduled_arrival"] == "2027-01-29T14:00:00+01:00"
    else:
        assert row["scheduled_departure"] is row["scheduled_arrival"] is None


@pytest.mark.parametrize("board", [live_map(), live_map(train(delay=0), train("00007", -2))])
def test_no_delays_skips_timetable(monkeypatch, board):
    fake = install(monkeypatch, board)
    report = run()
    assert report["trains"] == [] and report["delayed"] == 0 and report["notice"] is None
    assert [str(r.url) for r in fake.requests] == [config.LIVE_MAP_URL]


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, content=b"damaged")])
def test_timetable_failure_preserves_live_delays_and_does_not_cache_response(monkeypatch, response):
    install(monkeypatch, live_map(train()), **{delays.TIMETABLE_URL: lambda r: response})
    report = run()
    row, = report["trains"]
    assert row["schedule_match"] == "unavailable" and row["delay_minutes"] == 12
    assert row["scheduled_departure"] is row["scheduled_arrival"] is None
    assert report["notice"] and not (config.cache_dir() / "timetable.zip").exists()


def test_map_outage_returns_error_and_skips_timetable(monkeypatch):
    fake = install(monkeypatch, live_map(), **{config.LIVE_MAP_URL: lambda r: httpx.Response(503)})
    result = CliRunner().invoke(cli.app, ["delays"])
    assert result.exit_code == 5 and result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "upstream_unavailable" and error["retries"][0]["recovered"] is False
    assert [str(r.url) for r in fake.requests] == [config.LIVE_MAP_URL] * 3


def test_recovered_map_and_timetable_requests_are_logged_once_per_report(monkeypatch):
    board = live_map(train())
    archive = timetable("daily,3100,one", route("one"))
    maps = iter((httpx.Response(502), httpx.Response(200, json=board)))
    feeds = iter((httpx.Response(503), httpx.Response(200, content=archive)))
    install(monkeypatch, board, **{config.LIVE_MAP_URL: lambda r: next(maps), delays.TIMETABLE_URL: lambda r: next(feeds)})
    report = run()
    assert report["trains"][0]["schedule_match"] == "exact" and "retries" not in report["trains"][0]
    assert [(e["request"], e["tries"], e["recovered"]) for e in report["retries"]] == [
        ("live map", 2, True), ("Renfe timetable", 2, True)]


def test_only_timetable_is_cached_and_damaged_or_expired_cache_is_refetched(monkeypatch):
    archive = timetable("daily,3100,one", route("one"))
    path = config.cache_dir() / "timetable.zip"
    path.write_bytes(b"incomplete")
    fake = install(monkeypatch, live_map(train()), archive)
    assert run()["trains"][0]["schedule_match"] == "exact" and path.read_bytes() == archive
    fake.live = live_map(train(delay=31))
    assert run()["trains"][0]["delay_minutes"] == 31
    os.utime(path, (0, 0))
    assert run()["trains"][0]["schedule_match"] == "exact"
    assert [str(r.url) for r in fake.requests] == [
        config.LIVE_MAP_URL, delays.TIMETABLE_URL, config.LIVE_MAP_URL, config.LIVE_MAP_URL, delays.TIMETABLE_URL,
    ]


def ticket_routes():
    trips = "daily,00632,whole\ndaily,37632,middle\ndaily,37432,last"
    stops = "\n".join(("whole,1,04040,,08:00:00", "whole,2,11200,08:59:00,09:00:00",
                       "whole,3,10600,10:00:00,10:02:00", "whole,4,30100,12:00:00,",
                       route("middle", "09:00:00", "10:00:00", "11200", "10600"),
                       route("last", "10:02:00", "12:00:00", "10600", "30100")))
    return trips, stops


def test_live_and_ticket_numbers_find_the_same_row_with_verified_sections(monkeypatch):
    trips, stops = ticket_routes()
    board = live_map(train("00632", origin="04040", destination="30100"), train("11111", delay=5))
    fake = install(monkeypatch, board, timetable(trips, stops))
    row = run()["trains"][0]
    assert row["train"] == "00632" and row["schedule_match"] == "exact"
    assert [(s["train"], s["origin"]["code"], s["destination"]["code"],
             s["scheduled_departure"][11:16], s["scheduled_arrival"][11:16]) for s in row["number_segments"]] == [
        ("00632", "04040", "30100", "08:00", "12:00"), ("37632", "11200", "10600", "09:00", "10:00"),
        ("37432", "10600", "30100", "10:02", "12:00")]
    assert row["number_segments"][0]["origin"]["name"] == row["origin"]["name"]
    for number in ("632", "37432", "37632"):
        filtered = run(number)
        assert filtered["requested_train"] == number.zfill(5)
        assert (filtered["mapped_trains"], filtered["delayed"], filtered["matched_trains"]) == (2, 2, 1)
        assert filtered["trains"] == [row]
    assert len([r for r in fake.requests if str(r.url) == delays.TIMETABLE_URL]) == 1
    assert run("11111")["trains"][0]["number_segments"] == []  # Direct live number with no timetable.


@pytest.mark.parametrize("number,service,src,dst,dep,arr", [
    ("11111", "daily", "10600", "30100", "10:02:00", "12:00:00"),  # identical trip, outside published group
    ("37432", "offday", "10600", "30100", "10:02:00", "12:00:00"),
    ("37432", "daily", "10600", "30100", "10:03:00", "12:00:00"),
    ("37432", "daily", "30100", "10600", "10:02:00", "12:00:00"),
    ("37432", "daily", "10600", "71801", "10:02:00", "12:00:00"),  # only one shared station
])
def test_ticket_numbers_need_a_published_group_and_dated_timed_movement(monkeypatch, number, service, src, dst, dep, arr):
    _, whole_and_peers = ticket_routes()
    trips = f"daily,00632,whole\n{service},{number},candidate"
    stops = whole_and_peers + "\n" + route("candidate", dep, arr, src, dst)
    install(monkeypatch, live_map(train("00632", origin="04040", destination="30100")), timetable(trips, stops))
    assert [s["train"] for s in run()["trains"][0]["number_segments"]] == ["00632"]
    assert run(number)["trains"] == []


def test_multiple_stop_patterns_only_confirm_the_common_part(monkeypatch):
    # Identical endpoint times. One possible live trip uses a different station after Valladolid.
    trips, stops = ticket_routes()
    trips += "\ndaily,00632,other\ndaily,37432,long"
    stops += "\n" + "\n".join(("other,1,04040,,08:00:00", "other,2,11200,08:59:00,09:00:00",
                               "other,3,10600,10:00:00,10:02:00", "other,4,77777,11:00:00,11:02:00",
                               "other,5,30100,12:00:00,", "long,1,11200,,09:00:00",
                               "long,2,10600,10:00:00,10:02:00", "long,3,30100,12:00:00,"))
    install(monkeypatch, live_map(train("00632", origin="04040", destination="30100")), timetable(trips, stops))
    row, = run("37432")["trains"]
    assert [(s["origin"]["code"], s["destination"]["code"]) for s in row["number_segments"]
            if s["train"] == "37432"] == [("11200", "10600")]


def test_through_journey_requires_the_published_endpoints_and_all_legs(monkeypatch):
    trips = "daily,37632,one\ndaily,37432,two\ndaily,00632,three"
    legs = [route("one", "08:00:00", "09:00:00", "04040", "11200"),
            route("two", "09:10:00", "10:00:00", "11200", "10600"),
            route("three", "10:05:00", "12:00:00", "10600", "30100")]
    fake = install(monkeypatch, live_map(train("00632", origin="04040", destination="30100")),
                   timetable(trips, "\n".join(legs)))
    row, = run("37432")["trains"]
    assert row["schedule_match"] == "linked" and row["schedule_trains"] == ["37632", "37432", "00632"]
    # A valid change at Valladolid alone doesn't establish a different through journey.
    fake.live = live_map(train("00632", origin="11200", destination="30100"))
    assert run()["trains"][0]["schedule_match"] == "unmatched"


def test_ticket_lookup_uses_the_service_day_for_an_overnight_section(monkeypatch):
    trips = "night,00632,whole\nnight,37432,prior\ntoday,37632,today"
    stops = "\n".join(("whole,1,04040,,23:00:00", "whole,2,10600,24:05:00,24:10:00", "whole,3,30100,25:00:00,",
                       route("prior", "24:10:00", "25:00:00", "10600", "30100"),
                       route("today", "00:10:00", "01:00:00", "10600", "30100")))
    seen = "2027-01-30T00:45:00"
    install(monkeypatch, live_map(train("00632", origin="04040", destination="30100", expected=seen, reported=seen),
                                  updated=seen), timetable(trips, stops, exceptions="night,20270129,1\ntoday,20270130,1"))
    row, = run("37432")["trains"]
    assert row["number_segments"][-1]["scheduled_departure"] == "2027-01-30T00:10:00+01:00"
    assert run("37632")["matched_trains"] == 0


def test_ticket_number_status_also_finds_an_on_time_train(monkeypatch):
    trips, stops = ticket_routes()
    fake = install(monkeypatch, live_map(train("00632", delay=0, origin="04040", destination="30100")),
                   timetable(trips, stops))
    runner = CliRunner()
    direct = json.loads(runner.invoke(cli.app, ["status", "632"]).stdout)
    assert direct["train"] == "00632" and len(fake.requests) == 1
    status = json.loads(runner.invoke(cli.app, ["status", "37432"]).stdout)
    assert status["on_map"] and status["train"] == "00632" and status["requested_train"] == "37432"
    assert status["delay_minutes"] == 0 and len(fake.requests) == 3
    assert run("37432")["trains"] == []


def test_ticket_number_matching_multiple_live_trains_keeps_both(monkeypatch):
    trips, stops = ticket_routes()
    trips += "\ndaily,37632,shared"
    stops += "\n" + route("shared", "10:02:00", "12:00:00", "10600", "30100")
    install(monkeypatch, live_map(train("00632", origin="04040", destination="30100"),
                                  train("37632", origin="10600", destination="30100")), timetable(trips, stops))
    assert [row["train"] for row in run("37432")["trains"]] == ["00632", "37632"]
    result = CliRunner().invoke(cli.app, ["status", "37432"])
    out = json.loads(result.stdout)
    assert result.exit_code == 0 and out["matching_trains"] == ["00632", "37632"]
    assert out["delay_minutes"] is None


def test_invalid_filter_rejected_before_loading_the_map(monkeypatch):
    fake = install(monkeypatch, live_map())
    result = CliRunner().invoke(cli.app, ["delays", "00632+37432"])
    assert result.exit_code == 2 and not fake.requests


def test_groups_with_a_common_number_are_not_transitive(monkeypatch):
    # Renfe groups 37757 with 00757 and 00757 with 00613 on separate journeys.
    trips = "daily,37757,live\ndaily,00613,separate"
    install(monkeypatch, live_map(train("37757")), timetable(trips, route("live") + "\n" + route("separate")))
    assert [s["train"] for s in run()["trains"][0]["number_segments"]] == ["37757"]
    assert run("613")["matched_trains"] == 0


def test_timetable_failure_keeps_direct_results_and_explains_missing_ticket_lookup(monkeypatch):
    install(monkeypatch, live_map(train("00632", origin="04040", destination="30100")), archive=b"invalid")
    direct, = run("632")["trains"]
    assert direct["delay_minutes"] == 12 and direct["number_segments"] == []
    filtered = run("37432")
    assert filtered["matched_trains"] == 0 and filtered["notice"]
    status = CliRunner().invoke(cli.app, ["status", "37432"])
    out = json.loads(status.stdout)
    assert status.exit_code == 0 and out["delay_minutes"] is None and "lookup unavailable" in out["notice"]
