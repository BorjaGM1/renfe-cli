"""RsvClient against httpx.MockTransport: the anonymous session, headers and error mapping."""

import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from renfe import config
from renfe.client import RsvClient
from renfe.errors import ApiShapeError, AuthError, BlockedError, UpstreamError, UsageError

from fakes import API_KEY, TERMINAL, TOKEN, FakeRenfe, jwt


def client(handler) -> RsvClient:
    return RsvClient(transport=httpx.MockTransport(handler), spacing=0)


def test_anonymous_session_headers():
    fake = FakeRenfe()
    with client(fake) as c:
        c.get(config.STATIONS_PATH)
        c.get(config.STATIONS_PATH)
    assert fake.paths() == ["/config/arch-config.json", config.PUBLIC_TOKEN_PATH, config.STATIONS_PATH,
                            config.STATIONS_PATH]  # key and token fetched once, then reused
    token_req, api_req = fake.requests[1], fake.requests[2]
    assert token_req.headers["x-api-key"] == API_KEY and token_req.headers["channel"] == "VLP"
    assert "authorization" not in token_req.headers
    assert api_req.headers["authorization"] == f"Bearer {TOKEN}"
    assert api_req.headers["x-api-key"] == API_KEY
    assert api_req.headers["channel"] == "VLP"
    assert api_req.headers["x-terminal-id"] == TERMINAL
    assert api_req.headers["session-id"] == fake.requests[3].headers["session-id"]
    assert api_req.headers["user-agent"].startswith("renfe-cli/")


def test_post_sends_json():
    fake = FakeRenfe()
    with client(fake) as c:
        c.post(config.SEARCH_TRAVEL_PATH, {"a": 1})
    assert json.loads(fake.requests[-1].content) == {"a": 1}
    assert fake.requests[-1].headers["content-type"] == "application/json"


def test_missing_api_key_is_api_change():
    fake = FakeRenfe(**{"arch-config": lambda r: httpx.Response(200, json={"arch": {}})})
    with client(fake) as c, pytest.raises(ApiShapeError):
        c.get(config.STATIONS_PATH)


@pytest.mark.parametrize("status,error", [
    (403, BlockedError), (429, BlockedError), (401, AuthError), (502, UpstreamError), (418, ApiShapeError),
])
def test_status_mapping(status, error):
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: httpx.Response(status, json={})})
    with client(fake) as c, pytest.raises(error) as exc:
        c.get(config.STATIONS_PATH)
    assert exc.value.status == status


def test_bad_request_keeps_only_an_allowlisted_code():
    secret_echo = {"codeError": "F_NOT_READABLE", "clientMessage": "user@example.com said 600000001"}
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: httpx.Response(400, json=secret_echo)})
    with client(fake) as c, pytest.raises(UsageError) as exc:
        c.get(config.STATIONS_PATH)
    assert exc.value.code == "F_NOT_READABLE"
    payload = json.dumps(exc.value.to_dict())
    assert "example.com" not in payload and "600000001" not in payload


def test_html_instead_of_json_is_blocked():
    page = lambda r: httpx.Response(200, text="<html>challenge</html>", headers={"content-type": "text/html"})
    fake = FakeRenfe(**{config.STATIONS_PATH: page})
    with client(fake) as c, pytest.raises(BlockedError):
        c.get(config.STATIONS_PATH)


def test_errors_never_contain_key_or_token():
    def boom(request):
        raise httpx.ConnectError("failed " + request.headers.get("authorization", ""))

    fake = FakeRenfe(**{config.STATIONS_PATH: boom})
    with client(fake) as c, pytest.raises(UpstreamError) as exc:
        c.get(config.STATIONS_PATH)
    text = json.dumps(exc.value.to_dict())
    assert TOKEN not in text and API_KEY not in text


def test_timeout_is_upstream():
    def slow(request):
        raise httpx.ReadTimeout("slow")

    fake = FakeRenfe(**{config.STATIONS_PATH: slow})
    with client(fake) as c, pytest.raises(UpstreamError):
        c.get(config.STATIONS_PATH)


@pytest.mark.parametrize("response", [
    httpx.Response(400, text="<html>challenge</html>", headers={"content-type": "text/html; charset=utf-8"}),
    httpx.Response(302, headers={"location": "https://example.com/challenge"}),
])
def test_html_or_redirect_at_any_status_is_blocked(response):
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: response})
    with client(fake) as c, pytest.raises(BlockedError):
        c.get(config.STATIONS_PATH)


