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
playwright install chromium   # needed for Pathé Tuschinski
```

Free OMDb API key: https://www.omdbapi.com/apikey.aspx

## Cinemas

| Cinema | URL | Notes |
|---|---|---|
| Filmkoepel Haarlem | filmkoepel.nl | Showtimes from JSON-LD `ScreeningEvent` on each film page |
| Filmschuur Haarlem | schuur.nl/agenda/ | DFS state machine over `h3` date → `span` time → `h4` title |
| Pathé Tuschinski Amsterdam | pathe.nl | Blocks plain HTTP (403) — requires Playwright; site sometimes shows maintenance page |
| Eye Filmmuseum Amsterdam | eyefilm.nl/en/whats-on | GraphQL API at `service.eyefilm.nl/graphql` — returns all individual screenings with dates/times; filters to `productionType="1"` (films only, excludes events/talks/closures) |
| Filmhallen Amsterdam | filmhallen.nl | Film sitemap at `/fk-feed/film-sitemap-xml` identifies recently-updated films; each film page has ScreeningEvent JSON-LD; 30-day lookback window |

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

## Output files (all gitignored)

- `movies.html` — generated HTML, open in any browser
- `ratings_cache.json` — OMDb response cache, keyed by `"title|||year"`
