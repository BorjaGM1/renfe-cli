"""A fake Renfe API for httpx.MockTransport. Every value is synthetic; station codes and train numbers
are public timetable data."""

import base64
import copy
import json
import re

import httpx

from renfe import config

API_KEY = "k" * 40
TERMINAL = "X0000001"


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


TOKEN = jwt({"custom:terminal_id": TERMINAL, "token_use": "access"})

# The list the tests treat as shipped with the CLI (conftest swaps it in for the real one).
SHIPPED = [
    {"_u_i_c_station_code": "MADRI", "name": "Madrid (Todas)", "province": "Madrid",
     "agglomeration_type": "geographical", "main_station": False},
    {"_u_i_c_station_code": "60000", "name": "Madrid-Puerta de Atocha-Almudena Grandes", "province": "Madrid",
     "agglomeration_type": "commercial", "main_station": True},
    {"_u_i_c_station_code": "18000", "name": "Madrid-Atocha Cercanías", "province": "Madrid",
     "agglomeration_type": "commercial", "main_station": False},
    {"_u_i_c_station_code": "BARCE", "name": "Barcelona (Todas)", "province": "Barcelona",
     "agglomeration_type": "geographical", "main_station": False},
    {"_u_i_c_station_code": "71801", "name": "Barcelona-Sants", "province": "Barcelona",
     "agglomeration_type": "commercial", "main_station": True},
    {"_u_i_c_station_code": "04040", "name": "Zaragoza-Delicias", "province": "Zaragoza",
     "agglomeration_type": "commercial", "main_station": True},
]
# A station opened after the shipped list was made: only Renfe's live list has it.
NEW_STATION = {"_u_i_c_station_code": "99001", "name": "Apeadero Nuevo", "province": "Soria"}
STATIONS = SHIPPED + [NEW_STATION]  # what the fake Renfe answers for its station list


def path(service, dep, arr, origin="60000", destination="71801"):
    return {"origin_stop_code": origin, "destination_stop_code": destination, "service_name": service,
            "service_type_code": "AV1", "schedule_date": dep[:10], "departure_datetime": dep,
            "departure_datetime_timezone": "Europe/Madrid", "arrival_datetime": arr,
            "arrival_datetime_timezone": "Europe/Madrid", "intermediate_stop_count": 1}


TRAVEL_OPTIONS = {"total_count": 4, "travel_options": [
    {"travel_duration": 160, "travel_path": [path("03100", "2027-01-29T08:00:00Z", "2027-01-29T10:40:00Z")]},
    {"travel_duration": 165, "travel_path": [path("03000", "2027-01-29T05:30:00Z", "2027-01-29T08:15:00Z")]},
    {"travel_duration": 300, "travel_path": [
        path("00600", "2027-01-29T06:00:00Z", "2027-01-29T08:00:00Z", destination="04040"),
        path("00500", "2027-01-29T08:30:00Z", "2027-01-29T11:00:00Z", origin="04040")]},
    {"travel_duration": 170, "travel_path": [path("03200", "2027-01-29T12:00:00Z", "2027-01-29T14:50:00Z")]},
]}

FAMILIES = [{"id": "F1", "name": "Básico"}, {"id": "F2", "name": "Elige"}]


def priced(legs_body: list[dict]) -> dict:
    """A journey-search answer pricing every route it was asked about, with Madrid-local timestamps
    in Renfe's ±HHMM form (+0100 in winter, +0200 in summer)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def local(utc: str) -> str:
        moment = datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Madrid"))
        return moment.strftime("%Y-%m-%dT%H:%M:%S%z")

    routes = []
    for n, route in enumerate(legs_body):
        legs = [{"service_name": leg["service_name"], "service_type": {"code": "AV1", "name": "AVE"},
                 "departure_station": {"departure_timestamp": local(leg["departure"])}} for leg in route["legs"]]
        base = 30 + 10 * n
        routes.append({"legs": legs, "bundle_availability": {"availability": True},
                       "bundles": [{"product_family_id": "F2", "price": base + 5.5, "currency": "EUR"},
                                   {"product_family_id": "F1", "price": float(base), "currency": "EUR"}]})
    return {"data": {"offer": {"product_families": FAMILIES, "travels": [{"routes": routes}]}}, "messages": []}


# Renfe's legacy shop (venta.renfe.com/vol), which sells the dates before the cut-off. Made-up trains.


def js(value) -> str:
    """Python data as DWR writes it: a JavaScript object literal with bare keys, non-ASCII as \\u escapes."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{key}:{js(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(js(item) for item in value) + "]"
    return json.dumps(value)