def test_decoding_error_is_upstream():
    def bad(request):
        raise httpx.DecodingError("bad gzip")

    fake = FakeRenfe(**{config.STATIONS_PATH: bad})
    with client(fake) as c, pytest.raises(UpstreamError):
        c.get(config.STATIONS_PATH)


def test_arch_as_string_is_api_change():
    fake = FakeRenfe(**{"arch-config": lambda r: httpx.Response(200, json={"arch": "oops"})})
    with client(fake) as c, pytest.raises(ApiShapeError):
        c.get(config.STATIONS_PATH)


def test_token_renewed_after_expiry(monkeypatch):
    import renfe.client as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    fake = FakeRenfe()
    with client(fake) as c:
        c.get(config.STATIONS_PATH)
        now[0] += 1700  # within 1800 s minus the 60 s margin: reused
        c.get(config.STATIONS_PATH)
        now[0] += 100   # past expiry - margin: a new anonymous token
        c.get(config.STATIONS_PATH)
    assert fake.paths().count(config.PUBLIC_TOKEN_PATH) == 2
    assert fake.paths().count("/config/arch-config.json") == 2  # every mint re-reads the key (rotation heals)


def test_pacing_spans_clients_through_the_lock_file(monkeypatch, tmp_path):
    import renfe.client as mod

    clock = [5_000_000.0]
    sleeps = []
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])
    monkeypatch.setattr(mod.time, "sleep", lambda s: (sleeps.append(round(s, 3)), clock.__setitem__(0, clock[0] + s)))
    lock = tmp_path / "pace.lock"
    first = RsvClient(transport=httpx.MockTransport(FakeRenfe()), spacing=1.0, pace_file=lock)
    second = RsvClient(transport=httpx.MockTransport(FakeRenfe()), spacing=1.0, pace_file=lock)
    with first, second:
        first._send("GET", config.WEB_CONFIG_URL, label="x", headers={})   # no wait: nothing ran before
        second._send("GET", config.WEB_CONFIG_URL, label="x", headers={})  # another client: waits 1 s
        clock[0] += 0.4
        first._send("GET", config.WEB_CONFIG_URL, label="x", headers={})   # 0.4 s later: waits 0.6 s
    assert sleeps == [1.0, 0.6]


def test_future_pace_timestamp_never_stalls_long(monkeypatch, tmp_path):
    import renfe.client as mod

    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    lock = tmp_path / "pace.lock"
    lock.write_text(str(mod.time.time() + 10_000))  # a clock that jumped back
    with RsvClient(transport=httpx.MockTransport(FakeRenfe()), spacing=1.0, pace_file=lock) as c:
        c._send("GET", config.WEB_CONFIG_URL, label="x", headers={})
    assert sleeps and max(sleeps) <= 1.0


def test_no_pause_by_default(monkeypatch):
    import renfe.client as mod

    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
    with RsvClient(transport=httpx.MockTransport(FakeRenfe())) as c:
        for _ in range(5):
            c.get(config.STATIONS_PATH)
    assert sleeps == [] and config.REQUEST_SPACING == 0


# --- the session cached on disk ----------------------------------------------------------------------

OLD_KEY = "o" * 40
OLD_TOKEN = jwt({"custom:terminal_id": "X0000009", "token_use": "access"})
OLD_SESSION = "11111111-2222-4333-8444-555555555555"


def session_file() -> Path:
    return config.cache_dir() / "session.json"


def write_session(**override):
    saved = {"api_key": OLD_KEY, "access_token": OLD_TOKEN, "expires_at": time.time() + 600,
             "session_id": OLD_SESSION, **override}
    session_file().parent.mkdir(parents=True, exist_ok=True)
    session_file().write_text(json.dumps(saved))


def refuse_old(status, *, then=None):
    """Answers `status` to requests carrying the old cached token, normally otherwise."""
    def handler(request):
        if request.headers.get("authorization") == f"Bearer {OLD_TOKEN}":
            return httpx.Response(status, json={"message": "Unauthorized"})
        return then(request) if then else httpx.Response(200, json=[])
    return handler


