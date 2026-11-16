"""Against the real Renfe API (anonymous, public data). Skipped unless RENFE_LIVE=1; ~6 requests."""

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
