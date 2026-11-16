"""Renfe's station list (public master data), cached on disk for a day, and name/code lookup.

Codes are the `_u_i_c_station_code` values the search API takes: a UIC number for a single station
(60000 = Madrid-Puerta de Atocha) or a five-letter group code for all of a city's stations
(MADRI = "Madrid (Todas)").
"""

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import cachefile, config
from .client import RsvClient
from .errors import ApiShapeError, UsageError

_KEEP = ("_u_i_c_station_code", "name", "short_code", "province", "agglomeration_type", "main_station")


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    province: str | None = None
    group: bool = False  # all of a city's stations ("Madrid (Todas)")
    main: bool = False

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "province": self.province, "group": self.group, "main": self.main}


def parse(raw: object) -> list[Station]:
    if not isinstance(raw, list):
        raise ApiShapeError("stations: expected a list")
    if not raw:  # never cache (or trust) an empty list: every lookup would fail for a day
        raise ApiShapeError("stations: Renfe returned an empty station list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiShapeError("stations: expected objects in the list")
        code, name = item.get("_u_i_c_station_code"), item.get("name")
        if not isinstance(code, str) or not code or not isinstance(name, str) or not name:
            raise ApiShapeError("stations: an entry has no code or name")
        province = item.get("province")
        out.append(Station(
            code=code,
            name=name,
            province=province if isinstance(province, str) and province else None,
            group=item.get("agglomeration_type") == "geographical",
            main=item.get("main_station") is True,
        ))
    return out


def cache_path() -> Path:
    return config.cache_dir() / "stations.json"


def load(client: RsvClient, *, refresh: bool = False, path: Path | None = None) -> list[Station]:
    """The station list from the day-old cache, or from Renfe (then cached)."""
    path = path or cache_path()
    if not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(cached["fetched_at"])
            if 0 <= age < config.STATIONS_TTL:  # a timestamp in the future counts as stale
                return parse(cached["stations"])
        except (OSError, ValueError, KeyError, TypeError, ApiShapeError):  # ValueError covers UnicodeError
            pass
    raw = client.get(config.STATIONS_PATH, label="stations")
    stations = parse(raw)
    slim = [{k: item.get(k) for k in _KEEP} for item in raw]
    cachefile.write_atomic(path, json.dumps({"fetched_at": time.time(), "stations": slim}, ensure_ascii=False))
    return stations


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("(todas)", " ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _rank(query: str, station: Station) -> int | None:
    """0 code, 1 exact name, 2 name prefix, 3 a word starts with it, 4 contains it; None: no match."""
    if query.upper() == station.code.upper():
        return 0
    q, name = _norm(query), _norm(station.name)
    if not q:
        return None
    if name == q:
        return 1
    if name.startswith(q):
        return 2
    if re.search(rf"(?:^| ){re.escape(q)}", name):
        return 3
    if q in name:
        return 4
    return None


def find(query: str, stations: list[Station], limit: int = 20) -> list[Station]:
    """Matches, best first: by match quality, then whole-city groups, then main stations, then name."""
    scored = [(r, s) for s in stations if (r := _rank(query, s)) is not None]
    scored.sort(key=lambda rs: (rs[0], not rs[1].group, not rs[1].main, len(rs[1].name), rs[1].name))
    return [s for _, s in scored[:limit]]


def resolve(query: str, stations: list[Station]) -> Station:
    """The one station `query` means (a code or a name), or a UsageError listing the candidates."""
    scored = [(r, s) for s in stations if (r := _rank(query, s)) is not None]
    if not scored:
        raise UsageError(f"no station matches {query!r}; try `renfe stations {query}`")
    scored.sort(key=lambda rs: (rs[0], not rs[1].group, not rs[1].main, len(rs[1].name), rs[1].name))
    best_rank, best = scored[0]
    tied = [s for r, s in scored if r == best_rank and s.group == best.group and s.main == best.main]
    if best_rank >= 2 and len(tied) > 1:  # a code or an exact name is never ambiguous
        names = "; ".join(f"{s.name} ({s.code})" for s in tied[:5])
        raise UsageError(f"{query!r} matches several stations: {names}. Use a code or a longer name.")
    return best