def test_session_is_reused_by_the_next_process():
    first_fake, second_fake = FakeRenfe(), FakeRenfe()
    with client(first_fake) as c:
        c.get(config.STATIONS_PATH)
    with client(second_fake) as c:  # a new client, as the next `renfe` run would be
        c.get(config.STATIONS_PATH)
    assert second_fake.paths() == [config.STATIONS_PATH]  # no config, no token: one request
    first_api, second_api = first_fake.requests[2], second_fake.requests[0]
    for header in ("authorization", "x-api-key", "x-terminal-id", "session-id"):
        assert first_api.headers[header] == second_api.headers[header]


def test_session_file_is_private_and_has_only_the_session():
    with client(FakeRenfe()) as c:
        c.get(config.STATIONS_PATH)
    assert stat.S_IMODE(session_file().stat().st_mode) == 0o600
    saved = json.loads(session_file().read_text())
    assert set(saved) == {"api_key", "access_token", "expires_at", "session_id"}
    assert saved["access_token"] == TOKEN and saved["api_key"] == API_KEY
    assert not list(session_file().parent.glob(".session-*"))  # no temp file left behind


def assert_minted_afresh():
    fake = FakeRenfe()
    with client(fake) as c:
        c.get(config.STATIONS_PATH)
    assert fake.paths()[:2] == ["/config/arch-config.json", config.PUBLIC_TOKEN_PATH]
    assert fake.requests[2].headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"api_key": OLD_KEY}), "\udcff"])
def test_unreadable_session_file_is_ignored(content):
    session_file().parent.mkdir(parents=True, exist_ok=True)
    session_file().write_text(content, errors="surrogateescape")
    assert_minted_afresh()


@pytest.mark.parametrize("override", [
    {"expires_at": time.time() - 1},            # expired
    {"expires_at": time.time() + 100_000},      # longer than any token lives: a clock jump or an edit
    {"expires_at": True},
    {"expires_at": "soon"},
    {"api_key": "short"},
    {"access_token": "no-dots"},
    {"session_id": "not-a-uuid"},
    {"access_token": "a.\u00e9.c"},              # not header-safe: would crash the request, not 401
    {"access_token": "a b.c.d"},
    {"access_token": jwt({"exp": time.time() - 5, "custom:terminal_id": "X0000009"})},  # the JWT says expired
])
def test_invalid_session_file_is_ignored(override):
    write_session(**override)
    assert_minted_afresh()


def test_refused_disk_session_is_renewed_once():
    write_session()
    fake = FakeRenfe(**{config.STATIONS_PATH: refuse_old(401)})
    with client(fake) as c:
        assert c.get(config.STATIONS_PATH) == []
    assert fake.paths() == [config.STATIONS_PATH, "/config/arch-config.json", config.PUBLIC_TOKEN_PATH,
                            config.STATIONS_PATH]
    assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert fake.requests[-1].headers["x-api-key"] == API_KEY  # the re-read key, not the cached one
    assert json.loads(session_file().read_text())["access_token"] == TOKEN


@pytest.mark.parametrize("response", [
    httpx.Response(403, json={"message": "Forbidden"}),
    httpx.Response(403, text="Forbidden", headers={"content-type": "text/plain"}),
])
def test_403_to_a_disk_session_is_a_block_and_drops_the_file(response):
    write_session()
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: response})
    with client(fake) as c, pytest.raises(BlockedError) as exc:
        c.get(config.STATIONS_PATH)
    assert exc.value.status == 403
    assert fake.paths() == [config.STATIONS_PATH]  # no new token, no second try
    assert not session_file().exists()  # the next run starts afresh (heals a rotated key)


def test_newer_session_saved_by_another_run_is_adopted_not_deleted():
    newer = jwt({"custom:terminal_id": "X0000002", "token_use": "access"})
    write_session()

    def stations(request):
        if request.headers["authorization"] == f"Bearer {OLD_TOKEN}":
            write_session(access_token=newer, session_id="22222222-3333-4444-8555-666666666666")  # meanwhile
            return httpx.Response(401, json={})
        return httpx.Response(200, json=[])

    fake = FakeRenfe(**{config.STATIONS_PATH: stations})
    with client(fake) as c:
        assert c.get(config.STATIONS_PATH) == []
    assert fake.paths() == [config.STATIONS_PATH, config.STATIONS_PATH]  # no mint: the other run's session
    assert fake.requests[1].headers["authorization"] == f"Bearer {newer}"
    assert json.loads(session_file().read_text())["access_token"] == newer


