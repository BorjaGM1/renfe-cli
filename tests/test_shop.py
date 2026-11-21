"""shop.link(): the page on Renfe's website where a search continues, to pick the train and pay."""

import webbrowser
from datetime import date

import pytest
from urllib.parse import parse_qs, urlsplit

from renfe import config, shop
from renfe.stations import parse

from fakes import STATIONS

EVERY = {s.code: s for s in parse(STATIONS)}
MADRI, BARCE, SANTS = EVERY["MADRI"], EVERY["BARCE"], EVERY["71801"]


def parts(url):
    split = urlsplit(url)
    return f"{split.scheme}://{split.netloc}{split.path}", {k: v[0] for k, v in parse_qs(split.query).items()}


def test_before_the_cutoff_www_takes_route_and_date():
    page = shop.link(MADRI, BARCE, date(2026, 11, 20))
    base, query = parts(page["url"])
    assert base == "https://www.renfe.com/es/es/buscador-billetes"
    assert query == {"origin": "MADRI", "destination": "BARCE", "departure-date": "2026-11-20"}
    assert page["prefilled"] == ["origin", "destination", "date"]
    assert "Solo ida" in page["next"]  # the page won't search until one way (or a return date) is set


def test_from_the_cutoff_the_new_shop_takes_route_but_not_date():
    page = shop.link(SANTS, MADRI, config.NSV_FIRST_SALE_DATE)
    base, query = parts(page["url"])
    assert base == "https://venta.rsv.renfe.com/coext/search"
    assert query == {"originUicStationCode": "71801", "destinationUicStationCode": "MADRI", "isOneWay": "true"}
    assert "date" not in page["prefilled"]
    assert "2027-01-25" in page["next"]  # says which date to pick


def test_last_legacy_day_still_goes_to_www():
    page = shop.link(MADRI, BARCE, date(2027, 1, 24))
    assert page["url"].startswith(config.WWW_SEARCH_URL + "?")


def test_open_in_browser_reports_failure(monkeypatch):
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: False)
    assert shop.open_in_browser("https://example.com/") is False

    def broken(url, new=0):
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "open", broken)
    assert shop.open_in_browser("https://example.com/") is False


def test_the_no_browser_guard_is_never_swallowed():
    """conftest's guard must fail any test that reaches the real webbrowser.open."""
    with pytest.raises(AssertionError, match="real browser"):
        shop.open_in_browser("https://example.com/")

