"""The legacy shop's search before the cut-off: the four requests, its answer's parser, the mapping to
search()'s shape, and the fallback to the new system's partial list when it fails."""

import json
from datetime import date, time
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from renfe import cli, config, legacy, search
from renfe.client import RsvClient
from renfe.errors import ApiShapeError
from renfe.stations import parse

from fakes import LEGACY_SESSION, STATIONS, FakeRenfe, dwr_response, legacy_answer, legacy_leg, legacy_train

EVERY = {s.code: s for s in parse(STATIONS)}
NAMES = {code: s.name for code, s in EVERY.items()}
DAY = date(2026, 11, 20)


def run(fake, origin="MADRI", destination="BARCE", day=DAY, **kw):
    with RsvClient(transport=httpx.MockTransport(fake), spacing=0) as c:
        return search.search(c, EVERY[origin], EVERY[destination], day, names=NAMES, **kw)


def dwr_lines(request: httpx.Request) -> dict[str, str]:
    return dict(line.split("=", 1) for line in request.content.decode().splitlines() if "=" in line)


# --- the search ------------------------------------------------------------------------------------


def test_every_train_with_fares_cheapest_first():
    fake = FakeRenfe()
    out = run(fake)
    assert [t["service"] for t in out["trains"]] == ["00600+00500", "03100", "03200", "03300", "03400"]
    assert out["fares_included"] is True and out["notice"] is None and out["count"] == 5
    assert not fake.new_system_requests()
    direct = out["trains"][1]
    assert direct == {
        "service": "03100", "type": "AVE", "type_code": None,
        "departure": "2026-11-20T09:00:00+01:00", "arrival": "2026-11-20T11:40:00+01:00",
        "duration_minutes": 160, "changes": 0, "available": True,
        "fares": [{"name": "Básico", "price": 31.15, "currency": "EUR"},
                  {"name": "Prémium", "price": 79.8, "currency": "EUR"}],  # HTML entities decoded
        "intermediate_stops": None,  # the legacy shop doesn't say
    }
    assert [f["price"] for f in out["trains"][3]["fares"]] == [36.75, 41.5]  # sorted, not as sent
    assert "Texto libre" not in json.dumps(out)  # no server free text reaches the output


def test_a_train_with_a_change_has_one_leg_each():
    change = run(FakeRenfe())["trains"][0]
    assert change["changes"] == 1 and change["type"] == "MD + INTERCITY" and change["duration_minutes"] == 300
    assert change["departure"] == "2026-11-20T07:00:00+01:00" and change["arrival"] == "2026-11-20T12:00:00+01:00"
    first, second = change["legs"]
    assert first == {"service": "00600", "type_code": None,
                     "origin": {"code": "60000", "name": "Madrid-Puerta de Atocha-Almudena Grandes"},
                     "destination": {"code": "04040", "name": "Zaragoza-Delicias"},
                     "departure": "2026-11-20T07:00:00+01:00", "arrival": "2026-11-20T09:00:00+01:00",
                     "intermediate_stops": None}
    assert second["service"] == "00500" and second["origin"]["code"] == "04040"


def test_a_leg_keeps_its_own_date_and_arrival_after_midnight_is_the_next_day():
    late = legacy_train([legacy_leg("00700", "22:00", "23:50", destination="04040"),
                         dict(legacy_leg("00800", "00:20", "02:10", origin="04040"), fecha="2026-11-21")], 250,
                        [("Básico", "20")])
    overnight = run(FakeRenfe(legacy_trains=[late]))["trains"][0]
    assert overnight["legs"][1]["departure"] == "2026-11-21T00:20:00+01:00"
    assert overnight["arrival"] == "2026-11-21T02:10:00+01:00"
    full = run(FakeRenfe())["trains"][4]
    assert full["departure"] == "2026-11-20T22:30:00+01:00" and full["arrival"] == "2026-11-21T01:15:00+01:00"


def test_trains_not_on_sale_have_no_fares():
    trains = run(FakeRenfe())["trains"]
    assert [t["available"] for t in trains] == [True, True, False, True, False]
    assert trains[2]["fares"] == [] and trains[4]["fares"] == []


@pytest.mark.parametrize("reason,extra,fares,on_sale", [
    ("8", {}, True, True),  # what every train on sale carried
    ("", {}, True, True),
    (None, {}, True, False),  # the page shows anything but "" and "8" as not on sale
    ("3", {}, True, False),  # full
    ("8", {"completo": True}, True, False),
    ("4", {}, True, False),  # "not on sale at the moment"
    ("6", {}, True, False),  # doesn't run
    ("8", {"soloPlazaH": True, "plazaHDisponible": True}, True, False),  # only wheelchair places left
    ("8", {"soloPlazaH": True, "plazaHDisponible": False}, True, True),  # as the page reads it
    ("8", {}, False, False),
])
def test_on_sale_as_the_results_page_decides(reason, extra, fares, on_sale):
    row = legacy_train([legacy_leg("03100", "09:00", "11:40")], 160, [("Básico", "30")] if fares else (),
                       reason=reason, **extra)
    assert run(FakeRenfe(legacy_trains=[row]))["trains"][0]["available"] is on_sale