def test_403_does_not_drop_a_newer_session_another_run_saved():
    newer = jwt({"custom:terminal_id": "X0000002", "token_use": "access"})
    write_session()

    def stations(request):
        write_session(access_token=newer)
        return httpx.Response(403, json={})

    with client(FakeRenfe(**{config.STATIONS_PATH: stations})) as c, pytest.raises(BlockedError):
        c.get(config.STATIONS_PATH)
    assert json.loads(session_file().read_text())["access_token"] == newer


def test_failed_remint_is_not_repeated_by_parallel_calls():
    write_session()
    gate = threading.Barrier(3, timeout=5)

    def stations(request):
        gate.wait()  # all three calls carry the old session
        return httpx.Response(401, json={})

    fake = FakeRenfe(**{config.STATIONS_PATH: stations,
                        "arch-config": lambda r: httpx.Response(503, json={})})
    with client(fake) as c, ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(c.get, config.STATIONS_PATH) for _ in range(3)]
        errors = [f.exception() for f in futures]
    assert all(isinstance(e, UpstreamError) for e in errors)
    # one failed mint (its 3 tries of a 503), remembered for the others
    assert fake.paths().count("/config/arch-config.json") == 3


def test_mint_failure_is_retried_after_the_cooldown(monkeypatch):
    import renfe.client as mod

    now = [2_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    down = [True]
    fake = FakeRenfe(**{"arch-config": lambda r: httpx.Response(503, json={}) if down[0]
                        else httpx.Response(200, json={"arch": {"security": {"apiKey": API_KEY}}})})
    with client(fake) as c:
        with pytest.raises(UpstreamError):
            c.get(config.STATIONS_PATH)
        down[0] = False
        with pytest.raises(UpstreamError):
            c.get(config.STATIONS_PATH)  # within the cooldown: the remembered error, no request
        now[0] += 31
        c.get(config.STATIONS_PATH)
    assert fake.paths().count("/config/arch-config.json") == 3 + 1  # a 503 is tried 3 times, then the cooldown


def test_minted_token_with_unsafe_characters_is_an_api_change():
    fake = FakeRenfe(**{config.PUBLIC_TOKEN_PATH: lambda r: httpx.Response(
        200, json={"accessToken": "a.\u00e9.c", "expiresIn": 1800})})
    with client(fake) as c, pytest.raises(ApiShapeError):
        c.get(config.STATIONS_PATH)


def test_resend_can_be_vetoed_by_the_caller():
    write_session()
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: refuse_old(401)})
    with client(fake) as c, pytest.raises(AuthError):
        c.post(config.SEARCH_TRAVEL_PATH, {}, may_resend=lambda: False)  # e.g. another fare batch was blocked
    assert fake.paths() == [config.SEARCH_TRAVEL_PATH]  # no mint, no resend


def test_disk_session_refused_again_is_reported():
    write_session()
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: httpx.Response(401, json={})})
    with client(fake) as c, pytest.raises(AuthError):
        c.get(config.STATIONS_PATH)
    assert fake.paths().count(config.STATIONS_PATH) == 2  # one retry, never more


@pytest.mark.parametrize("status", [401, 403])
def test_fresh_session_refusal_is_not_retried(status):
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: httpx.Response(status, json={})})
    with client(fake) as c, pytest.raises((AuthError, BlockedError)):
        c.get(config.STATIONS_PATH)
    assert fake.paths().count(config.STATIONS_PATH) == 1
    assert fake.paths().count(config.PUBLIC_TOKEN_PATH) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(429, json={}),
    httpx.Response(403, text="<html>challenge</html>", headers={"content-type": "text/html"}),
    httpx.Response(503, json={}),
    httpx.Response(302, headers={"location": "https://example.com/"}),
])
def test_disk_session_not_renewed_for_blocks_or_outages(response):
    write_session()
    fake = FakeRenfe(**{config.STATIONS_PATH: lambda r: response})
    with client(fake) as c, pytest.raises(BlockedError):
        c.get(config.STATIONS_PATH)
    tries = 3 if response.status_code == 503 else 1  # an outage is retried; a block or a redirect never is
    assert fake.paths() == [config.STATIONS_PATH] * tries  # and no new token
    assert session_file().exists()


def test_parallel_calls_share_one_session():
    fake = FakeRenfe()
    with client(fake) as c, ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: c.get(config.STATIONS_PATH), range(16)))
    assert fake.paths().count(config.PUBLIC_TOKEN_PATH) == 1
    assert fake.paths().count("/config/arch-config.json") == 1


