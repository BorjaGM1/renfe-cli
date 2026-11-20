"""Renfe's legacy shop (venta.renfe.com/vol): every train, with fares for one adult, for trips before the
cut-off (config.NSV_FIRST_SALE_DATE). Before it, Renfe's new system lists only some trains and prices none.
Search only. Once the cut-off has passed, delete this module with the rest of the legacy-shop code (issue #1).

A guest search, sent the way the shop's results page sends it (observed 2026-11-17, nothing selected or bought):
  1. POST the www.renfe.com search form to buscarTren.do, which answers 302 to the results page;
  2. GET the results page, buscarTrenEnlaces.do (an empty shell its script fills in);
  3. DWR __System.generateId: the page's DWR session id;
  4. DWR trainEnlacesManager.getTrainsList(filter): the trains, as a JavaScript object literal, read by a
     small parser here and never evaluated.
The same DWR interface also selects a train and validates fares, which start the booking flow: never called.

Cookies live in memory for the one search. A one-off failure at any step (a 500 or 502 without Retry-After, or a
connection that drops or is refused) starts the search over from step 1, with a new guest session and fresh cookies,
up to 2 more times (retry.py; seen on 18 Nov 2026: the results page answered 500 once). Those count whatever their
body: the shop's own pages are HTML. Nothing else is retried: a 503 or 504 (here it may be an edge deny or a waiting
room), a 403/429, a redirect anywhere but the results page (a waiting room), an HTML page where DWR was expected, a
timeout, or an answer that doesn't parse raises a RenfeError at once, and search() falls back to the new system's
partial list.
"""

import html
import json
import re
import secrets
import string
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from . import config, retry
from .errors import ApiShapeError, BlockedError, UpstreamError
from .stations import Station

BASE = "https://venta.renfe.com/vol"
SEARCH_URL = f"{BASE}/buscarTren.do?Idioma=es&Pais=ES"
RESULTS_PATH = "/vol/buscarTrenEnlaces.do"
RESULTS_URL = f"https://venta.renfe.com{RESULTS_PATH}"
SESSION_URL = f"{BASE}/dwr/call/plaincall/__System.generateId.dwr"
TRAINS_URL = f"{BASE}/dwr/call/plaincall/trainEnlacesManager.getTrainsList.dwr"
WWW = "https://www.renfe.com"
MADRID = ZoneInfo(config.TIMEZONE)  # the shop gives wall-clock times, no zone
ADMIN = "0071"  # Renfe's administration code, the first part of every station key on www.renfe.com

_SESSION_ID = re.compile(r'r\.handleCallback\("0","0","([A-Za-z0-9$*_.!-]{8,64})"\)')
_TRAINS_CALL = 'r.handleCallback("1","0",'
_PRICE = re.compile(r"([0-9]{1,5})(?:,([0-9]{1,2}))?")  # "31,15", "59,6", "149,0"
_HOUR = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])(?::[0-5][0-9])?")
_ON_SALE = ("", "8")  # razonNoDisponible values the results page sells (see _on_sale)


# --- the search ------------------------------------------------------------------------------------


def trains(origin: Station, destination: Station, day: date, names: dict[str, str], *,
           transport: httpx.BaseTransport | None = None, retries: list[dict] | None = None) -> list[dict]:
    """Every train the legacy shop sells from `origin` to `destination` on `day`, with fares, in the shape
    search() builds (legs keep `_dep_utc` for sorting). A search that had to start over is logged in `retries`."""
    return retry.call("legacy shop search (all four requests, started over)",
                      lambda: _search(origin, destination, day, names, transport), retries)


