"""Constants observed on Renfe's web app (venta.rsv.renfe.com, anonymous session, 2026-11-15).

The static x-api-key is not stored here: the client reads it at run time from the public web config,
the same way the web app does, so a rotation doesn't break the CLI.
"""

import os
from datetime import date
from pathlib import Path

WEB_ORIGIN = "https://venta.rsv.renfe.com"
WEB_CONFIG_URL = f"{WEB_ORIGIN}/config/arch-config.json"
API_BASE = "https://api.rsv.renfe.com"
CHANNEL = "VLP"  # the `channel` header the web app sends
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "es-ES"
TIMEZONE = "Europe/Madrid"

# Seconds between requests across every renfe process. 0 = no pause: Renfe answers in 0.1-0.5 s and its
# web app fires its calls back to back. A block or rate limit still stops the CLI (exit 5).
REQUEST_SPACING = 0.0
REQUEST_TIMEOUT = 25.0

PUBLIC_TOKEN_PATH = "/user/external-users-login/v1/tokens/public"
STATIONS_PATH = "/catalog/masterdata/v1/stations"
SEARCH_TRAVEL_PATH = "/browsing/traveloptions/v1/search-travel"
JOURNEY_SEARCH_PATH = "/browsing/journey/v1/orientations/journey-search"

# Renfe runs two sales systems until early 2027. The new one (this API) prices journeys from this date;
# earlier journeys are sold by the legacy shop at venta.renfe.com, which search() asks for them (legacy.py).
NSV_FIRST_SALE_DATE = date(2027, 1, 25)
LEGACY_SHOP_URL = "https://venta.renfe.com/vol/"

# Where a search continues in the browser, to pick the train and pay (checked 2026-11-16 as a guest, nothing
# bought). www.renfe.com takes the route and the date in the URL, for the dates the legacy shop sells. The
# new shop takes the route and "one way" in the URL but not the date: that comes from a cookie www.renfe.com
# sets, so on the new shop the date is picked on the page.
WWW_SEARCH_URL = "https://www.renfe.com/es/es/buscador-billetes"
SHOP_SEARCH_URL = f"{WEB_ORIGIN}/coext/search"

FARE_BATCH = 3  # trains per journey-search call, as the web app sends them (Renfe refuses 22 per call)
FARE_WORKERS = 3  # journey-search calls in flight at once, as the web app keeps them (2026-11-15)
EXCLUDED_PRODUCT_TYPES = ("DG", "BK")  # sent by the web app on every search; meaning unconfirmed

# Renfe's live map of long- and medium-distance trains, the public JSON its map page reads (no key, no session;
# Cache-Control: max-age=30, 2026-11-18). It lists trains only while they run, and is unreachable at times: on 17 Nov
# it refused connections from Spain for over 2 h; on 18 Nov, checked every 15 min, it dropped them once, for under
# a minute.
LIVE_MAP_URL = "https://tiempo-real.largorecorrido.renfe.com/renfe-visor/flotaLD.json"
LIVE_TIMEOUT = 10.0  # how long `status` and `watch` wait for the map
LIVE_SEARCH_BUDGET = 2.5  # seconds from its start a search for today waits for the map, which it fetches alongside
WATCH_INTERVAL = 120.0  # seconds between `watch`'s checks (Renfe caches the map's answer for 30 s)
WATCH_LIMIT = 12 * 3600.0  # `watch` ends on its own after this: the map doesn't show a trip's end
WATCH_MAX_UNREADABLE = 3  # answers in a row that aren't the map before `watch` stops (exit 7): it moved or changed

STATIONS_TTL = 24 * 3600
CACHE_ENV = "RENFE_CACHE_DIR"


def cache_dir() -> Path:
    if env := os.environ.get(CACHE_ENV):
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or ""
    return (Path(base) if os.path.isabs(base) else Path.home() / ".cache") / "renfe-cli"
