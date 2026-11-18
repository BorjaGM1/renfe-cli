"""One-off failures, tried again. A 24-hour soak test (18 Nov 2026) saw Renfe fail single requests: the legacy shop's
results page answered 500 once, and the live map dropped connections for under a minute. Both were gone 60 s later.

Retried: an HTTP 500, 502, 503 or 504 (on the legacy shop, only 500 and 502, and never one with Retry-After), and
a connection that drops or is refused before the answer is in (httpx's ConnectError, ReadError, WriteError,
CloseError, RemoteProtocolError). Up to 2 retries, 3 tries in all, 1 s and then 2 s apart; the client's one resend
for a stale saved session is one of those 3 tries. Never retried: a block or rate limit (403, 429), a bot,
challenge or waiting-room page (HTML where JSON was expected, whatever the status), any other 4xx, an answer that
doesn't read, and a timeout, which has already cost the whole timeout.

Each request that had to be retried is logged, for the `retries` list of the command's output.
"""

import time
from collections.abc import Callable
from typing import TypeVar

import httpx

from .errors import RenfeError

PAUSES = (1.0, 2.0)  # seconds before the second and the third try
_STATUSES = (500, 502, 503, 504)
LEGACY_STATUSES = (500, 502)  # the legacy shop's 503 or 504 may be an edge deny or a waiting room
_sleep, _clock = time.sleep, time.monotonic  # tests replace _sleep

T = TypeVar("T")


def status_failure(status: int, statuses: tuple[int, ...] = _STATUSES) -> str | None:
    """What failed, for a status worth another try; None for any other."""
    return f"server error ({status})" if status in statuses else None


def connection_failure(exc: httpx.RequestError) -> str | None:
    """What failed, for a connection refused or dropped before the answer was in; None for a timeout or the rest."""
    if isinstance(exc, httpx.ConnectError):
        return f"no connection ({type(exc).__name__})"
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):  # read, write, close; a reset
        return f"connection dropped ({type(exc).__name__})"
    return None


def call(request: str, send: Callable[[], T], log: list[dict] | None, *, may_retry: Callable[[], bool] | None = None,
         deadline: float | None = None, resend: Callable[[RenfeError], str | None] | None = None,
         describe: Callable[[RenfeError], str | None] | None = None) -> T:
    """`send()`, up to 3 tries in all. A RenfeError that carries `transient` is tried again after a pause (1 s
    before the first such retry, 2 s before the second); one that doesn't may still be sent again at once when
    `resend(err)` says what failed (the client's stale-session resend, one of the 3 tries). `resend` is asked on the
    last try too, for what it cleans up, though nothing is sent then. No retry when `may_retry` says no (asked again
    right before each re-send), or when the pause and another try as long as the last one wouldn't end by
    `deadline` (a time.monotonic() value). The last error is raised as it was.

    A request sent again gets one entry in `log`, added before the first re-send and filled in as it goes, so a
    caller that stops waiting can see the tries so far: what failed on each (`describe(err)` can name a failure
    that wasn't the request's own), and whether a later one worked."""
    entry: dict | None = None
    paused = 0  # retries after a pause so far: the next pause is PAUSES[paused]
    for n in range(len(PAUSES) + 1):
        started = _clock()
        try:
            result = send()
        except RenfeError as err:
            took, last = _clock() - started, n == len(PAUSES)
            what, pause = err.transient, None if last else PAUSES[paused]
            if what is None and resend is not None:
                answer = resend(err)  # on the last try too: a refused saved session is dropped even then
                what, pause = (None, None) if last else (answer, 0.0)
            again = (what is not None and pause is not None
                     and (deadline is None or _clock() + pause + took <= deadline)
                     and (may_retry is None or may_retry()))
            if again and pause:
                _sleep(pause)
                paused += 1
                again = may_retry is None or may_retry()
            text = (describe(err) if describe is not None else None) or what or err.message
            if not again:
                if entry is not None:
                    entry["tries"] = n + 1
                    entry["errors"].append(text)
                raise
            if entry is None:  # a re-send follows: from now on the request shows in the log
                entry = {"request": request, "tries": 0, "errors": [], "recovered": False}
                if log is not None:  # list.append is atomic: fare batches log from their threads
                    log.append(entry)
            entry["tries"] = n + 1
            entry["errors"].append(text)
        else:
            if entry is not None:
                entry["tries"], entry["recovered"] = n + 1, True
            return result
    raise AssertionError("unreachable: the last try raises or returns")  # pragma: no cover
