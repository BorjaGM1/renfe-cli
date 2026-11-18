"""Links that continue a search on Renfe's website, where you pick the train and pay.

The open command builds the link and, when asked, opens it in your browser. A link can't
select a train, so the page opens on that day's search and you choose the train there.
"""

import webbrowser
from datetime import date
from urllib.parse import urlencode

from . import config
from .stations import Station


def link(origin: Station, destination: Station, day: date) -> dict:
    """The page to open for this search, what it arrives with filled in, and what's left to do there."""
    if day < config.NSV_FIRST_SALE_DATE:  # sold on the legacy shop, which www.renfe.com searches for you
        query = {"origin": origin.code, "destination": destination.code, "departure-date": day.isoformat()}
        return {
            "url": f"{config.WWW_SEARCH_URL}?{urlencode(query)}",
            "prefilled": ["origin", "destination", "date"],
            # the page's search button stays disabled until one of these is set; a link can't set "one way"
            "next": "Mark 'Solo ida' (or pick a return date), press 'Buscar billetes' and pick your train.",
        }
    query = {"originUicStationCode": origin.code, "destinationUicStationCode": destination.code, "isOneWay": "true"}
    return {
        "url": f"{config.SHOP_SEARCH_URL}?{urlencode(query)}",
        "prefilled": ["origin", "destination", "one_way"],
        "next": f"Pick {day.isoformat()} as the date (Renfe's new shop doesn't take one in a link), "
                "press 'Buscar billetes' and pick your train.",
    }


def open_in_browser(url: str) -> bool:
    """Open `url` in the default browser; False when there is none or it fails to start."""
    try:
        return bool(webbrowser.open(url, new=2))
    except (webbrowser.Error, OSError):
        return False