def _search(origin: Station, destination: Station, day: date, names: dict[str, str],
            transport: httpx.BaseTransport | None) -> list[dict]:
    """One guest search: a new client, so new cookies, and the four requests."""
    with httpx.Client(
        transport=transport,
        http2=transport is None,
        timeout=config.REQUEST_TIMEOUT,
        follow_redirects=False,
        headers={"user-agent": config.USER_AGENT, "accept-language": config.ACCEPT_LANGUAGE},
    ) as http:
        _open_results(http, origin, destination, day)
        session = _dwr_session(http)
        reply = _train_list(http, origin, destination, day, session)
    return _parse_trains(reply, origin, destination, names)


def _send(http: httpx.Client, method: str, url: str, label: str, **kw: Any) -> httpx.Response:
    try:
        response = http.request(method, url, **kw)
    except httpx.TimeoutException:  # never retried: it already cost the whole timeout
        raise UpstreamError(f"{label}: Renfe did not answer in time") from None
    except httpx.RequestError as exc:
        raise UpstreamError(f"{label}: request failed ({type(exc).__name__})",
                            transient=_step(label, retry.connection_failure(exc))) from None
    status = response.status_code
    if status in (403, 429):
        raise BlockedError(f"{label}: Renfe refused the request ({status}); wait before trying again", status=status)
    if status >= 500:  # a 503 or 504 here may be an edge deny or a waiting room, and Retry-After asks to wait
        again = None if "retry-after" in response.headers else retry.status_failure(status, retry.LEGACY_STATUSES)
        raise UpstreamError(f"{label}: Renfe returned a server error ({status})", status=status,
                            transient=_step(label, again))
    return response


def _step(label: str, failure: str | None) -> str | None:
    """Which of the four requests failed, for the retries log: "legacy shop results page: server error (500)"."""
    return f"{label}: {failure}" if failure else None


def _station_key(station: Station) -> str:
    """A station's key as www.renfe.com's list mostly gives it: "0071,MADRI,null" for a city's stations,
    "0071,04040,04040" for one. Its list differs for about a quarter of the stations (a null or another
    number last, another country's code first); the shop answered "0071,60000,60000" for Atocha, whose key
    there ends in 00600, with Atocha's trains. _parse_trains checks the answer is for the stations asked."""
    return f"{ADMIN},{station.code},{'null' if station.group else station.code}"


def search_form(origin: Station, destination: Station, day: date) -> dict[str, str]:
    """The one-way, one-adult search posted by both the CLI and browser checkout."""
    shown = day.strftime("%d/%m/%Y")
    return {
        "tipoBusqueda": "autocomplete", "currenLocation": "menuBusqueda", "vengoderenfecom": "SI",
        "desOrigen": origin.name.upper(), "desDestino": destination.name.upper(),
        "cdgoOrigen": _station_key(origin), "cdgoDestino": _station_key(destination),
        "idiomaBusqueda": "ES", "FechaIdaSel": shown, "FechaVueltaSel": "", "_fechaIdaVisual": shown,
        "_fechaVueltaVisual": "", "minPriceDeparture": "false", "minPriceReturn": "false", "adultos_": "1",
        "ninos_": "0", "ninosMenores": "0", "codPromocional": "", "plazaH": "false", "sinEnlace": "false",
        "conMascota": "false", "conBicicleta": "false", "asistencia": "false", "Idioma": "es", "Pais": "ES",
        "franjaHoraI": "", "franjaHoraV": "",
    }


def results_redirect(status: int, location: str) -> str:
    """Accept only the results page of the legacy shop."""
    try:
        results = urljoin(SEARCH_URL, location)
        target = urlsplit(results)
        where = target.scheme, target.netloc, target.path
    except ValueError:
        where = None
    if status != 302 or where != ("https", "venta.renfe.com", RESULTS_PATH):
        raise BlockedError(f"legacy shop search: Renfe didn't send the search to its results page ({status}); "
                           "a waiting room or bot protection?", status=status)
    return results


