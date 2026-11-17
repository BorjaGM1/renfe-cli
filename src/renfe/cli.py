"""`renfe` CLI. Prints JSON to stdout; errors go to stderr as {"error": {...}} with the exit codes
1 (internal), 2 (usage), 3 (auth), 5 (blocked/upstream) and 7 (API changed). No account or login needed.
"""

import json
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, NoReturn

import typer

try:  # Typer >= 0.29 vendors Click; older Typer uses the click package
    from typer._click import exceptions as click_exceptions
except ImportError:  # pragma: no cover
    from click import exceptions as click_exceptions

from . import package_version, search as search_mod, shop as shop_mod, stations as stations_mod
from .client import RsvClient
from .errors import RenfeError, UsageError
from .output import render, render_error

app = typer.Typer(
    name="renfe",
    help="Search Renfe trains and fares over Renfe's own web API, then open Renfe's website to buy. "
         "No account needed. Output is JSON.",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks print frame locals, which include the bearer and the api key.
    pretty_exceptions_enable=False,
)


def make_client() -> RsvClient:
    """The client every command uses (tests replace this)."""
    return RsvClient()


def _fail(err: RenfeError) -> NoReturn:
    typer.echo(render_error(err), err=True)
    raise typer.Exit(err.exit_code)


def _emit(produce: Callable[[], Any]) -> None:
    """Print `produce()` as JSON. Any failure becomes the stderr error payload: a RenfeError as is,
    anything else by its type only (messages can embed URLs, headers or token values).

    A payload carrying `fares_error` (Renfe blocked or failed a fare request mid-search) is a partial
    result: it is printed on stdout, the error goes to stderr, and the exit code is the error's (5),
    so scripts checking `$?` stop too."""
    try:
        payload = produce()
        text = render(payload)
    except (typer.Exit, typer.Abort):
        raise
    except RenfeError as err:
        _fail(err)
    except Exception as exc:
        _fail(RenfeError(f"unexpected internal error: {type(exc).__name__}"))
    typer.echo(text)
    partial = payload.get("fares_error") if isinstance(payload, dict) else None
    if isinstance(partial, dict):
        typer.echo(json.dumps({"error": partial}, ensure_ascii=False, allow_nan=False), err=True)
        raise typer.Exit(int(partial.get("exit_code") or 5))


# A safety bound, not Renfe's rule: www.renfe.com's search form loads at most 12 months of prices
# (DEFAULT_MAX_MONTHS_TO_CHARGE in its script, 2026-11-16). Beyond that a date is a typo.
_MAX_DAYS_AHEAD = 366


def _parse_date(text: str) -> date:
    today = datetime.now(search_mod.MADRID).date()
    word = text.strip().lower()
    if word in ("today", "hoy"):
        return today
    if word in ("tomorrow", "mañana", "manana"):
        return today + timedelta(days=1)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", word):
            raise ValueError
        day = date.fromisoformat(word)
    except ValueError:
        raise UsageError(f"--date must be YYYY-MM-DD, today or tomorrow (got {text!r})") from None
    if day < today:
        raise UsageError(f"--date {day.isoformat()} is in the past")
    if day > today + timedelta(days=_MAX_DAYS_AHEAD):
        raise UsageError(f"--date {day.isoformat()} is more than a year ahead; Renfe doesn't sell that far")
    return day


def _parse_time(text: str | None) -> time | None:
    if text is None:
        return None
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text.strip()):
        raise UsageError(f"--after must be HH:MM (got {text!r})")
    hours, minutes = text.strip().split(":")
    return time(int(hours), int(minutes))


def _print_version(value: bool) -> None:
    if value:
        typer.echo(package_version() or "unknown")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_print_version, is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    pass


