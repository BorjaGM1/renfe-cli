"""The `renfe` command line: JSON on stdout, errors as JSON on stderr with the documented exit codes."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from renfe import cli, config
from renfe.client import RsvClient

from fakes import FakeRenfe

runner = CliRunner()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin "now" to 2026-11-15 10:00 Madrid, so date-pinned tests never hit the today filter."""
    from datetime import datetime as real_datetime

    from renfe import search as search_mod

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 15, 10, 0, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", Frozen)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path))
    handler = FakeRenfe()
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    return handler


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def test_search_outputs_json(fake):
    result = invoke("search", "Madrid", "Barcelona", "--date", "2027-01-29", "--limit", "2")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["origin"]["code"] == "MADRI" and out["destination"]["code"] == "BARCE"
    assert out["count"] == 2 and out["trains"][0]["fares"]


def test_search_no_fares(fake):
    result = invoke("search", "60000", "71801", "-d", "2027-01-29", "--no-fares")
    assert result.exit_code == 0
    assert "/browsing/journey/v1/orientations/journey-search" not in fake.paths()


@pytest.mark.parametrize("args,needle", [
    (("search", "Madrid", "Barcelona", "--date", "29/01/2027"), "YYYY-MM-DD"),
    (("search", "Madrid", "Barcelona", "--after", "25:00"), "HH:MM"),
    (("search", "Narnia", "Barcelona"), "no station matches"),
    (("search", "Madrid", "MADRI"), "same station"),
])
def test_usage_errors(fake, args, needle):
    result = invoke(*args)
    assert result.exit_code == 2
    err = json.loads(result.stderr)["error"]
    assert err["type"] == "usage" and needle in err["message"]
    assert result.stdout == ""


def test_blocked_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path))
    handler = FakeRenfe(**{"stations": lambda r: httpx.Response(429, json={}, headers={"retry-after": "30"})})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("stations", "madrid")
    assert result.exit_code == 5
    err = json.loads(result.stderr)["error"]
    assert err["type"] == "blocked" and "retry after 30" in err["message"]


def test_unexpected_exception_reports_type_only(monkeypatch):
    def broken():
        raise RuntimeError("Bearer secret-token-value")

    monkeypatch.setattr(cli, "make_client", broken)
    result = invoke("stations")
    assert result.exit_code == 1
    assert "secret-token-value" not in result.stderr and "RuntimeError" in result.stderr


def test_stations(fake):
    result = invoke("stations", "madrid", "--limit", "2")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert [s["code"] for s in out["stations"]] == ["MADRI", "60000"]


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0 and result.stdout.strip()


@pytest.mark.parametrize("argv,needle", [
    (["search", "Madrid"], "destination"),
    (["search", "Madrid", "Barcelona", "--limit", "0"], "--limit"),
    (["nonsense"], "nonsense"),
])
def test_main_renders_click_usage_errors_as_json(fake, capsys, argv, needle):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["type"] == "usage" and needle in err["message"]


def test_main_success_and_failure_exit_codes(fake, capsys):
    with pytest.raises(SystemExit) as ok:
        cli.main(["stations", "madrid", "--limit", "1"])
    assert ok.value.code == 0 and json.loads(capsys.readouterr().out)["count"] == 1
    with pytest.raises(SystemExit) as bad:
        cli.main(["search", "Narnia", "Madrid"])
    assert bad.value.code == 2 and json.loads(capsys.readouterr().err)["error"]["type"] == "usage"


def test_today_hides_trains_that_already_left(fake, monkeypatch):
    from datetime import datetime as real_datetime

    from renfe import search as search_mod

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2027, 1, 29, 9, 30, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", Frozen)
    result = invoke("search", "Madrid", "Barcelona", "--no-fares")
    assert result.exit_code == 0, result.output
    departures = [t["departure"][11:16] for t in json.loads(result.stdout)["trains"]]
    assert departures and min(departures) >= "09:30"


def test_block_during_fares_prints_partial_result_and_exits_5(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path))
    handler = FakeRenfe(**{"journey-search": lambda r: httpx.Response(503, json={})})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("search", "Madrid", "Barcelona", "--date", "2027-01-29")
    assert result.exit_code == 5
    out = json.loads(result.stdout)
    assert out["count"] == 4 and out["fares_error"]["type"] == "upstream_unavailable"
    assert json.loads(result.stderr)["error"]["http_status"] == 503


def test_today_with_every_train_gone_says_so(fake, monkeypatch):
    from datetime import datetime as real_datetime

    from renfe import search as search_mod

    class Late(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2027, 1, 29, 23, 30, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", Late)
    result = invoke("search", "Madrid", "Barcelona", "--no-fares")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["count"] == 0 and "No more trains today after 23:30" in out["notice"]
