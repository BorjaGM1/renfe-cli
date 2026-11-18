"""Network delay snapshot and scheduled endpoint times from Renfe's public GTFS timetable."""

import csv
import io
import re
import time
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from . import cachefile, config, live, retry
from .errors import ApiShapeError, RenfeError, UpstreamError

TIMETABLE_URL = "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"
_ONE_DAY = timedelta(days=1)
# An entry can appear before its train leaves, or linger after its scheduled arrival.
_RUN_MARGIN = timedelta(hours=1)
_MAX_JOIN = timedelta(hours=1)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# Renfe publishes these services under two numbers (see the service-number list linked in the README).
# (live number, origin, destination) -> (first leg's number, junction); the onward leg uses the live number.
_LINKS = {
    ("4176", "13200", "17000"): ("04366", "11200"),
    ("1181", "04307", "60911"): ("34182", "71801"),
}


@dataclass(frozen=True)
class Stop:
    sequence: int
    code: str
    arrival: timedelta | None
    departure: timedelta | None


type Trips = dict[tuple[date, str], list[list[Stop]]]
type Times = tuple[timedelta, timedelta]


def _rows(archive: zipfile.ZipFile, table: str, required: str) -> Iterator[dict[str, str]]:
    with archive.open(table + ".txt") as member, io.TextIOWrapper(member, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        # Renfe pads the last column of its CSV records, including the header.
        reader.fieldnames = [name.strip() for name in reader.fieldnames or ()]
        if not set(required.split()).issubset(reader.fieldnames):
            raise ValueError("missing GTFS column")
        yield from reader


def _services(archive: zipfile.ZipFile, days: set[date]) -> dict[str, set[date]]:
    """Apply the weekly calendar, then dated additions and removals, for each possible service day."""
    active = defaultdict(set)
    dates = {day.strftime("%Y%m%d"): day for day in days}
    has_calendar = "calendar.txt" in archive.namelist()
    if has_calendar:
        for row in _rows(archive, "calendar", "service_id start_date end_date " + " ".join(_WEEKDAYS)):
            start, end = row["start_date"].strip(), row["end_date"].strip()
            for stamp, day in dates.items():
                if start <= stamp <= end and row[_WEEKDAYS[day.weekday()]].strip() == "1":
                    active[row["service_id"].strip()].add(day)
    if not has_calendar or "calendar_dates.txt" in archive.namelist():
        for row in _rows(archive, "calendar_dates", "service_id date exception_type"):
            if day := dates.get(row["date"].strip()):
                if row["exception_type"].strip() == "1":
                    active[row["service_id"].strip()].add(day)
                elif row["exception_type"].strip() == "2":
                    active[row["service_id"].strip()].discard(day)
    return active


def _offset(text: str) -> timedelta | None:
    # GTFS uses e.g. 24:15:00 for an arrival the day after the trip starts.
    text = text.strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]{1,2}:[0-5][0-9]:[0-5][0-9]", text):
        raise ValueError("invalid GTFS time")
    h, m, s = map(int, text.split(":"))
    return timedelta(hours=h, minutes=m, seconds=s)


def _parse(data: bytes, days: set[date], numbers: set[str]) -> Trips:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(member.file_size for member in archive.infolist()) > 256 * 1024**2:
                raise ValueError("oversized timetable")
            active = _services(archive, days)
            selected = {}
            for row in _rows(archive, "trips", "trip_id trip_short_name service_id"):
                number = live.key(row["trip_short_name"].strip())
                service = row["service_id"].strip()
                if number in numbers and active.get(service):
                    selected[row["trip_id"].strip()] = (number, active[service])
            stops = defaultdict(list)
            for row in _rows(archive, "stop_times", "trip_id stop_sequence stop_id departure_time arrival_time"):
                tid = row["trip_id"].strip()
                if tid in selected:
                    stops[tid].append(Stop(int(row["stop_sequence"]), row["stop_id"].strip(),
                                           _offset(row["arrival_time"]), _offset(row["departure_time"])))
            trips = defaultdict(list)
            for tid, route in stops.items():
                route.sort(key=lambda stop: stop.sequence)
                number, service_days = selected[tid]
                for day in service_days:
                    trips[day, number].append(route)
            return trips
    except (zipfile.BadZipFile, zlib.error, csv.Error, LookupError, ValueError, TypeError, AttributeError,
            EOFError, RuntimeError, OSError):
        raise ApiShapeError("Renfe timetable: unreadable GTFS archive or table") from None


def _download(transport: httpx.BaseTransport | None) -> bytes:
    try:
        with httpx.Client(transport=transport, timeout=config.REQUEST_TIMEOUT) as http:
            with http.stream("GET", TIMETABLE_URL) as response:
                if response.status_code != 200:
                    status = response.status_code
                    html = "html" in response.headers.get("content-type", "").lower()
                    raise UpstreamError(f"Renfe timetable: HTTP {status}", status=status,
                                        transient=None if html else retry.status_failure(status))
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 32 * 1024**2:
                        raise ApiShapeError("Renfe timetable: download too large")
                return bytes(data)
    except httpx.RequestError as exc:
        raise UpstreamError(f"Renfe timetable: request failed ({type(exc).__name__})",
                            transient=retry.connection_failure(exc)) from None