@app.command("stations")
def stations_cmd(
    query: Annotated[str | None, typer.Argument(help="Part of a station name, or a code.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=2000, help="Most results to show.")] = 20,
    refresh: Annotated[bool, typer.Option(
        help="Ask Renfe for its current list instead of using the one shipped with the CLI.")] = False,
) -> None:
    """List stations matching QUERY (all of them without one), best match first."""

    def pick(every: list[stations_mod.Station]) -> list[stations_mod.Station]:
        return stations_mod.find(query, every, limit) if query else every[:limit]

    def produce() -> dict:
        found = [] if refresh else pick(stations_mod.bundled())
        if not found:  # asked to, or nothing in the shipped list matches: Renfe's live list
            with make_client() as client:
                found = pick(stations_mod.load(client, refresh=refresh))
        return {"query": query, "count": len(found), "stations": [s.to_dict() for s in found]}

    _emit(produce)


def _stations(client: RsvClient, origin: str, destination: str) -> tuple[stations_mod.Station, stations_mod.Station]:
    src, dst = stations_mod.lookup(origin, client), stations_mod.lookup(destination, client)
    if src.code == dst.code:
        raise UsageError("origin and destination are the same station")
    return src, dst


def _names(*picked: stations_mod.Station) -> dict[str, str]:
    """Station names by code, for the legs of a search; a code the shipped list lacks keeps no name."""
    return {s.code: s.name for s in stations_mod.bundled()} | {s.code: s.name for s in picked}


@app.command("search")
def search_cmd(
    origin: Annotated[str, typer.Argument(help="From: a station name (Madrid, Atocha) or code (MADRI, 60000).")],
    destination: Annotated[str, typer.Argument(help="To: a station name or code.")],
    date_: Annotated[str, typer.Option("--date", "-d", help="Travel date: YYYY-MM-DD, today or tomorrow.")] = "today",
    after: Annotated[str | None, typer.Option(
        help="Only trains leaving at or after HH:MM, local time (today: defaults to now).")] = None,
    fares: Annotated[bool, typer.Option(help="Also fetch fares (one extra request per 3 trains).")] = True,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Most trains to return, earliest first.")] = 10,
) -> None:
    """Trains from ORIGIN to DESTINATION on a date, with fares for one adult (before 25 Jan 2027, from Renfe's
    legacy shop)."""

    def produce() -> dict:
        day, after_time = _parse_date(date_), _parse_time(after)
        now = datetime.now(search_mod.MADRID)
        hide_departed = after_time is None and day == now.date()
        if hide_departed:  # don't list trains that have already left
            after_time = now.time().replace(second=0, microsecond=0)
        with make_client() as client:
            src, dst = _stations(client, origin, destination)
            out = search_mod.search(client, src, dst, day, names=_names(src, dst), after=after_time, fares=fares,
                                    limit=limit)
        if hide_departed and out["count"] == 0:
            gone = f"No more trains today after {after_time.strftime('%H:%M')}; try --date tomorrow."
            out["notice"] = f"{out['notice']} {gone}" if out["notice"] else gone
        return out

    _emit(produce)


def _stdout_is_terminal() -> bool:
    return sys.stdout.isatty()


def _service_parts(text: str) -> list[str] | None:
    """`03081` -> ["3081"]; `00601 + 00533` -> ["601", "533"]; None when it isn't a train number."""
    compact = re.sub(r"\s*\+\s*", "+", text.strip())  # spaces allowed around "+", not inside a number
    if not re.fullmatch(r"[0-9]{1,6}(\+[0-9]{1,6})*", compact):
        return None
    return [part.lstrip("0") or "0" for part in compact.split("+")]


@app.command("open")
def open_cmd(
    origin: Annotated[str, typer.Argument(help="From: a station name (Madrid, Atocha) or code (MADRI, 60000).")],
    destination: Annotated[str, typer.Argument(help="To: a station name or code.")],
    date_: Annotated[str, typer.Option("--date", "-d", help="Travel date: YYYY-MM-DD, today or tomorrow.")] = "today",
    train: Annotated[str | None, typer.Option(
        "--train", "-t", help="A service number from `renfe search` (e.g. 03081): checked against that day's "
                              "trains and shown, so you know which one to pick on the page.")] = None,
    browser: Annotated[bool | None, typer.Option(
        "--browser/--no-browser",
        help="Open the link in your default browser. Default: only when run from a terminal, so scripts "
             "and agents get the link without a browser popping up.")] = None,
) -> None:
    """Open Renfe's website on this search, to pick the train and pay there. The CLI never books or pays."""

    def produce() -> dict:
        day = _parse_date(date_)
        wanted = _service_parts(train) if train is not None else None
        if train is not None and wanted is None:
            raise UsageError(f"--train must be a train number such as 03081 or 00601+00533 (got {train!r})")
        with make_client() as client:
            src, dst = _stations(client, origin, destination)
            chosen, notices = None, []
            if wanted is not None:
                found = search_mod.search(client, src, dst, day, names=_names(src, dst), fares=False)
                chosen = next((t for t in found["trains"] if _service_parts(t["service"]) == wanted), None)
                # Without fares, search() has a notice only when its list may be missing trains: the legacy shop
                # failed before the cut-off, or Renfe's new system reported more trains than it returned.
                if found["notice"]:
                    notices.append(found["notice"])
                if chosen is None and not found["notice"]:
                    raise UsageError(f"no train {train} from {src.name} to {dst.name} on {day.isoformat()}; "
                                     "`renfe search` lists them")
                if chosen is None:
                    notices.append(f"Train {train} isn't in the list this CLI can see for {day.isoformat()}, "
                                   "which is incomplete; look for it on Renfe's page.")
                elif datetime.fromisoformat(chosen["departure"]) < datetime.now(search_mod.MADRID):
                    notices.append(f"Train {chosen['service']} has already left "
                                   f"({chosen['departure'][11:16]}).")
        page = shop_mod.link(src, dst, day)
        launch = _stdout_is_terminal() if browser is None else browser  # scripts and agents: link only
        opened = shop_mod.open_in_browser(page["url"]) if launch else False
        return {"date": day.isoformat(), "origin": src.to_dict(), "destination": dst.to_dict(),
                "train": chosen, "notice": " ".join(notices) or None, "shop": page, "opened": opened}

    _emit(produce)


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point. Runs Typer without its standalone error printing, so that usage errors (a missing
    argument, an out-of-range option) come out as the same JSON error payload as everything else."""
    try:
        rv = app(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click_exceptions.NoArgsIsHelpError as exc:
        exc.show()
        sys.exit(0)
    except click_exceptions.ClickException as exc:
        typer.echo(render_error(UsageError(exc.format_message())), err=True)
        sys.exit(2)
    except typer.Abort:
        sys.exit(1)
    sys.exit(rv if isinstance(rv, int) else 0)
