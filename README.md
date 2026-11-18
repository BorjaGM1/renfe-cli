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
> Live delays (`status`, `watch`, and `search` for today) come from a second host the website's search never
> calls: Renfe's public live train map, one request per check (see [Delays](#delays)).

## Use

```sh
uv run renfe search Madrid Barcelona --date 2027-01-29
uv run renfe search Atocha Sants -d tomorrow --after 17:00 --limit 5
uv run renfe search MADRI BARCE -d 2027-02-05 --no-fares   # timetable only
uv run renfe stations sevilla                             # find a station or its code
uv run renfe open Madrid Barcelona -d 2027-01-29 --train 03081   # buy it on Renfe's website
uv run renfe status 03081                                 # that train's live delay and next station
uv run renfe watch 03081                                  # a JSON line each time that changes
```

`search` returns, for each train:
- service number and `type_code`, plus `type` (AVE, Intercity…) when fares are fetched; a train
  with a change gets one name per leg, as on the website (`INTERCITY + INTERCITY`). Before 25 Jan 2027
  `type` is always there and `type_code` is `null`;
- departure and arrival in each station's local time, as Renfe reports it;
- duration and number of changes;
- fares for one adult, cheapest first;
- `delay_minutes`: for today, the train's live delay, else `null`. A train with a change also has one on
  each leg, and the train's is the first of those the map has.

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
trains. For today, trains that have already left are hidden unless you pass `--after`; before 25 Jan 2027
the legacy shop can leave them out anyway (see [Delays](#delays)).

Speed (measured 2026-11-15): a search takes about 0.3–0.5 s, and a full day of 22 trains with fares
about 1.4 s. The anonymous session (a 30-minute token and the site's public API key) is kept in the
cache folder as `session.json`, so a timetable search usually sends a single request; fares add one per
3 trains. If Renfe answers 401 to a saved session, the CLI opens a new one and resends that request
once. A 403 is reported as a block, and the saved session is dropped so the next run starts afresh.
Before 25 Jan 2027 a search is the legacy shop's four requests instead, about 0.8–1.8 s with fares
(measured 2026-11-17), and nothing is saved: its cookies live in memory for that one search. A search for
today also sends one request to Renfe's live map, alongside the others (see [Delays](#delays)).

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

## Delays

`renfe status TRAIN` shows a train's live delay from Renfe's live train map, the public JSON behind its map
page (`tiempo-real.largorecorrido.renfe.com`, no key or session). TRAIN is a number from `renfe search`
(`03081`, leading zeros optional); for a train with a change, one leg's number. It returns
`delay_minutes` (negative: early), the train's origin and destination, its previous and next station (with
the expected arrival there), `overdue`, `reported_at` and `map_updated`. `reported_at` is the time on the
train's entry; Renfe refreshes it every few minutes even when the entry's delay and stations are hours old,
so it doesn't show how fresh they are.
- **While it runs, and mainly long- and medium-distance trains.** The map lists trains around the time they
  run (some while still at their first station), and at night it is empty. A train that isn't on it has
  `on_map: false` and no delay: it may not have left yet, may have arrived or been cancelled, or isn't on
  the map at all. Its delay is unknown, not zero.
- **The delay is the last one Renfe recorded.** A train held somewhere keeps its delay and next station
  while it gets later. `overdue` is `true` once the expected arrival at its next station is more than 5
  minutes behind the map's update, or more than 10 when Renfe gives the same station as previous and next
  (the train is probably stopped there, and the notice says since when); never while the train runs early,
  waiting for its time. After midnight the map can date that arrival a day ahead; `status` brings it back.
- **Whichever run is on the map.** The map has no run date, so after midnight `status` and `watch` can show
  yesterday's run of a train that runs again today, still going (on 18 Nov at 02:14, 18135 of the 17th, 170
  minutes late). Only `search` guards against that (below).
- **Often unreachable.** On 17 Nov 2026 the map was unreachable from Spain for over two hours while it
  answered from other countries. `status` fails with exit 5 when the map refuses the connection, or doesn't
  connect or send data for 10 s at a time.

**In `search`, for today only.** The search asks the map alongside its own requests and waits for it at most
2.5 s from its start, so a map that hangs costs at most that. Each train on the map gets its
`delay_minutes`, the last one recorded, as in `status`; one that isn't gets `null`, and so do they all on
other days. `delays_included` says whether the map answered: when it doesn't, `notice` says the delays are
unknown, and the search and its exit code are unaffected. A train gets no delay from an entry left over on
the map: one whose expected arrival at its next station is more than 45 minutes past (on 17 Nov at 17:48,
4 of 95, one of them still at its first station 7.5 hours later with a delay of 0). Nor from yesterday's
run: the map's time for the train must be less than 12 hours before the leg searched leaves or after it
arrives. `renfe open` never asks the map.

Today's list hides trains past their scheduled departure, even a late one that hasn't reached your station
yet:
- **Before 25 Jan 2027** the list comes from the legacy shop, which left out a train that had already left the
  station searched from, even with `--after`. On 18 Nov 2026 at about 06:30, `search Barcelona Madrid
  --after 05:00` started at 06:35, without 03052, which had left Barcelona-Sants (its first station) at
  05:40 and was on the map 7 minutes late. From a station further along its route, without `--after`, it
  was there: `search Zaragoza Madrid` listed it leaving at 07:09, with `delay_minutes: 7`.
- **From 25 Jan 2027** `--after HH:MM` shows the trains Renfe's new system lists for that time; whether it
  lists trains that have already left is unverified.

**`renfe watch TRAIN`** checks the map every 2 minutes and prints one line of JSON, shaped like `status`'s
output, on the first check and whenever the train's delay, its next station, the expected arrival there,
`overdue` or `on_map` changes, by any amount. Renfe moves the expected arrival between delay updates, so it
is the finer signal: on 18 Nov from 06:29 to 06:59 it moved 264 times with the delay and next station
unchanged, mostly by a minute either way, and a train on the map throughout gave about 10 lines in those
30 minutes (at most 15). It keeps checking a train that isn't on the map yet, so you can start it before
departure. Two other lines carry an `event` instead: `map_unreachable` (with the `error`) when the map stops
answering, once per outage, after which the next answer prints a line whether or not anything changed; and
`ended` when the watch stops on its own. Pipe it into whatever pings you; the CLI sends no notifications itself:

```sh
uv run renfe watch 03081 | while read -r line; do
  case "$line" in
    *'"event": "map_unreachable"'*) msg="Renfe's live map isn't answering: delay unknown for now" ;;
    *'"event": "ended"'*) msg="renfe watch has stopped" ;;
    *) msg="$line" ;;
  esac
  curl -s -d "$msg" https://ntfy.sh/<your-topic> >/dev/null
done
```

- It ends on its own after 12 hours (the map doesn't show where a trip ends; for a longer one, run it
  again), or on Ctrl-C, both with exit 0. If what reads its output goes away, it ends with exit 1.
- A map that doesn't answer doesn't end it. A block or rate limit (403/429, a bot page) stops it with exit 5,
  and 3 answers in a row that aren't the map (it moved or changed) with exit 7.
- A train held somewhere keeps its delay, its next station and the expected arrival there, which slips into
  the past. `watch` prints one line when it becomes overdue there, and another when it moves on or Renfe
  updates it, not one per check.

## Output and errors

stdout is one JSON document, except for `watch`, which prints one per line (JSON Lines, including its outage
lines). Errors go to stderr as `{"error": {...}}`, with these exit codes:

| Code | Meaning |
|---|---|
| `0` | ok; check `notice` |
| `1` | internal error (only the exception type is shown) |
| `2` | usage: bad arguments, unknown station, bad date, or a request Renfe rejected |
| `3` | auth: Renfe refused the anonymous session |
| `5` | blocked or upstream: 403/429, a bot page, timeout or 5xx, including Renfe's live map for `status` (for `watch`, only a block or rate limit). If this happens while fetching fares, stdout still has the timetable (plus `fares_error`). On the legacy shop it falls back instead (exit 0, `notice`) |
| `7` | API changed |

## Develop

```sh
uv sync
uv run pytest                      # offline: HTTP is mocked
RENFE_LIVE=1 uv run pytest -m live # about 10 real requests, plus one to the live map
uv run python scripts/update_stations.py  # refresh the shipped station list; prints what changed
```

A scheduled workflow (`.github/workflows/stations-reminder.yml`) opens an issue to refresh the station
list on 1 February and 1 August. GitHub turns schedules off in a public repo after 60 days without
activity; re-enable it from the Actions tab, or run it from there by hand.

Personal-data guard: `.githooks/` checks staged changes and commit messages for passengers' and
buyers' data (names, DNI/NIE, emails, phones, booking locators, card numbers) and for capture files.
Enable it once per clone with `python3 .githooks/pii_check.py enable`.