def _timetable(days: set[date], numbers: set[str], transport: httpx.BaseTransport | None, retries: list) -> Trips:
    path = config.cache_dir() / "timetable.zip"
    try:
        if 0 <= time.time() - path.stat().st_mtime < _ONE_DAY.total_seconds():
            return _parse(path.read_bytes(), days, numbers)
    except (OSError, ApiShapeError):
        pass  # Refresh a missing or damaged cache.
    content = retry.call("Renfe timetable", lambda: _download(transport), retries)
    trips = _parse(content, days, numbers)
    cachefile.write_atomic(path, content)
    return trips


def _legs(trips: Trips, day: date, number: str, origin: str, destination: str) -> set[Times]:
    """All distinct timings between the two stops in the requested direction on this service."""
    times = set()
    for route in trips.get((day, number), ()):
        departures = []
        for stop in route:
            if stop.code == destination and stop.arrival is not None:
                times.update((start, stop.arrival) for start in departures if start < stop.arrival)
            if stop.code == origin and stop.departure is not None:
                departures.append(stop.departure)
    return times


def _service_days(reference: datetime) -> set[date]:
    # A trip can run past midnight or already appear on the map before a midnight departure.
    return {reference.date() - _ONE_DAY, reference.date(), (reference + _RUN_MARGIN).date()}


def _plausible(times: set[Times], day: date, reference: datetime) -> set[tuple[datetime, datetime]]:
    midnight = datetime.combine(day, datetime.min.time(), live.MADRID)
    offset = reference - midnight
    return {(midnight + start, midnight + end) for start, end in times
            if start - _RUN_MARGIN <= offset <= end + _RUN_MARGIN}


def _schedule(trips: Trips, entry: dict, reference: datetime) -> dict:
    number, origin, dest = live.key(entry["number"]), entry["origin"], entry["destination"]
    link = _LINKS.get((number, origin, dest))
    direct, linked = set(), set()
    for day in _service_days(reference):
        direct.update(_plausible(_legs(trips, day, number, origin, dest), day, reference))
        if link:
            feeder, junction = link
            first = _legs(trips, day, live.key(feeder), origin, junction)
            last = _legs(trips, day, number, junction, dest)
            through = {(dep, arr) for dep, at in first for onward, arr in last
                       if timedelta(0) <= onward - at <= _MAX_JOIN}
            linked.update(_plausible(through, day, reference))
    matches = direct or linked
    if len(matches) != 1:
        return {"schedule_match": "ambiguous" if matches else "unmatched"}
    (dep, arr), = matches
    result = {"schedule_match": "exact" if direct else "linked",
              "scheduled_departure": dep.isoformat(), "scheduled_arrival": arr.isoformat()}
    if not direct and link:
        result["schedule_trains"] = [link[0], entry["number"]]
    return result


def report(board: live.Board, names: dict[str, str], *, transport: httpx.BaseTransport | None = None,
           retries: list[dict] | None = None) -> dict:
    entries = sorted((e for e in board.trains.values() if (e["delay"] or 0) > 0),
                     key=lambda e: (-e["delay"], int(e["number"])))
    rows, references = [], {}
    retries = [] if retries is None else retries
    for entry in entries:
        number = entry["number"]
        row = live.status(board, number, names)
        del row["retries"]  # All trains share the requests logged at the top level.
        row.update(scheduled_departure=None, scheduled_arrival=None, schedule_match="unmatched")
        rows.append(row)
        # The map has no service date. Undo the delay at the next stop to place the train in its
        # timetable window, including yesterday's late or overnight service.
        moment = entry["expected"] or board.updated or entry["reported_at"]
        if moment is not None:
            references[number] = moment.astimezone(live.MADRID) - timedelta(minutes=entry["delay"])
    notice = None
    if references:
        days = {day for moment in references.values() for day in _service_days(moment)}
        numbers = {live.key(n) for n in references}
        numbers.update(live.key(feeder) for (n, _, _), (feeder, _) in _LINKS.items() if n in numbers)
        try:
            trips = _timetable(days, numbers, transport, retries)
        except RenfeError as err:
            notice = err.message
            for row in rows:
                row["schedule_match"] = "unavailable"
        else:
            for row, entry in zip(rows, entries):
                if reference := references.get(row["train"]):
                    row.update(_schedule(trips, entry, reference))
    delays = [e["delay"] for e in board.trains.values()]
    return {"map_updated": board.updated.isoformat() if board.updated else None,
            "mapped_trains": len(delays), "delayed": len(rows), "on_time": delays.count(0),
            "early": sum(d is not None and d < 0 for d in delays), "unknown": delays.count(None),
            "trains": rows, "notice": notice, "retries": retries}
