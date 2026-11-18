"""Match a live journey to dated timetable trips and verify the sections sold under other numbers."""

from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from functools import cache
from importlib.resources import files

from . import live

_ONE_DAY = timedelta(days=1)
_RUN_MARGIN = timedelta(hours=1)
_MAX_JOIN = timedelta(hours=1)
# These through services have a documented junction even when it is an intermediate stop on both trips.
_JUNCTIONS = {("4176", "13200", "17000"): ("4366", "11200"),
              ("1181", "04307", "60911"): ("34182", "71801")}


@dataclass(frozen=True)
class Group:
    origin: str
    destination: str
    numbers: frozenset[str]


@cache
def _groups() -> dict[str, list[Group]]:
    index = defaultdict(list)
    for line in files("renfe").joinpath("data/train_number_groups.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            origin, dest, numbers = line.split()
            group = Group(origin, dest, frozenset(live.key(n) for n in numbers.split("/")))
            for number in group.numbers:
                index[number].append(group)
    return dict(index)


def timetable_numbers(numbers: set[str]) -> set[str]:
    """Load only directly published peers. Two rows sharing a number do not make one larger group."""
    return numbers.union(peer for n in numbers for group in _groups().get(n, ()) for peer in group.numbers)


@dataclass(frozen=True)
class Stop:
    sequence: int
    code: str
    arrival: timedelta | None
    departure: timedelta | None


type Trips = dict[tuple[date, str], list[list[Stop]]]


@dataclass(frozen=True)
class Leg:
    number: str
    stops: tuple[Stop, ...]


type Journey = tuple[Leg, ...]
type Edge = tuple[str, timedelta | None, str, timedelta | None]


def service_days(reference: datetime) -> set[date]:
    return {reference.date() - _ONE_DAY, reference.date(), (reference + _RUN_MARGIN).date()}


def _legs(trips: Trips, day: date, number: str, origin: str, dest: str) -> Iterator[Leg]:
    for route in trips.get((day, number), ()):
        for i, start in enumerate(route):
            if start.code != origin or start.departure is None:
                continue
            for j in range(i + 1, len(route)):
                end = route[j]
                if end.code == dest and end.arrival is not None and start.departure < end.arrival:
                    yield Leg(number, tuple(route[i:j + 1]))


def _edges(stops: Sequence[Stop]) -> tuple[Edge, ...]:
    return tuple((a.code, a.departure, b.code, b.arrival) for a, b in zip(stops, stops[1:]))


def _shared_ranges(edges: tuple[Edge, ...], other: tuple[Edge, ...]) -> Iterator[tuple[int, int]]:
    # Compare timed movements. At section boundaries only the outgoing departure / incoming arrival matter.
    for i, _, length in SequenceMatcher(None, edges, other, autojunk=False).get_matching_blocks():
        start = i
        for k in range(i, i + length):
            src, dep, dest, arr = edges[k]
            if dep is None or arr is None or arr <= dep or src == dest:
                if start < k:
                    yield start, k
                start = k + 1
        if start < i + length:
            yield start, i + length


def _connections(group: frozenset[str], trips: Trips, day: date, number: str,
                 origin: str, dest: str) -> Iterator[Journey]:
    """Join dated trips only at published trip termini, within the same explicit number group."""
    starts = defaultdict(list)
    for peer in group:
        for route in trips.get((day, peer), ()):
            dep, arr = route[0].departure, route[-1].arrival
            if len(route) >= 2 and dep is not None and arr is not None and dep < arr:
                starts[route[0].code].append((peer, tuple(route)))

    def extend(station: str, arrival: timedelta | None, path: Journey, used: set[str]) -> Iterator[Journey]:
        for peer, stops in starts[station]:
            if peer in used:
                continue
            if arrival is not None and not timedelta(0) <= stops[0].departure - arrival <= _MAX_JOIN:
                continue
            onward = (*path, Leg(peer, stops))
            if stops[-1].code == dest:
                if path and (number == peer or number in used):
                    yield onward
            else:
                yield from extend(stops[-1].code, stops[-1].arrival, onward, used | {peer})

    yield from extend(origin, None, (), set())


def _linked(trips: Trips, day: date, number: str, origin: str, dest: str) -> Iterator[Journey]:
    if junction := _JUNCTIONS.get((number, origin, dest)):
        feeder, station = junction
        for first in _legs(trips, day, feeder, origin, station):
            for last in _legs(trips, day, number, station, dest):
                if timedelta(0) <= last.stops[0].departure - first.stops[-1].arrival <= _MAX_JOIN:
                    yield first, last
    for group in _groups().get(number, ()):
        if (origin, dest) != (group.origin, group.destination):
            continue
        yield from _connections(group.numbers, trips, day, number, origin, dest)
        # A peer can cover the entire mapped journey, with the live number used on a shared stretch.
        for peer in group.numbers - {number}:
            for leg in _legs(trips, day, peer, origin, dest):
                edges = _edges(leg.stops)
                if any(any(_shared_ranges(edges, _edges(route))) for route in trips.get((day, number), ())):
                    yield (leg,)


@dataclass(frozen=True)
class Section:
    number: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime

    def render(self, names: dict[str, str]) -> dict:
        return {"train": self.number.zfill(5),
                "origin": {"code": self.origin, "name": names.get(self.origin)},
                "destination": {"code": self.destination, "name": names.get(self.destination)},
                "scheduled_departure": self.departure.isoformat(), "scheduled_arrival": self.arrival.isoformat()}


def _sections(trips: Trips, day: date, number: str, paths: list[Journey]) -> set[Section]:
    midnight = datetime.combine(day, time.min, live.MADRID)
    # The primary legs already have confirmed endpoints, even if some intermediate times are blank.
    declared, alternatives = [], []
    for path in paths:
        legs, path_edges = set(), []
        for leg in path:
            first, last = leg.stops[0], leg.stops[-1]
            legs.add(Section(leg.number, first.code, last.code, midnight + first.departure, midnight + last.arrival))
            path_edges.extend(_edges(leg.stops))
        declared.append(legs)
        alternatives.append(tuple(path_edges))
    sections = set.intersection(*declared)
    for peer in timetable_numbers({number}):
        for route in trips.get((day, peer), ()):
            edges = _edges(route)
            spans = {(0, len(edges))}
            # Retain the stretches supported by every possible stop pattern for these exact endpoint times.
            for alternative in alternatives:
                shared = list(_shared_ranges(edges, alternative))
                spans = {(max(a, c), min(b, d)) for a, b in spans for c, d in shared if max(a, c) < min(b, d)}
                if not spans:
                    break
            for start, end in spans:
                src, dep, _, _ = edges[start]
                _, _, dst, arr = edges[end - 1]
                sections.add(Section(peer, src, dst, midnight + dep, midnight + arr))
    return sections


def match(trips: Trips, entry: dict, reference: datetime, names: dict[str, str]) -> dict:
    number, origin, dest = live.key(entry["number"]), entry["origin"], entry["destination"]
    # Prefer the number Renfe put on the map. Fall back to its published peers only if needed.
    for direct in (True, False):
        matches = defaultdict(set)
        for day in service_days(reference):
            midnight = datetime.combine(day, time.min, live.MADRID)
            paths = (((leg,) for leg in _legs(trips, day, number, origin, dest)) if direct
                     else _linked(trips, day, number, origin, dest))
            for path in paths:
                dep, arr = midnight + path[0].stops[0].departure, midnight + path[-1].stops[-1].arrival
                if dep - _RUN_MARGIN <= reference <= arr + _RUN_MARGIN:
                    matches[dep, arr].add((day, path))
        if not matches:
            continue
        if len(matches) > 1:
            return {"schedule_match": "ambiguous"}
        (dep, arr), candidates = next(iter(matches.items()))
        # Several GTFS trip variants can agree on endpoint times. Report only sections they all support.
        by_day = defaultdict(list)
        for day, path in candidates:
            by_day[day].append(path)
        sections = set.intersection(*(_sections(trips, day, number, paths) for day, paths in by_day.items()))
        # A short GTFS variant often repeats a portion of a longer one with the same number.
        maximal = [s for s in sections if not any(
            other.number == s.number and other.departure <= s.departure and s.arrival <= other.arrival
            and (other.departure, other.arrival) != (s.departure, s.arrival) for other in sections)]
        maximal.sort(key=lambda s: (s.departure, -(s.arrival - s.departure), s.number != number,
                                    s.number, s.origin, s.destination))
        if not direct and not any(s.number == number for s in maximal):
            return {"schedule_match": "ambiguous"}
        result = {"schedule_match": "exact" if direct else "linked", "scheduled_departure": dep.isoformat(),
                  "scheduled_arrival": arr.isoformat(), "number_segments": [s.render(names) for s in maximal]}
        if not direct:
            result["schedule_trains"] = list(dict.fromkeys(s.number.zfill(5) for s in maximal))
        return result
    return {"schedule_match": "unmatched"}