def test_parallel_refusals_of_a_disk_session_mint_once():
    write_session()
    gate = threading.Barrier(4, timeout=5)

    def stations(request):
        if request.headers.get("authorization") == f"Bearer {OLD_TOKEN}":
            gate.wait()  # all four calls used the old session before any of them renews it
            return httpx.Response(401, json={})
        return httpx.Response(200, json=[])

    fake = FakeRenfe(**{config.STATIONS_PATH: stations})
    with client(fake) as c, ThreadPoolExecutor(4) as pool:
        assert list(pool.map(lambda _: c.get(config.STATIONS_PATH), range(4))) == [[]] * 4
    assert fake.paths().count(config.PUBLIC_TOKEN_PATH) == 1
    assert fake.paths().count(config.STATIONS_PATH) == 8


def test_retries_and_the_stale_session_resend_share_one_budget_of_three_tries(retry_pauses):
    """500, 500, then 401 to the saved session: 3 tries, then the 401 is reported. Before, the resend started a
    second round of retries (6 requests) and the log had two entries for the one call."""
    write_session()
    answers = [httpx.Response(500, json={}), httpx.Response(500, json={}), httpx.Response(401, json={}),
               httpx.Response(500, json={}), httpx.Response(500, json={})]
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: answers.pop(0) if answers else fake.default(r)})
    with client(fake) as c, pytest.raises(AuthError):
        c.post(config.SEARCH_TRAVEL_PATH, {}, label="search-travel")
    assert fake.paths().count(config.SEARCH_TRAVEL_PATH) == 3 and retry_pauses == [1.0, 2.0]
    assert c.retries == [{"request": "search-travel", "tries": 3, "recovered": False, "errors": [
        "server error (500)", "server error (500)", "search-travel: Renfe refused the anonymous session (401)"]}]


def test_a_stale_session_resent_then_retried_is_one_entry(retry_pauses):
    """401 to the saved session, resent at once with a new one (no pause), a 500, then the answer: 3 tries, and the
    one pause is the first retry's, 1 s."""
    write_session()
    answers = [httpx.Response(401, json={}), httpx.Response(500, json={})]
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: answers.pop(0) if answers else fake.default(r)})
    with client(fake) as c:
        c.post(config.SEARCH_TRAVEL_PATH, {}, label="search-travel")
    assert fake.paths().count(config.SEARCH_TRAVEL_PATH) == 3 and retry_pauses == [1.0]
    assert c.retries == [{"request": "search-travel", "tries": 3, "recovered": True, "errors": [
        "session refused (401), sent again with another", "server error (500)"]}]


def test_a_failed_mint_isnt_tried_again_by_the_call_that_needed_it(retry_pauses):
    """The web config's own 3 tries, then the call gives up: no second round."""
    fake = FakeRenfe(**{"arch-config": lambda r: httpx.Response(503, json={})})
    with client(fake) as c, pytest.raises(UpstreamError):
        c.get(config.STATIONS_PATH)
    assert fake.paths().count("/config/arch-config.json") == 3 and config.STATIONS_PATH not in fake.paths()
    assert [e["request"] for e in c.retries] == ["web config"]


def test_a_403_on_the_third_try_still_drops_the_saved_session(retry_pauses):
    """500, 500, then 403 to the saved session: no fourth try, but the file is dropped as for a 403 on the first."""
    write_session()
    answers = [httpx.Response(500, json={}), httpx.Response(500, json={}), httpx.Response(403, json={})]
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: answers.pop(0)})
    with client(fake) as c, pytest.raises(BlockedError):
        c.post(config.SEARCH_TRAVEL_PATH, {}, label="search-travel")
    assert fake.paths().count(config.SEARCH_TRAVEL_PATH) == 3 and not session_file().exists()


def test_a_new_session_failing_after_a_resend_is_named_as_such(retry_pauses):
    """401 to the saved session; the new session's web config answers 503 three times. The call's entry says the
    session failed, so its last error isn't read as search-travel's own 503."""
    write_session()
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: refuse_old(401),
                        "arch-config": lambda r: httpx.Response(503, json={})})
    with client(fake) as c, pytest.raises(UpstreamError):
        c.post(config.SEARCH_TRAVEL_PATH, {}, label="search-travel")
    call = next(e for e in c.retries if e["request"] == "search-travel")
    assert call == {"request": "search-travel", "tries": 2, "recovered": False, "errors": [
        "session refused (401), sent again with another",
        "new session failed: web config: Renfe returned a server error (503)"]}

