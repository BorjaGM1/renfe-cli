"""Against the real Renfe API, legacy shop and live map (anonymous, public data). Skipped unless RENFE_LIVE=1;
about 11 requests."""

import os
from datetime import date, timedelta

import pytest

from renfe import config, search, stations
from renfe.client import RsvClient

REAL_SHIPPED = stations.bundled  # the real list; conftest gives every test a fake one

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RENFE_LIVE") != "1", reason="set RENFE_LIVE=1 to call the real API"),
]


def test_live_search_with_fares(tmp_path):
    with RsvClient() as client:
        every = stations.load(client, path=tmp_path / "stations.json")
        src, dst = stations.resolve("Madrid", every), stations.resolve("Barcelona", every)
        out = search.search(client, src, dst, max(config.NSV_FIRST_SALE_DATE, date.today()) + timedelta(days=11),
                               names={s.code: s.name for s in every}, limit=3)
    assert out["count"] > 0
    assert any(t["fares"] for t in out["trains"]), "no train came back with fares"


def test_live_legacy_shop_lists_trains_with_fares():
    """Before the cut-off: the legacy shop's four requests, no request to the new system."""
    day = date.today() + timedelta(days=10)
    if day >= config.NSV_FIRST_SALE_DATE:
        pytest.skip("no date before the cut-off is left; delete the legacy-shop code (issue #1)")
    every = stations.bundled()
    src, dst = stations.resolve("Madrid", every), stations.resolve("Barcelona", every)
    with RsvClient() as client:
        out = search.search(client, src, dst, day, names={s.code: s.name for s in every}, limit=200)
    assert out["notice"] is None, out["notice"]  # a notice here means it fell back to the new system
    assert out["count"] > 0 and out["fares_included"] is True
    assert any(t["available"] and t["fares"] for t in out["trains"]), "no train on sale came back with fares"


def test_live_status_of_a_train_on_the_map():
    """One request to Renfe's live map. Skips, not fails, when it doesn't answer (often, from Spain) or is empty
    (at night)."""
    from renfe import live
    from renfe.errors import UpstreamError

    try:
        board = live.fetch()
    except UpstreamError as err:
        pytest.skip(f"Renfe's live map didn't answer: {err.message}")
    if not board.trains:
        pytest.skip("Renfe's live map lists no trains right now (night)")
    names = {s.code: s.name for s in REAL_SHIPPED()}
    listed = [live.status(board, number, names) for number in board.trains]
    assert all(out["on_map"] for out in listed)
    assert any(isinstance(out["delay_minutes"], int) for out in listed), "no train on the map has a readable delay"
    assert any(out["next_station"] and out["next_station"]["name"] for out in listed), "no next station has a name"