def test_without_fares_the_list_stays_complete():
    fake = FakeRenfe()
    out = run(fake, fares=False)
    assert out["count"] == 5 and out["notice"] is None and out["fares_included"] is False
    assert all(t["fares"] is None and t["available"] is None for t in out["trains"])
    assert out["trains"][1]["type"] == "AVE" and not fake.new_system_requests()


def test_after_and_limit():
    out = run(FakeRenfe(), after=time(9, 0), limit=2)
    assert [t["service"] for t in out["trains"]] == ["03100", "03200"]


# --- the requests ----------------------------------------------------------------------------------


def test_the_four_requests_of_the_results_page_and_nothing_else():
    fake = FakeRenfe()
    run(fake)
    requests = fake.legacy_requests()
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/vol/buscarTren.do"), ("GET", "/vol/buscarTrenEnlaces.do"),
        ("POST", "/vol/dwr/call/plaincall/__System.generateId.dwr"),
        ("POST", "/vol/dwr/call/plaincall/trainEnlacesManager.getTrainsList.dwr")]
    form = {k: v[0] for k, v in parse_qs(requests[0].content.decode(), keep_blank_values=True).items()}
    assert form["cdgoOrigen"] == "0071,MADRI,null" and form["cdgoDestino"] == "0071,BARCE,null"
    assert form["desOrigen"] == "MADRID (TODAS)" and form["FechaIdaSel"] == "20/11/2026"
    assert form["adultos_"] == "1" and form["sinEnlace"] == "false"
    assert requests[0].headers["origin"] == "https://www.renfe.com"
    assert "JSESSIONID=fake0001" in requests[1].headers["cookie"]  # the shop's session cookie, in memory
    trains_call = requests[3]
    lines = dwr_lines(trains_call)
    assert lines["c0-methodName"] == "getTrainsList" and lines["batchId"] == "1"
    assert unquote(lines["c0-e1"]) == "string:MADRI" and unquote(lines["c0-e3"]) == "string:20/11/2026"
    assert lines["scriptSessionId"].startswith(f"{LEGACY_SESSION}/")
    assert f"DWRSESSIONID={LEGACY_SESSION}" in trains_call.headers["cookie"]
    assert trains_call.headers["akamai-key"] == "MADRI, BARCE, 20/11/2026, , I, , 1, 0, 0, 0, 0, 0, 0, 0, , , , ,"


def test_a_single_station_is_keyed_as_on_www_renfe_com():
    fake = FakeRenfe()
    run(fake, origin="60000", destination="71801")
    form = parse_qs(fake.legacy_requests()[0].content.decode())
    assert form["cdgoOrigen"] == ["0071,60000,60000"] and form["cdgoDestino"] == ["0071,71801,71801"]


# --- when it fails: the new system's partial list, with a notice -----------------------------------