def _open_results(http: httpx.Client, origin: Station, destination: Station, day: date) -> None:
    """Steps 1-2: the www.renfe.com form, then the results page it redirects to."""
    form = search_form(origin, destination, day)
    response = _send(http, "POST", SEARCH_URL, "legacy shop search", content=urlencode(form), headers={
        "content-type": "application/x-www-form-urlencoded", "origin": WWW, "referer": f"{WWW}/"})
    results = results_redirect(response.status_code, response.headers.get("location", ""))
    label = "legacy shop results page"
    response = _send(http, "GET", results, label, headers={"referer": f"{WWW}/"})
    if response.status_code != 200 or "trainEnlacesManager" not in response.text:
        raise BlockedError(f"{label}: not the results page ({response.status_code}); a waiting room or bot "
                           "protection?", status=response.status_code)


def _dwr(http: httpx.Client, url: str, label: str, lines: list[str], headers: dict[str, str] | None = None) -> str:
    response = _send(http, "POST", url, label, content="\n".join(lines) + "\n", headers={
        "content-type": "text/plain", "origin": "https://venta.renfe.com", "referer": RESULTS_URL, **(headers or {})})
    ctype = response.headers.get("content-type", "").lower()
    if "html" in ctype or response.status_code != 200:
        raise BlockedError(f"{label}: got {'an HTML page' if 'html' in ctype else response.status_code} instead "
                           "of a DWR answer (bot protection?)", status=response.status_code)
    return response.text


def _dwr_session(http: httpx.Client) -> str:
    """Step 3: the DWR session id, which the page's script also keeps as the DWRSESSIONID cookie."""
    text = _dwr(http, SESSION_URL, "legacy shop session", [
        "callCount=1", "c0-scriptName=__System", "c0-methodName=generateId", "c0-id=0", "batchId=0",
        "instanceId=0", f"page={quote(RESULTS_PATH, safe='')}", "scriptSessionId=", "windowName="])
    match = _SESSION_ID.search(text)
    if not match:
        raise ApiShapeError("legacy shop session: no session id in the answer")
    http.cookies.set("DWRSESSIONID", match.group(1), domain="venta.renfe.com", path="/vol")
    return match.group(1)


def _train_list(http: httpx.Client, origin: Station, destination: Station, day: date, session: str) -> Any:
    """Step 4: getTrainsList for one adult, one way, connections included."""
    fields = {
        "origen": origin.code, "destino": destination.code, "fechaSalida": day.strftime("%d/%m/%Y"),
        "fechaVuelta": "", "trayecto": "I", "idaVuelta": "", "adultos": "1", "ninos": "0", "ninosMenores": "0",
        "sinEnlace": "false", "conMascota": "false", "conBicicleta": "false", "plazaH": "false",
        "atendo": "false", "tipoFranjaI": "", "horaFranjaIda": "", "tipoFranjaV": "", "horaFranjaVuelta": "",
        "codPromo": "",
    }
    lines = ["callCount=1", "nextReverseAjaxIndex=0", "c0-scriptName=trainEnlacesManager",
             "c0-methodName=getTrainsList", "c0-id=0"]
    lines += [f"c0-e{n}=string:{quote(value, safe='')}" for n, value in enumerate(fields.values(), start=1)]
    refs = ", ".join(f"{name}:reference:c0-e{n}" for n, name in enumerate(fields, start=1))
    page_id = _random(10) + "-" + _random(9)  # what DWR's engine.js makes up for each page load
    lines += [f"c0-param0=Object_Object:{{{refs}}}", "batchId=1", "instanceId=0",
              f"page={quote(RESULTS_PATH, safe='')}", f"scriptSessionId={session}/{page_id}", "windowName="]
    # the page's script sends the filter's values in this header too, booleans as 1/0 (not a secret)
    akamai = ", ".join({"true": "1", "false": "0"}.get(v, v) for v in fields.values()).strip()
    text = _dwr(http, TRAINS_URL, "legacy shop train list", lines, {"akamai-key": akamai})
    start = text.find(_TRAINS_CALL)
    if start < 0:
        if 'r.handleException("1","0"' in text:
            raise UpstreamError("legacy shop train list: Renfe's shop failed to list trains")
        raise ApiShapeError("legacy shop train list: no train list in the answer")
    doc, end = read_literal(text, start + len(_TRAINS_CALL))
    if not re.match(r"\s*\);", text[end:]):
        raise ApiShapeError("legacy shop train list: unreadable answer")
    return doc


