"""A fake Renfe API for httpx.MockTransport. Every value is synthetic; station codes and train numbers
are public timetable data."""

import base64
import json

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


class FakeRenfe:
    """Routes requests by URL; records them for assertions."""

    def __init__(self, **overrides):
        self.requests: list[httpx.Request] = []
        self.overrides = overrides  # url-path -> callable(request) -> httpx.Response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for key, fn in self.overrides.items():
            if key in url:
                return fn(request)
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
        return httpx.Response(404, json={"codeError": "NOT_FOUND"})

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]