FAILURES = {
    "blocked": ("buscarTren.do", lambda r: httpx.Response(403), 1, "refused the request (403)"),
    "rate limited": ("getTrainsList", lambda r: httpx.Response(429), 4, "refused the request (429)"),
    "waiting room": ("buscarTren.do", lambda r: httpx.Response(302, headers={
        "location": "https://renfe.queue-it.net/?c=renfe&e=venta"}), 1, "waiting room"),
    "a malformed redirect": ("buscarTren.do", lambda r: httpx.Response(302, headers={"location": "https://["}),
                             1, "waiting room"),
    "a page instead of the redirect": ("buscarTren.do", lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html>queue</html>"), 1, "waiting room"),
    "a challenge instead of the results page": ("buscarTrenEnlaces.do", lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html>challenge</html>"), 2, "not the results page"),
    "server error": ("buscarTrenEnlaces.do", lambda r: httpx.Response(503), 2, "server error (503)"),  # not retried
    "timeout": ("generateId", lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), 3 + 3, "did not answer"),
    "connect timeout": ("generateId", lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")), 3,
                        "did not answer"),
    "no session id": ("generateId", lambda r: dwr_response('r.handleCallback("0","0",null);'), 3, "no session id"),
    "an HTML page for DWR": ("getTrainsList", lambda r: httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html></html>"), 4, "HTML page"),
    "a DWR exception": ("getTrainsList", lambda r: dwr_response(
        'r.handleException("1","0",{javaClassName:"java.lang.Throwable",message:"x"});'), 4, "failed to list"),
    "a changed answer": ("getTrainsList", lambda r: dwr_response('r.handleCallback("1","0",{a:new Date(1)});'),
                         4, "unreadable answer"),
    "the shop's own error": ("getTrainsList", lambda r: dwr_response(
        'r.handleCallback("1","0",{listadoTrenes:[{hayError:true,cdgoError:"E1"}]});'), 4, "reported an error"),
    "no trains": ("getTrainsList", lambda r: dwr_response(legacy_answer("2026-11-20", [])), 4,
                  "it listed no trains"),
    "no train list, as overnight on 19 Nov": ("getTrainsList", lambda r: dwr_response(
        'r.handleCallback("1","0",{actEtiqOferta:null,activoCuposAbonosRec:"SI",listadoTrenes:null});'), 4,
        "Renfe's shop isn't listing trains right now"),
}


@pytest.mark.parametrize("name", FAILURES)
def test_a_failure_falls_back_to_the_new_systems_list_with_a_notice(name):
    url_part, answer, sent, reason = FAILURES[name]
    fake = FakeRenfe(**{url_part: answer})
    out = run(fake)
    assert len(fake.legacy_requests()) == sent  # it stopped at the failure (a 500, 502 or read timeout started over)
    assert reason in out["notice"] and "legacy shop" in out["notice"] and "may be missing trains" in out["notice"]
    assert out["count"] == 4 and out["fares_included"] is False and all(t["fares"] is None for t in out["trains"])
    assert "Texto libre" not in out["notice"]


@pytest.mark.parametrize("origin,destination,leg", [
    ("60000", "71801", legacy_leg("03100", "09:00", "11:40", origin="18000")),
    ("60000", "71801", legacy_leg("03100", "09:00", "11:40", destination="04040")),
])
def test_an_answer_for_other_stations_falls_back(origin, destination, leg):
    row = legacy_train([leg], 160, [("Básico", "30")])
    out = run(FakeRenfe(legacy_trains=[row]), origin=origin, destination=destination)
    assert "the answer is for other stations" in out["notice"]


def test_a_city_group_takes_any_of_its_stations():
    row = legacy_train([legacy_leg("03100", "09:00", "11:40", origin="18000")], 160, [("Básico", "30")])
    assert run(FakeRenfe(legacy_trains=[row]))["notice"] is None  # MADRI -> BARCE


@pytest.mark.parametrize("price", ["31.15", "1.031,15", "", "31,155", None])
def test_an_unreadable_price_falls_back(price):
    row = legacy_train([legacy_leg("03100", "09:00", "11:40")], 160)
    row["tarifasDisponibles"] = [{"titulo": "Básico", "precioTarifa": price}]
    assert "a fare has no usable price" in run(FakeRenfe(legacy_trains=[row]))["notice"]


def test_a_bug_is_never_hidden_by_the_fallback(monkeypatch):
    monkeypatch.setattr(legacy, "_parse_trains", lambda *args: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        run(FakeRenfe())


def test_cli_search_falls_back_with_exit_0(monkeypatch):
    from datetime import datetime as real_datetime

    from typer.testing import CliRunner

    class Frozen(real_datetime):  # "now" before the search date, so it never becomes a date in the past
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 15, 10, 0, tzinfo=search.MADRID)

    monkeypatch.setattr(cli, "datetime", Frozen)
    handler = FakeRenfe(**{"buscarTren.do": lambda r: httpx.Response(403)})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = CliRunner().invoke(cli.app, ["search", "Madrid", "Barcelona", "-d", "2026-11-20"])
    assert result.exit_code == 0, result.output
    assert "legacy shop" in json.loads(result.stdout)["notice"]


# --- the answer's parser ---------------------------------------------------------------------------


def test_reads_a_javascript_object_literal():
    text = r'x({a:1,"b c":[true,false,null,-2.5,1e3],s:"q\"b\\s\/n\né\'",e:{},l:[ ]});'
    value, end = legacy.read_literal(text, 2)
    assert value == {"a": 1, "b c": [True, False, None, -2.5, 1000.0], "s": "q\"b\\s/n\né'", "e": {}, "l": []}
    assert text[end:] == ");"


def test_a_surrogate_pair_is_one_character():
    assert legacy.read_literal(r'"🚆"')[0] == "\U0001f686"


@pytest.mark.parametrize("text", [
    "{a:new Date(1)}", "{a:s0}", "{a:1,}", "{a:'x'}", '{a:"x}', r'{a:"\x41"}', r'{a:"\u12"}', "[1 2]",
    "NaN", "{1:2}", "[" * 50 + "]" * 50, '"a\nb"', "", "}",
])
def test_anything_else_is_unreadable(text):
    with pytest.raises(ApiShapeError):
        legacy.read_literal(text)


def test_every_legacy_request_names_the_cli():
    fake = FakeRenfe()
    run(fake)
    agents = {r.headers["user-agent"] for r in fake.requests if r.url.host == "venta.renfe.com"}
    assert agents and all(agent.startswith("renfe-cli/") for agent in agents)


def test_a_search_waits_for_its_slot_before_any_request(monkeypatch):
    fake, calls = FakeRenfe(), []
    monkeypatch.setattr(config, "SEARCH_SPACING", 1.0)
    monkeypatch.setattr(search, "search_slot", lambda spacing, path: calls.append((spacing, path.name, len(fake.requests))))
    run(fake)
    assert calls == [(1.0, "search.lock", 0)]