def legacy_leg(service, dep, arr, origin="60000", destination="71801", kind="AVE"):
    return {"cdgoEstacionOrigenTrayecto": origin, "cdgoEstacionDestinoTrayecto": destination, "cdgoTren": service,
            "tipoTren": kind, "horaSalida": f"{dep}:00", "horaLlegada": f"{arr}:00", "razonNoDisponible": None,
            "lstIncidencias": [{"cdgoInc": "X", "description": "Texto libre del servidor"}]}


def legacy_train(legs, minutes, fares=(), reason="8", **extra):
    return {"trayectos": legs, "duracionViajeTotalEnMinutos": minutes, "razonNoDisponible": reason,
            "completo": False, "soloPlazaH": False, "msgTrenNoDispo": "Texto libre del servidor",
            "tarifasDisponibles": [{"titulo": name, "precioTarifa": price} for name, price in fares] or None,
            **extra}


LEGACY_TRAINS = [
    legacy_train([legacy_leg("03300", "17:00", "19:45")], 165, [("Elige", "41,5"), ("Básico", "36,75")]),
    legacy_train([legacy_leg("03100", "09:00", "11:40")], 160, [("Básico", "31,15"), ("Pr&eacute;mium", "79,8")]),
    legacy_train([legacy_leg("00600", "07:00", "09:00", destination="04040", kind="MD"),
                  legacy_leg("00500", "09:30", "12:00", origin="04040", kind="INTERCITY")], 300, [("Básico", "25")]),
    legacy_train([legacy_leg("03200", "13:00", "15:50")], 170, reason="4"),  # not on sale at the moment
    legacy_train([legacy_leg("03400", "22:30", "01:15")], 165, reason="3", completo=True),  # full
]


def legacy_answer(day: str, rows) -> str:
    """getTrainsList's answer, every leg dated `day` (YYYY-MM-DD) unless it says otherwise."""
    rows = copy.deepcopy(rows)
    for row in rows:
        for leg in row["trayectos"]:
            leg.setdefault("fecha", day)
    doc = {"actEtiqOferta": "SI", "listadoTrenes": [
        {"cdgoError": None, "hayError": False, "listviajeViewEnlaceBean": rows, "viajeIda": True}]}
    return f'r.handleCallback("1","0",{js(doc)});'


def dwr_response(call: str) -> httpx.Response:
    """A DWR reply carrying `call`, wrapped as the shop wraps it."""
    text = (f"throw 'allowScriptTagRemoting is false.';\n(function(){{\nvar r=window.dwr._[0];\n//#DWR-INSERT\n"
            f"//#DWR-REPLY\n{call}\n}})();\n")
    return httpx.Response(200, headers={"content-type": "text/javascript; charset=utf-8"}, text=text)


LEGACY_SESSION = "FAKEDWRSESSION0001"


# Renfe's live train map (config.LIVE_MAP_URL). Made-up positions, in the map's shape: Madrid times without a zone.


def map_train(number, delay, previous, next_, expected, reported="2027-01-29T09:29:40", origin="60000",
              destination="71801"):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    stamp = int(datetime.fromisoformat(reported).replace(tzinfo=ZoneInfo("Europe/Madrid")).timestamp())
    return {"codComercial": number, "codEstAnt": previous, "codEstSig": next_, "horaLlegadaSigEst": expected,
            "codProduct": 2, "codOrigen": origin, "codDestino": destination, "accesible": False,
            "ultRetraso": str(delay), "time": stamp, "p": "1"}