def _random(n: int) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


# --- the answer ------------------------------------------------------------------------------------


def _parse_trains(doc: Any, origin: Station, destination: Station, names: dict[str, str]) -> list[dict]:
    try:
        trains = _parse_trains_unchecked(doc, names)
    except ApiShapeError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, OverflowError):
        raise ApiShapeError("legacy shop train list: unexpected answer shape") from None
    for train in trains:  # a city's stations can't be checked: any of them may come back
        legs = train["legs"]
        if (not origin.group and legs[0]["origin"]["code"] != origin.code
                or not destination.group and legs[-1]["destination"]["code"] != destination.code):
            raise ApiShapeError("legacy shop train list: the answer is for other stations")
    return trains


def _parse_trains_unchecked(doc: Any, names: dict[str, str]) -> list[dict]:
    if isinstance(doc, dict) and "listadoTrenes" in doc and doc["listadoTrenes"] is None:
        # 19 Nov 2026 from 03:49, every search got {"actEtiqOferta": null, "activoCuposAbonosRec": "SI",
        # "listadoTrenes": null}, back by 04:04 for Madrid-Barcelona and by about 04:35 for Zaragoza-Madrid (not
        # seen on the 20th). An outage, not a changed answer; another try seconds later wouldn't help.
        raise UpstreamError("legacy shop train list: Renfe's shop isn't listing trains right now")
    (outbound,) = doc["listadoTrenes"]  # one list per direction; we ask for one way only
    if outbound.get("hayError") is True:
        raise UpstreamError("legacy shop train list: Renfe's shop reported an error for this search")
    rows = outbound.get("listviajeViewEnlaceBean") or []
    if not isinstance(rows, list):
        raise ApiShapeError("legacy shop train list: the trains are not a list")
    return [_train(row, names) for row in rows]


def _train(row: dict, names: dict[str, str]) -> dict:
    legs = [_leg(leg, names) for leg in row["trayectos"]]
    if not legs:
        raise ApiShapeError("legacy shop train list: a train has no legs")
    fares = sorted((_fare(f) for f in row.get("tarifasDisponibles") or []), key=lambda f: f["price"])
    kinds = [leg.pop("_type") for leg in legs]
    duration = row.get("duracionViajeTotalEnMinutos")
    return {
        "service": "+".join(leg["service"] for leg in legs),
        "type": " + ".join(kinds) if all(isinstance(k, str) and k for k in kinds) else None,
        "type_code": None,  # the legacy shop's product codes aren't the new system's (AV1…)
        "departure": legs[0]["departure"],
        "arrival": legs[-1]["arrival"],
        "duration_minutes": duration if isinstance(duration, int) and not isinstance(duration, bool) else None,
        "changes": len(legs) - 1,
        "legs": legs,
        "available": _on_sale(row, fares),
        "fares": fares,
    }


def _on_sale(row: dict, fares: list[dict]) -> bool:
    """As the results page decides it (listaTrenes.js, 2026-11-17) for one adult without a wheelchair place:
    razonNoDisponible "3" or `completo` is a full train, "4" "not on sale at the moment", "5"-"7" "doesn't
    run", any other value but "" or "8" "not on sale"; and only wheelchair places left (soloPlazaH with
    plazaHDisponible) is no seat for this search. "8" is what every train on sale carried."""
    return (row.get("razonNoDisponible") in _ON_SALE and row.get("completo") is not True
            and not (row.get("soloPlazaH") is True and row.get("plazaHDisponible") is True) and bool(fares))


