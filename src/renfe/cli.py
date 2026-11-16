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

from . import package_version, search as search_mod, stations as stations_mod
from .client import RsvClient
from .errors import RenfeError, UsageError
from .output import render, render_error

app = typer.Typer(
    name="renfe",
    help="Search Renfe trains and fares over Renfe's own web API. No account needed. Output is JSON.",
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


def _parse_date(text: str) -> date:
    today = datetime.now(search_mod.MADRID).date()
    word = text.strip().lower()
    if word in ("today", "hoy"):
        return today
    if word in ("tomorrow", "mañana", "manana"):
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise UsageError(f"--date must be YYYY-MM-DD, today or tomorrow (got {text!r})") from None


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
    refresh: Annotated[bool, typer.Option(help="Refetch the station list instead of using the day-old cache.")] = False,
) -> None:
    """List stations matching QUERY (all of them without one), best match first."""

    def produce() -> dict:
        with make_client() as client:
            every = stations_mod.load(client, refresh=refresh)
        found = stations_mod.find(query, every, limit) if query else every[:limit]
        return {"query": query, "count": len(found), "stations": [s.to_dict() for s in found]}

    _emit(produce)


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
    """Trains from ORIGIN to DESTINATION on a date, with fares for one adult when Renfe's new system sells it."""

    def produce() -> dict:
        day, after_time = _parse_date(date_), _parse_time(after)
        now = datetime.now(search_mod.MADRID)
        hide_departed = after_time is None and day == now.date()
        if hide_departed:  # don't list trains that have already left
            after_time = now.time().replace(second=0, microsecond=0)
        with make_client() as client:
            every = stations_mod.load(client)
            src, dst = stations_mod.resolve(origin, every), stations_mod.resolve(destination, every)
            if src.code == dst.code:
                raise UsageError("origin and destination are the same station")
            names = {s.code: s.name for s in every}
            out = search_mod.search(client, src, dst, day, names=names, after=after_time, fares=fares, limit=limit)
        if hide_departed and out["count"] == 0:
            gone = f"No more trains today after {after_time.strftime('%H:%M')}; try --date tomorrow."
            out["notice"] = f"{out['notice']} {gone}" if out["notice"] else gone
        return out

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
