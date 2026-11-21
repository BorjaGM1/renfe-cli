"""RsvClient: a plain httpx client for Renfe's new sales system API (api.rsv.renfe.com).

Anonymous access, as the web app does before anyone signs in:
  1. GET the public web config and read the static x-api-key from it;
  2. GET /tokens/public with that key -> a 30-minute anonymous Cognito access token;
  3. send API calls with the bearer, the key, `channel: VLP`, the token's terminal id and a session id.

The key and token are kept together in the cache dir (`session.json`, mode 0600) until the token is
about to expire, so a timetable search usually sends one request instead of three (fares add one per 3
trains). Minting always re-reads the web config, so a rotated key heals within one token lifetime.

Every request (web config, token, search-travel, journey-search) gets up to 3 tries (retry.py), logged as one
entry in `retries`. A one-off failure (a 500, 502, 503 or 504 that isn't an HTML page, or a connection that drops
or is refused) is tried again 1 s and then 2 s later. And when Renfe answers 401 to a session read from disk, that
session is replaced (by a newer one another run saved, or a fresh one) and the request is sent once more at once:
one of the same 3 tries. A 403 is a block even then: it is reported, and the file is dropped so the next run starts
afresh. A failed mint (its own 3 tries) is not repeated for 30 s, nor by the call that needed it. Nothing else is
retried: on a block, rate limit, timeout or lasting outage we stop and report. Errors never echo the key, the
token or a body.

Pacing is off by default (Renfe answers in 0.1-0.5 s and the web app fires its calls back to back).
`spacing` > 0 turns on the old mode: one request at a time across every `renfe` process on the machine,
at least `spacing` seconds apart (a lock file in the cache dir).
"""

import base64
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from . import cachefile, config, retry
from .errors import ApiShapeError, AuthError, BlockedError, RenfeError, UpstreamError, UsageError

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: pacing falls back to this process only
    fcntl = None  # type: ignore[assignment]

_RENFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,59}")  # Renfe's error codes, e.g. F_NOT_READABLE
_TERMINAL_ID = re.compile(r"[A-Za-z0-9]{1,32}")
_API_KEY = re.compile(r"[A-Za-z0-9]{20,80}")
_RETRY_AFTER = re.compile(r"[0-9]{1,5}")
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TOKEN_MARGIN = 60  # renew the anonymous token this many seconds before it expires
_TOKEN_MAX_LIFETIME = 1800  # what Renfe grants; a longer claim (or a cache file saying so) isn't trusted
_JWT = re.compile(r"[A-Za-z0-9_-]{1,4096}\.[A-Za-z0-9_-]{1,8192}\.[A-Za-z0-9_-]{1,4096}")  # header-safe
_MINT_COOLDOWN = 30  # seconds a failed mint is remembered: parallel calls get that error, not a new try


class _ChallengePage(BlockedError):
    """An HTML page where JSON was expected: bot protection, never a stale session."""


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


class _Pacer:
    """At most one request per `spacing` seconds across all renfe processes sharing `path`: the lock is
    held for the whole request and the file keeps the last request's wall-clock time. Without a usable
    lock file it paces this process only."""

    def __init__(self, spacing: float, path: Path | None):
        self._spacing = spacing
        self._path = path
        self._last: float | None = None

    def _open(self) -> int | None:
        if fcntl is None or self._path is None:
            return None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            return os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None

    @staticmethod
    def _read(fd: int | None) -> float:
        if fd is None:
            return 0.0
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return float(os.read(fd, 64).decode() or 0)
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _write(fd: int | None, moment: float) -> None:
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{moment:.3f}".encode())
        except OSError:
            pass

    @contextmanager
    def turn(self) -> Iterator[None]:
        if self._spacing <= 0:
            yield
            return
        fd = self._open()
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            last = max(self._last or 0.0, self._read(fd))
            wait = self._spacing - (time.time() - last)
            if wait > 0:
                time.sleep(min(wait, self._spacing))  # clamp: a clock jump never stalls us for long
            try:
                yield
            finally:
                self._last = time.time()
                self._write(fd, self._last)
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


def search_slot(spacing: float, path: Path | None) -> None:
    """Wait until `spacing` seconds have passed since the last search started on this machine (any renfe process
    sharing `path`), then mark this one as started. It spaces whole searches, not their requests."""
    with _Pacer(spacing, path).turn():
        pass


class RsvClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        spacing: float = config.REQUEST_SPACING,
        timeout: float = config.REQUEST_TIMEOUT,
        pace_file: Path | None = None,
        session_file: Path | None = None,
    ):
        self.transport = transport  # the legacy shop's search (legacy.py) sends through it too
        self._pacer = _Pacer(spacing, pace_file if pace_file is not None else config.cache_dir() / "pace.lock")
        self._session_file = session_file if session_file is not None else config.cache_dir() / "session.json"
        self._lock = threading.RLock()  # fare batches run in threads; one session for all of them
        self._api_key: str | None = None
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._terminal_id: str | None = None
        self._session_id = str(uuid.uuid4())
        self._from_disk = False
        self._generation = 0  # bumped whenever the session is replaced or dropped
        self._mint_error: RenfeError | None = None
        self._mint_error_at = 0.0
        self.retries: list[dict] = []  # each request that had to be retried (retry.py), for the output
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            http2=transport is None,  # one multiplexed connection for parallel fare calls
            follow_redirects=False,
            headers={"user-agent": config.USER_AGENT, "accept-language": config.ACCEPT_LANGUAGE},
        )

    def __enter__(self) -> "RsvClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # --- transport -------------------------------------------------------------------------------

    def _send(self, method: str, url: str, *, label: str, headers: dict[str, str], body: Any = None,
              may_retry: Callable[[], bool] | None = None) -> Any:
        """One request, tried again after a one-off failure (retry.py); `may_retry` can stop that."""
        return retry.call(label, lambda: self._send_once(method, url, label=label, headers=headers, body=body),
                          self.retries, may_retry=may_retry)

    def _send_once(self, method: str, url: str, *, label: str, headers: dict[str, str], body: Any = None) -> Any:
        with self._pacer.turn():
            try:
                response = self._http.request(method, url, headers=headers, json=body)
            except httpx.TimeoutException:  # never retried: it already cost the whole timeout
                raise UpstreamError(f"{label}: Renfe did not answer in time") from None
            except httpx.RequestError as exc:  # transport, decoding, redirect-loop errors
                raise UpstreamError(f"{label}: request failed ({type(exc).__name__})",
                                    transient=retry.connection_failure(exc)) from None
        return _checked_json(response, label)

    # --- anonymous session -----------------------------------------------------------------------

    def _fetch_api_key(self) -> str:
        cfg = self._send("GET", config.WEB_CONFIG_URL, label="web config", headers={"accept": "application/json"})
        key = None
        if isinstance(cfg, dict) and isinstance(cfg.get("arch"), dict):
            security = cfg["arch"].get("security")
            key = security.get("apiKey") if isinstance(security, dict) else None
        if not isinstance(key, str) or not _API_KEY.fullmatch(key):
            raise ApiShapeError("web config: no usable api key at arch.security.apiKey")
        return key

    def _mint(self) -> None:
        if self._mint_error is not None and time.time() - self._mint_error_at < _MINT_COOLDOWN:
            raise self._mint_error  # another call just tried and failed: don't ask Renfe again
        try:
            key = self._fetch_api_key()  # re-read on every mint: a rotated key heals within one token lifetime
            body = self._send(
                "GET",
                config.API_BASE + config.PUBLIC_TOKEN_PATH,
                label="anonymous token",
                headers={"accept": "application/json", "x-api-key": key, "channel": config.CHANNEL},
            )
            token = body.get("accessToken") if isinstance(body, dict) else None
            lifetime = body.get("expiresIn") if isinstance(body, dict) else None
            if not isinstance(token, str) or not _JWT.fullmatch(token):
                raise ApiShapeError("anonymous token: no accessToken in the response")
        except RenfeError as err:
            self._mint_error, self._mint_error_at = err, time.time()
            raise
        self._mint_error = None
        if not isinstance(lifetime, int) or isinstance(lifetime, bool) or lifetime <= 0:
            lifetime = _TOKEN_MAX_LIFETIME
        lifetime = min(lifetime, _TOKEN_MAX_LIFETIME)
        self._api_key, self._token = key, token
        self._token_expires = time.time() + max(lifetime - _TOKEN_MARGIN, 30)
        self._terminal_id = _terminal(token)
        self._session_id = str(uuid.uuid4())
        self._from_disk = False
        self._generation += 1
        cachefile.write_atomic(self._session_file, json.dumps({
            "api_key": key, "access_token": token, "expires_at": self._token_expires,
            "session_id": self._session_id,
        }))

    def _read_saved(self) -> dict | None:
        """The session in the cache file, if it is well formed and still valid."""
        try:
            saved = json.loads(self._session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # ValueError covers UnicodeError
            return None
        if not isinstance(saved, dict):
            return None
        key, token = saved.get("api_key"), saved.get("access_token")
        expires, session_id = saved.get("expires_at"), saved.get("session_id")
        now = time.time()
        if not (isinstance(key, str) and _API_KEY.fullmatch(key)
                and isinstance(token, str) and _JWT.fullmatch(token)
                and isinstance(expires, (int, float)) and not isinstance(expires, bool)
                and now < expires <= now + _TOKEN_MAX_LIFETIME
                and isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id)):
            return None
        exp = _jwt_claims(token).get("exp")
        if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp - _TOKEN_MARGIN <= now:
            return None
        return saved

    def _adopt(self, saved: dict) -> None:
        self._api_key, self._token = saved["api_key"], saved["access_token"]
        self._token_expires = float(saved["expires_at"])
        self._terminal_id = _terminal(self._token)
        self._session_id = saved["session_id"]
        self._from_disk = True
        self._generation += 1

    def _session(self) -> int:
        """Make sure a valid session is in memory; returns its generation."""
        with self._lock:
            if self._token is None or time.time() >= self._token_expires:
                saved = self._read_saved()
                if saved is not None:
                    self._adopt(saved)
                else:
                    self._mint()
            return self._generation

    def api_key(self) -> str:
        with self._lock:
            self._session()
            return self._api_key or ""

    def _api_headers(self) -> tuple[dict[str, str], int]:
        with self._lock:
            generation = self._session()
            headers = {
                "accept": "application/json, text/plain, */*",
                "authorization": f"Bearer {self._token}",
                "x-api-key": self._api_key or "",
                "channel": config.CHANNEL,
                "session-id": self._session_id,
                "origin": config.WEB_ORIGIN,
                "referer": f"{config.WEB_ORIGIN}/",
            }
            if self._terminal_id:
                headers["x-terminal-id"] = self._terminal_id
            return headers, generation

    def _stale(self, err: RenfeError, generation: int) -> bool:
        """Renfe refused a request: send it once more with another session? Only after a 401 to a session
        read from disk (or replaced since the request left). A 403 stays a block: it is reported, and a
        saved session that drew it is dropped so the next run starts afresh."""
        if isinstance(err, _ChallengePage) or err.status not in (401, 403):
            return False
        with self._lock:
            if generation != self._generation:  # another call already replaced that session
                return err.status == 401
            if not self._from_disk:
                return False  # minted by this process just now: a real refusal, report it
            saved = self._read_saved()
            newer = saved is not None and saved["access_token"] != self._token  # another run renewed it
            if err.status == 403:
                if not newer:
                    cachefile.remove(self._session_file)
                return False
            if newer:
                self._adopt(saved)
            else:
                cachefile.remove(self._session_file)
                self._token, self._from_disk = None, False
                self._generation += 1
            return True

    def _call(self, method: str, path: str, label: str, body: Any = None,
              may_resend: Callable[[], bool] | None = None) -> Any:
        """One API call: 3 tries at most, shared by the retries and the one stale-session resend, and one entry in
        `retries` for all of them. A failed mint isn't tried again here: it had its own tries."""
        url = config.API_BASE + path
        state = {"generation": 0, "sent": False, "resent": False}

        def once() -> Any:
            state["sent"] = False
            headers, state["generation"] = self._api_headers()
            state["sent"] = True
            return self._send_once(method, url, label=label, headers=headers, body=body)

        def stale(err: RenfeError) -> str | None:
            if (not state["sent"] or state["resent"] or not isinstance(err, (AuthError, BlockedError))
                    or not self._stale(err, state["generation"])):
                return None
            state["resent"] = True
            return f"session refused ({err.status}), sent again with another"

        def setup_failed(err: RenfeError) -> str | None:  # not the call's own failure: the session it needed
            return None if state["sent"] else f"new session failed: {err.message}"

        return retry.call(label, once, self.retries, resend=stale, describe=setup_failed,
                          may_retry=lambda: state["sent"] and (may_resend is None or may_resend()))

    # --- API calls -------------------------------------------------------------------------------

    def get(self, path: str, *, label: str | None = None) -> Any:
        return self._call("GET", path, label or path)

    def post(self, path: str, body: Any, *, label: str | None = None,
             may_resend: Callable[[], bool] | None = None) -> Any:
        """`may_resend` is asked before the one stale-session resend and before each retry; False reports the
        failure instead (fare batches use it so nothing more is sent once another batch was blocked)."""
        return self._call("POST", path, label or path, body, may_resend)


