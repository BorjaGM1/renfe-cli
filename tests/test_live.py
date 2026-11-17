"""Against the real Renfe API and legacy shop (anonymous, public data). Skipped unless RENFE_LIVE=1; ~10 requests."""

import os
from datetime import date, timedelta

import pytest

from renfe import config, search, stations
from renfe.client import RsvClient

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
