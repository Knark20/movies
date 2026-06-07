# Cinema Movie Aggregator

Personal tool that scrapes currently showing films from 5 cinemas in Haarlem/Amsterdam, filters by rating, and outputs a dark-themed HTML page with showtimes.

**GitHub:** https://github.com/Knark20/movies

## Running

Double-click `run.bat` — it sets the API key, runs the script, and opens `movies.html`.

Or manually:
```
set OMDB_API_KEY=<your-key>
python fetch_movies.py
```

## Setup (first time)

```
pip install -r requirements.txt
```

Free OMDb API key: https://www.omdbapi.com/apikey.aspx

## Cinemas

| Cinema | URL | Notes |
|---|---|---|
| Filmkoepel Haarlem | filmkoepel.nl | Via Cineville API — see below |
| Filmschuur Haarlem | schuur.nl/agenda/ | DFS state machine over `h3` date → `span` time → `h4` title |
| Eye Filmmuseum Amsterdam | eyefilm.nl/en/whats-on | GraphQL API at `service.eyefilm.nl/graphql` — returns all individual screenings with dates/times; filters to `productionType="1"` (films only, excludes events/talks/closures) |
| Filmhallen Amsterdam | filmhallen.nl | Via Cineville API — see below |
| Lab111 Amsterdam | lab111.nl/programma/ | Schedule embedded in page HTML; each film block has `h2.hidemobile` title, `/movie/` page link, and ticket anchors with datetime text "do 18 jun 20:30" |

## Cineville API (Filmhallen + Filmkoepel)

Filmhallen and Filmkoepel are both Cineville partners. Their own websites time out from the corporate network, so both scrapers use `api.cineville.nl` instead.

**Endpoint:**
```
GET https://api.cineville.nl/events
  ?venueId[eq]={venue_id}
  &startDate[gte]={now_utc}
  &startDate[lte]={now+7d_utc}
  &page[limit]=200
```

Returns all screenings at the venue in the next 7 days. Each event has `productionId` and `startDate` (UTC). For each unique production, `GET /productions/{id}` provides the clean title and slug used to build the film link (`filmhallen.nl/films/{slug}/`).

**Venue IDs:**
- Filmhallen: `500f04ec-a10e-4f92-a8e6-d7f98b3b2d51`
- Filmkoepel: `f030b2cb-60d6-45ae-b1e0-719ed1c104f1`

`startDate` values from the API are UTC; converted to Amsterdam local time (CEST = UTC+2 in Apr–Oct, CET = UTC+1 otherwise).

## Filter logic

- **Both IMDb and RT present:** include only if IMDb ≥ 7.0 AND RT ≥ 70%
- **Only one rating present:** include if that rating passes its threshold
- **Not in OMDb or no ratings at all:** always include (goes in Misc section)
- Films not found in OMDb appear in a collapsible **Misc** section; films below threshold are hidden entirely

## OMDb rating lookup — fallback chain

`get_ratings()` tries three levels before giving up:
1. Exact title via `t=`
2. Diacritics stripped (e.g. `César` → `Cesar`) via `t=`
3. Fuzzy search via `s=`, also retried with 2-letter particles removed (handles `et` vs `&`)

Ratings are cached in `ratings_cache.json` (gitignored). To force a re-fetch, delete the file or remove specific entries.

## SSL

`SESSION.verify = False` throughout — the network uses a corporate proxy with a custom root CA. `urllib3` warnings are suppressed. Playwright uses `ignore_https_errors=True`.

## Key constants (top of fetch_movies.py)

- `IMDB_MIN = 7.0` — IMDb threshold
- `RT_MIN = 70` — Rotten Tomatoes threshold
- `_EYE_GRAPHQL_URL` / `_EYE_GRAPHQL_QUERY` — GraphQL endpoint and query for Eye screenings
- `_CINEVILLE_API` / `_FILMHALLEN_VENUE_ID` / `_FILMKOEPEL_VENUE_ID` — Cineville API base and venue IDs

## Output files (all gitignored)

- `movies.html` — generated HTML, open in any browser
- `ratings_cache.json` — OMDb response cache, keyed by `"title|||year"`