def _leg(leg: dict, names: dict[str, str]) -> dict:
    origin, destination = _code(leg["cdgoEstacionOrigenTrayecto"]), _code(leg["cdgoEstacionDestinoTrayecto"])
    day = date.fromisoformat(leg["fecha"])
    departure, arrival = _at(day, leg["horaSalida"]), _at(day, leg["horaLlegada"])
    if arrival <= departure:  # arrives after midnight: the answer has no arrival date
        arrival = _at(day + timedelta(days=1), leg["horaLlegada"])
    service = leg["cdgoTren"]
    if not isinstance(service, str) or not re.fullmatch(r"[0-9]{1,6}", service):
        raise ApiShapeError("legacy shop train list: a leg has no train number")
    return {
        "service": service,
        "type_code": None,
        "origin": {"code": origin, "name": names.get(origin)},
        "destination": {"code": destination, "name": names.get(destination)},
        "departure": departure.isoformat(),
        "arrival": arrival.isoformat(),
        "intermediate_stops": None,  # not in the legacy shop's answer
        "_dep_utc": departure.astimezone(timezone.utc),
        "_type": leg.get("tipoTren"),
    }


def _code(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ApiShapeError("legacy shop train list: a leg has no station code")
    return value


def _at(day: date, hour: Any) -> datetime:
    """A Madrid wall-clock time on `day` ("06:27" or "06:27:00")."""
    match = _HOUR.fullmatch(hour) if isinstance(hour, str) else None
    if not match:
        raise ApiShapeError("legacy shop train list: unreadable time")
    return datetime(day.year, day.month, day.day, int(match.group(1)), int(match.group(2)),
                    tzinfo=MADRID)


def _fare(fare: dict) -> dict:
    price = _PRICE.fullmatch(fare["precioTarifa"]) if isinstance(fare.get("precioTarifa"), str) else None
    if not price:
        raise ApiShapeError("legacy shop train list: a fare has no usable price")
    name = fare.get("titulo")
    return {"name": html.unescape(name) if isinstance(name, str) and name else None,
            "price": round(float(f"{price.group(1)}.{price.group(2) or '0'}"), 2), "currency": "EUR"}


# --- a JavaScript object literal, read as data ----------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<string>"(?:[^"\\\x00-\x1f]|\\.)*")
  | (?P<key>[A-Za-z_$][A-Za-z0-9_$]*)(?=\s*:)
  | (?P<word>true|false|null)
  | (?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
  | (?P<open>[{\[]) | (?P<close>[}\]]) | (?P<mark>[:,]) | (?P<space>\s+)
""", re.VERBOSE)
_MAX_DEPTH = 40


def read_literal(text: str, start: int = 0) -> tuple[Any, int]:
    """The value written at text[start:] and the index just past it. Objects (bare or quoted keys), arrays,
    double-quoted strings, numbers, true, false and null: the bare keys are quoted and the rest is read as
    JSON. Anything else (a function call, a variable, a date) is an ApiShapeError. Nothing is evaluated."""
    parts, depth, i = [], 0, start
    while True:
        token = _TOKEN.match(text, i)
        if token is None:
            raise ApiShapeError("legacy shop train list: unreadable answer")
        i, kind, part = token.end(), token.lastgroup, token.group()
        if kind == "key":
            part = json.dumps(part)
        elif kind == "string":  # JavaScript's \' isn't JSON
            part = re.sub(r"\\(.)", lambda m: "'" if m.group(1) == "'" else m.group(), part)
        depth += {"open": 1, "close": -1}.get(kind, 0)
        if not 0 <= depth <= _MAX_DEPTH:
            raise ApiShapeError("legacy shop train list: unreadable answer")
        parts.append(part)
        if depth == 0 and kind in ("string", "word", "number", "close"):
            break
    try:
        return json.loads("".join(parts)), i
    except ValueError:
        raise ApiShapeError("legacy shop train list: unreadable answer") from None
