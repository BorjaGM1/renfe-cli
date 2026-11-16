# renfe-cli

Search Renfe trains and fares from the terminal. It talks directly to Renfe's own web API, the same
anonymous way the website does before anyone signs in. No account, no browser to search, and JSON
output. To buy, it opens Renfe's website on your search.

> Unofficial. Renfe's API is undocumented and can change without notice. The CLI sends what the
> website sends, no more: one timetable call, then fares 3 trains per call with at most 3 calls in
> flight. If Renfe blocks or rate-limits it, it stops and says so (exit 5). The only retry is one
> resend when Renfe rejects a saved session with 401 (see below).

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
  with a change gets one name per leg, as on the website (`INTERCITY + INTERCITY`);
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
- Before 25 Jan 2027 `--train` can only confirm trains the CLI can see (its list is incomplete then). A
  train it can't see still opens the page, with a `notice`.

Dates are YYYY-MM-DD, `today` or `tomorrow`, up to a year ahead. `search` returns at most 10 trains by default
(`--limit`, up to 200). Fares cost one extra request per 3
trains. For today, trains that have already left are hidden unless you pass `--after`.

Speed (measured 2026-11-15): a search takes about 0.3–0.5 s, and a full day of 22 trains with fares
about 1.4 s. The anonymous session (a 30-minute token and the site's public API key) is kept in the
cache folder as `session.json`, so a timetable search usually sends a single request; fares add one per
3 trains. If Renfe answers 401 to a saved session, the CLI opens a new one and resends that request
once. A 403 is reported as a block, and the saved session is dropped so the next run starts afresh.

**Fares exist only from 25 Jan 2027.** Renfe is moving to a new sales system. Until then it sells
earlier trips on its legacy shop (venta.renfe.com), which this CLI can't search; `shop.url` and
`renfe open` take you to the full list on Renfe's website. For those dates
you get no fares and a `notice`, and the list of trains is **incomplete**: the new system knows only
some of them. For Madrid–Barcelona on Friday 20 Nov 2026 it had 8 of the 24 trains the legacy shop
sells. From 25 Jan 2027 the CLI's list matches the website's exactly, trains and cheapest fares
(checked against the web for Friday 29 Jan: 22 trains each).

Stations are matched by name, ignoring accents and case, or by code. A city name such as `Madrid` means
all of that city's stations (`MADRI`). The station list is cached for a day in the cache folder:
`~/.cache/renfe-cli`, or `$XDG_CACHE_HOME/renfe-cli`, or `$RENFE_CACHE_DIR` if set.

## Output and errors

stdout is JSON. Errors go to stderr as `{"error": {...}}`, with these exit codes:

| Code | Meaning |
|---|---|
| `0` | ok; check `notice` |
| `1` | internal error (only the exception type is shown) |
| `2` | usage: bad arguments, unknown station, bad date, or a request Renfe rejected |
| `3` | auth: Renfe refused the anonymous session |
| `5` | blocked or upstream: 403/429, a bot page, timeout or 5xx. If this happens while fetching fares, stdout still has the timetable (plus `fares_error`) |
| `7` | API changed |

## Develop

```sh
uv sync
uv run pytest                      # offline: HTTP is mocked
RENFE_LIVE=1 uv run pytest -m live # about 5 real requests
```

Personal-data guard: `.githooks/` checks staged changes and commit messages for passengers' and
buyers' data (names, DNI/NIE, emails, phones, booking locators, card numbers) and for capture files.
Enable it once per clone with `python3 .githooks/pii_check.py enable`.