def _terminal(token: str) -> str | None:
    terminal = _jwt_claims(token).get("custom:terminal_id")
    return terminal if isinstance(terminal, str) and _TERMINAL_ID.fullmatch(terminal) else None


def _checked_json(response: httpx.Response, label: str) -> Any:
    status = response.status_code
    ctype = response.headers.get("content-type", "").lower()
    if "html" in ctype:  # a challenge or error page, whatever the status
        raise _ChallengePage(f"{label}: got an HTML page instead of JSON (bot protection?)", status=status)
    if 300 <= status < 400:
        raise BlockedError(f"{label}: Renfe redirected the request ({status}); bot protection or a moved endpoint",
                           status=status)
    if 200 <= status < 300:
        try:
            return response.json()
        except ValueError:
            raise ApiShapeError(f"{label}: response is not JSON", status=status) from None
    if status in (403, 429):
        wait = response.headers.get("retry-after", "")
        hint = f"; retry after {wait} s" if _RETRY_AFTER.fullmatch(wait) else ""
        raise BlockedError(f"{label}: Renfe refused the request ({status}){hint}; wait before trying again", status=status)
    if status == 401:
        raise AuthError(f"{label}: Renfe refused the anonymous session (401)", status=status)
    if status >= 500:
        raise UpstreamError(f"{label}: Renfe returned a server error ({status})", status=status,
                            transient=retry.status_failure(status))
    if status in (400, 404, 409, 422):
        code = _renfe_code(response)
        raise UsageError(f"{label}: Renfe rejected the request ({status})", status=status, code=code)
    raise ApiShapeError(f"{label}: unexpected HTTP status {status}", status=status)


def _renfe_code(response: httpx.Response) -> str | None:
    try:
        code = response.json().get("codeError")
    except Exception:
        return None
    return code if isinstance(code, str) and _RENFE_CODE.fullmatch(code) else None
