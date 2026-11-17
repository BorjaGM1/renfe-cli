import sys
from pathlib import Path

import pytest

from renfe import config

sys.path.insert(0, str(Path(__file__).parent))  # lets tests import the shared `fakes` module


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """No test may open a browser: `renfe open` tests replace this with a recorder."""
    import webbrowser

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a real browser")

    monkeypatch.setattr(webbrowser, "open", refuse)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Every test gets its own cache dir: no test reads or writes the real session, stations or pace
    files (the live test included)."""
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path_factory.mktemp("cache")))


@pytest.fixture(autouse=True)
def shipped_stations(monkeypatch):
    """Lookups use a small fake shipped list, so refreshing the real one never changes a test. Tests of the
    real file use `stations.bundled` as imported before this runs."""
    from fakes import SHIPPED

    from renfe import stations

    every = stations.parse(SHIPPED)
    monkeypatch.setattr(stations, "bundled", lambda: every)