def live_map(*trains, updated="2027-01-29T09:30:00") -> dict:
    return {"fechaActualizacion": updated, "trenes": list(trains)}


# 09:30 on the day TRAVEL_OPTIONS runs: 03100 (09:00 from Madrid) 12 minutes late on its way to Zaragoza, and 00500
# (09:30 from Zaragoza, a change's second leg) 5 minutes late, standing in Zaragoza.
LIVE_MAP = live_map(map_train("03100", 12, "60000", "04040", "2027-01-29T10:32:00"),
                    map_train("00500", 5, "04040", "04040", "2027-01-29T09:33:00", origin="04040"))


class FakeRenfe:
    """Routes requests by URL; records them for assertions."""

    def __init__(self, legacy_trains=None, live=None, **overrides):
        self.requests: list[httpx.Request] = []
        self.overrides = overrides  # url-path -> callable(request) -> httpx.Response
        self.legacy_trains = LEGACY_TRAINS if legacy_trains is None else legacy_trains
        self.live = LIVE_MAP if live is None else live

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for key, fn in self.overrides.items():
            if key in url:
                return fn(request)
        return self.default(request)

    def default(self, request: httpx.Request) -> httpx.Response:
        """The answer without overrides (an override can fall back to it)."""
        url = str(request.url)
        if url == config.WEB_CONFIG_URL:
            return httpx.Response(200, json={"arch": {"security": {"apiKey": API_KEY}}})
        if url.endswith(config.PUBLIC_TOKEN_PATH):
            return httpx.Response(200, json={"accessToken": TOKEN, "expiresIn": 1800})
        if url.endswith(config.STATIONS_PATH):
            return httpx.Response(200, json=STATIONS)
        if url.endswith(config.SEARCH_TRAVEL_PATH):
            return httpx.Response(200, json=TRAVEL_OPTIONS)
        if url.endswith(config.JOURNEY_SEARCH_PATH):
            body = json.loads(request.content)
            return httpx.Response(200, json=priced(body["travels"][0]["routes"]))
        if request.url.host == "venta.renfe.com":
            return self.legacy(request)
        if url == config.LIVE_MAP_URL:
            return httpx.Response(200, json=self.live)
        return httpx.Response(404, json={"codeError": "NOT_FOUND"})

    def legacy(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/vol/buscarTren.do":
            return httpx.Response(302, headers={"location": "https://venta.renfe.com/vol/buscarTrenEnlaces.do",
                                                "set-cookie": "JSESSIONID=fake0001; Path=/vol"})
        if path == "/vol/buscarTrenEnlaces.do":
            return httpx.Response(200, headers={"content-type": "text/html;charset=ISO-8859-1"},
                                  text="<script src='/vol/dwr/interface/trainEnlacesManager.js?v=6'></script>")
        if path.endswith("/__System.generateId.dwr"):
            return dwr_response(f'r.handleCallback("0","0","{LEGACY_SESSION}");')
        if path.endswith("/trainEnlacesManager.getTrainsList.dwr"):
            day, month, year = re.search(r"c0-e3=string:(\d\d)%2F(\d\d)%2F(\d{4})", request.content.decode()).groups()
            return dwr_response(legacy_answer(f"{year}-{month}-{day}", self.legacy_trains))
        return httpx.Response(404, text="")

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def legacy_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == "venta.renfe.com"]

    def new_system_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == "api.rsv.renfe.com"]

    def live_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if str(request.url) == config.LIVE_MAP_URL]


def flaky(fake: FakeRenfe, *failures):
    """An override that answers with `failures` first (responses or exceptions to raise), then as `fake` would."""
    queue = list(failures)

    def answer(request: httpx.Request) -> httpx.Response:
        if not queue:
            return fake.default(request)
        got = queue.pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    return answer

