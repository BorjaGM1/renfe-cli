# renfe-cli

Search Renfe trains and fares from the terminal. It talks directly to Renfe's own web API, the same
anonymous way the website does before anyone signs in; for trips before 25 Jan 2027, to Renfe's legacy
shop, as a guest. No account, no browser to search, and JSON output. To buy, it opens Renfe's website on
your search.

> Unofficial. Renfe's API is undocumented and can change without notice. The CLI sends what the
> website sends, no more: one timetable call, then fares 3 trains per call with at most 3 calls in
> flight; before 25 Jan 2027, the four requests the legacy shop's results page makes. If Renfe blocks
> or rate-limits it, it stops and says so (exit 5); before 25 Jan 2027 it falls back instead (see
> below). The only retry is one resend when Renfe rejects a saved session with 401 (see below).

## Use

```sh
uv run renfe search Madrid Barcelona --date 2027-01-29
uv run renfe search Atocha Sants -d tomorrow --after 17:00 --limit 5
uv run renfe search MADRI BARCE -d 2027-02-05 --no-fares   # timetable only
uv run renfe stations sevilla                             # find a station or its code
uv run renfe open Madrid Barcelona -d 2027-01-29 --train 03081   # buy it on Renfe's website
```

`search` returns, for each train:
- service number and `type_code`, plus `type` (AVE, Intercity…) when fares are fetched; a train
  with a change gets one name per leg, as on the website (`INTERCITY + INTERCITY`). Before 25 Jan 2027
  `type` is always there and `type_code` is `null`;
- departure and arrival in each station's local time, as Renfe reports it;
- duration and number of changes;
- fares for one adult, cheapest first.

It also returns `shop`: the page on Renfe's website where that search continues, to pick the train and
pay.

**Buying.** `renfe open FROM TO -d DATE` opens that page in your browser when run from a terminal
(`--browser` forces it, `--no-browser` only prints the link). Add `--train` to check the train exists
that day and show it, so you know which one to pick. The CLI never books or pays: a link can't select a train, so you pick it and pay on Renfe's page.
- **Dates before 25 Jan 2027:** www.renfe.com opens with the route and the date filled in. Mark
  "Solo ida" (or pick a return date) to enable "Buscar billetes"; it then searches Renfe's legacy shop.
- **From 25 Jan 2027:** Renfe's new shop opens with the route filled in and one way selected. Pick the
  date there: the new shop doesn't take a date in a link.
- `--train` is checked against the full list (before 25 Jan 2027, the legacy shop's). If the legacy shop
  fails, the CLI sees only the new system's partial list, so a train it can't see still opens the page,
  with a `notice`.

Dates are YYYY-MM-DD, `today` or `tomorrow`, up to a year ahead. `search` returns at most 10 trains by default
(`--limit`, up to 200). Fares cost one extra request per 3
trains. For today, trains that have already left are hidden unless you pass `--after`.

Speed (measured 2026-11-15): a search takes about 0.3–0.5 s, and a full day of 22 trains with fares
about 1.4 s. The anonymous session (a 30-minute token and the site's public API key) is kept in the
cache folder as `session.json`, so a timetable search usually sends a single request; fares add one per
3 trains. If Renfe answers 401 to a saved session, the CLI opens a new one and resends that request
once. A 403 is reported as a block, and the saved session is dropped so the next run starts afresh.
Before 25 Jan 2027 a search is the legacy shop's four requests instead, about 0.8–1.8 s with fares
(measured 2026-11-17), and nothing is saved: its cookies live in memory for that one search.

**Before 25 Jan 2027: Renfe's legacy shop.** Renfe is moving to a new sales system, which prices trips
only from 25 Jan 2027 and before that knows only some trains (for Madrid–Barcelona on Friday 20 Nov 2026,
8 of the 24). Renfe sells the earlier trips on its legacy shop (venta.renfe.com), so for those dates
`search` asks the legacy shop, the way its results page does, and gets every train with its fares in the
same JSON (Madrid–Barcelona on Friday 27 Nov 2026, checked 2026-11-17: 24 trains, cheapest €31.15).
There, `type_code` and `intermediate_stops` are `null`, and a train that isn't on sale (full, "not on
sale at the moment", not running, or only wheelchair places left) has `available: false` and no fares.
The CLI only searches there: it never selects a train or a fare.

If the legacy shop fails (a block or rate limit, a waiting room or challenge page, an answer the CLI
can't read, or an empty list), you get the new system's partial list instead, without fares, and a
`notice` saying what failed; the exit code stays 0. The CLI never tries to get past a waiting room or a
captcha. `shop.url` and `renfe open` take you to the full list on Renfe's website either way.

From 25 Jan 2027 the CLI's list matches the website's exactly, trains and cheapest fares (checked
against the web for Friday 29 Jan: 22 trains each).

Stations are matched by name, ignoring accents and case, or by code. A city name such as `Madrid` means
all of that city's stations (`MADRI`). Renfe's list of about 1,400 stations ships with the CLI, so
finding one sends no request. Only a name that matches none of them makes the CLI ask Renfe for its
current list, in case the station is newer than the shipped one; that list is kept for a day.
`renfe stations --refresh` asks Renfe directly. The cache folder is `~/.cache/renfe-cli`, or
`$XDG_CACHE_HOME/renfe-cli`, or `$RENFE_CACHE_DIR` if set.

## Output and errors

stdout is JSON. Errors go to stderr as `{"error": {...}}`, with these exit codes:

| Code | Meaning |
|---|---|
| `0` | ok; check `notice` |
| `1` | internal error (only the exception type is shown) |
| `2` | usage: bad arguments, unknown station, bad date, or a request Renfe rejected |
| `3` | auth: Renfe refused the anonymous session |
| `5` | blocked or upstream: 403/429, a bot page, timeout or 5xx. If this happens while fetching fares, stdout still has the timetable (plus `fares_error`). On the legacy shop it falls back instead (exit 0, `notice`) |
| `7` | API changed |

## Develop

```sh
uv sync
uv run pytest                      # offline: HTTP is mocked
RENFE_LIVE=1 uv run pytest -m live # about 10 real requests
uv run python scripts/update_stations.py  # refresh the shipped station list; prints what changed
```

A scheduled workflow (`.github/workflows/stations-reminder.yml`) opens an issue to refresh the station
list on 1 February and 1 August. GitHub turns schedules off in a public repo after 60 days without
activity; re-enable it from the Actions tab, or run it from there by hand.

Personal-data guard: `.githooks/` checks staged changes and commit messages for passengers' and
buyers' data (names, DNI/NIE, emails, phones, booking locators, card numbers) and for capture files.
Enable it once per clone with `python3 .githooks/pii_check.py enable`.
