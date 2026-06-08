#!/usr/bin/env python3
"""
Cinema movie aggregator: collects currently showing films from 4 cinemas
and filters by IMDb/RT rating using the OMDb API.

Setup:
  pip install -r requirements.txt
  playwright install chromium        # needed for Pathé Tuschinski only

  Windows:     set OMDB_API_KEY=<your-key>
  Linux/macOS: export OMDB_API_KEY=<your-key>

  Free OMDb API key: https://www.omdbapi.com/apikey.aspx

Usage:
  python fetch_movies.py    →  generates movies.html, open in browser
"""

from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

# Force UTF-8 output on Windows so non-ASCII film titles don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import re
import urllib3
import requests
from bs4 import BeautifulSoup

# Disable SSL verification warnings — corporate proxy uses its own root CA
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Configuration ──────────────────────────────────────────────────────────────
OMDB_KEY    = os.environ.get("OMDB_API_KEY", "")
TMDB_TOKEN  = os.environ.get("TMDB_TOKEN", "")
IMDB_MIN   = 7.0
RT_MIN     = 70
BASE_DIR   = Path(__file__).parent
CACHE_FILE = BASE_DIR / "ratings_cache.json"
OUTPUT     = BASE_DIR / "movies.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# Shared session with SSL verification off (needed on networks with proxy root CA)
SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update(HEADERS)


# ── Rating cache ────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8-sig"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalize_title(title: str) -> str:
    """Strip diacritics: 'César et Rosalie' → 'Cesar et Rosalie'."""
    return unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").strip()


_QUALIFIER_RE = re.compile(
    r'\s*\([^)]*(?:restoration|remaster(?:ed)?|anniversary|sing.along|re-release|incl\.|'
    r'(?:director|extended|special|theatrical|final)[^)]*(?:cut|edition|version))[^)]*\)\s*$',
    re.I,
)

def _strip_qualifiers(title: str) -> str:
    return _QUALIFIER_RE.sub('', title).strip() or title


_NOT_FOUND: dict = {"found": False, "imdb": None, "rt": None}

_ENGLISH_COUNTRIES = {"usa", "uk", "united states", "united kingdom", "australia", "canada", "ireland", "new zealand"}
_CJK_RE = re.compile(r'[⺀-鿿豈-﫿가-힣؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]')

def _is_english_only(country: str) -> bool:
    if not country:
        return True
    return {c.strip().lower() for c in country.split(",")}.issubset(_ENGLISH_COUNTRIES)


def _omdb_parse(data: dict, fallback_title: str) -> dict:
    imdb_raw = data.get("imdbRating")
    rt_raw = next(
        (x["Value"] for x in data.get("Ratings", []) if x["Source"] == "Rotten Tomatoes"),
        None,
    )
    return {
        "found":   True,
        "title":   data.get("Title", fallback_title),
        "year":    data.get("Year"),
        "imdb":    float(imdb_raw) if imdb_raw not in (None, "N/A") else None,
        "rt":      int(rt_raw.rstrip("%")) if rt_raw else None,
        "runtime": int(m.group(1)) if (m := re.match(r"(\d+)", data.get("Runtime") or "")) else None,
        "poster":  data.get("Poster") if data.get("Poster") != "N/A" else None,
        "plot":    data.get("Plot"),
        "imdb_id": data.get("imdbID"),
        "country": data.get("Country"),
    }


def _omdb_fetch(search_title: str, year: Optional[str] = None) -> dict:
    """Direct title lookup via OMDb t= parameter."""
    params: dict = {"t": search_title, "apikey": OMDB_KEY, "type": "movie"}
    if year:
        params["y"] = year
    try:
        data = SESSION.get("https://www.omdbapi.com/", params=params, timeout=10).json()
        if data.get("Response") == "True":
            return _omdb_parse(data, search_title)
    except Exception:
        pass
    finally:
        time.sleep(0.12)
    return _NOT_FOUND


def _omdb_search(search_title: str) -> dict:
    """Search OMDb, trying progressively simplified queries when needed.
    Handles variants like 'César et Rosalie' → 'Cesar & Rosalie' by stripping
    2-letter particles ('et', 'en', 'de', …) that differ between title variants."""
    queries = [search_title]
    without_particles = " ".join(w for w in search_title.split() if len(w) != 2)
    if without_particles and without_particles != search_title:
        queries.append(without_particles)

    for query in queries:
        try:
            data = SESSION.get("https://www.omdbapi.com/",
                               params={"s": query, "apikey": OMDB_KEY, "type": "movie"},
                               timeout=10).json()
            time.sleep(0.12)
            if data.get("Response") != "True" or not data.get("Search"):
                continue
            best = max(data["Search"],
                       key=lambda r: (
                           SequenceMatcher(None, search_title.lower(), r["Title"].lower()).ratio(),
                           int(r.get("Year", "0")[:4] or "0"),
                       ))
            if SequenceMatcher(None, search_title.lower(), best["Title"].lower()).ratio() < 0.6:
                continue
            data2 = SESSION.get("https://www.omdbapi.com/",
                                params={"i": best["imdbID"], "apikey": OMDB_KEY},
                                timeout=10).json()
            time.sleep(0.12)
            if data2.get("Response") == "True":
                return _omdb_parse(data2, search_title)
        except Exception:
            time.sleep(0.12)
    return _NOT_FOUND


def _tmdb_fetch(title: str, year: Optional[str] = None) -> dict:
    """Search TMDb and return poster URL, rating, and vote count for the top result."""
    if not TMDB_TOKEN:
        return {}
    try:
        params: dict = {"query": title, "language": "en-US", "page": 1}
        if year:
            params["year"] = year
        resp = SESSION.get(
            "https://api.themoviedb.org/3/search/movie",
            params=params,
            headers={"Authorization": f"Bearer {TMDB_TOKEN}"},
            timeout=10,
        )
        results = resp.json().get("results", [])
        if not results:
            return {}
        top = results[0]
        out: dict = {}
        if top.get("poster_path"):
            out["poster"] = f"https://image.tmdb.org/t/p/w300{top['poster_path']}"
        if top.get("title"):
            out["title"] = top["title"]
        if top.get("original_title") and top.get("original_title") != top.get("title"):
            out["original_title"] = top["original_title"]
        if top.get("release_date"):
            out["year"] = top["release_date"][:4]
        if top.get("overview"):
            out["plot"] = top["overview"]
        vote_avg   = top.get("vote_average")
        vote_count = top.get("vote_count", 0)
        if vote_avg and vote_count >= 10:
            out["tmdb_rating"] = round(float(vote_avg), 1)
        # Fetch IMDb ID when well-known (50+ votes) OR title is a close match
        title_sim = SequenceMatcher(None, title.lower(), (top.get("title") or "").lower()).ratio()
        tmdb_id = top.get("id") if (vote_count >= 50 or title_sim >= 0.8) else None
        if tmdb_id:
            ext = SESSION.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids",
                headers={"Authorization": f"Bearer {TMDB_TOKEN}"},
                timeout=10,
            ).json()
            if ext.get("imdb_id"):
                out["imdb_id"] = ext["imdb_id"]
        return out
    except Exception:
        return {}


def _rt_fetch(title: str, year: Optional[str] = None) -> dict:
    """Search RT and return Tomatometer score for the best-matching film.
    Uses the search page which embeds tomatometer-score in the result element."""
    try:
        resp = SESSION.get(
            "https://www.rottentomatoes.com/search",
            params={"search": title},
            timeout=10,
        )
        soup = BeautifulSoup(resp.content, "html.parser")
        rows = soup.find_all("search-page-media-row")
        if not rows:
            return {}
        best_row = None
        best_sim = 0.0
        best_year_match = False
        for row in rows:
            score_str = row.attrs.get("tomatometer-score", "")
            if not score_str:
                continue
            title_link = row.find("a", {"data-qa": "info-name"})
            row_title = title_link.get_text(strip=True) if title_link else ""
            if not row_title:
                img = row.find("img", alt=True)
                row_title = img["alt"] if img else ""
            sim = SequenceMatcher(None, title.lower(), row_title.lower()).ratio()
            if sim < 0.8:
                continue
            row_year = row.attrs.get("release-year", "")
            year_match = bool(year and row_year and row_year[:4] == str(year)[:4])
            if year_match and not best_year_match:
                best_row, best_sim, best_year_match = row, sim, True
            elif year_match == best_year_match and sim > best_sim:
                best_row, best_sim = row, sim
        if best_row is None:
            return {}
        url_link = best_row.find("a", {"data-qa": "thumbnail-link"})
        return {
            "rt": int(best_row.attrs["tomatometer-score"]),
            "rt_url": url_link["href"] if url_link else "",
        }
    except Exception:
        return {}


def get_ratings(title: str, year: Optional[str] = None, cache: Optional[dict] = None) -> dict:
    """Look up IMDb + RT ratings from OMDb with three fallback levels:
    1. exact title  2. diacritics stripped  3. fuzzy search (handles 'et' vs '&' etc.)"""
    key = f"{title}|||{year or ''}"
    if cache is not None and key in cache:
        cached = cache[key]
        # Re-fetch if found but missing country (needed for dual-title display)
        if not cached.get("found") or "country" in cached:
            return cached

    if not OMDB_KEY:
        return {"found": False, "imdb": None, "rt": None}

    lookup = _strip_qualifiers(title)

    result = _omdb_fetch(lookup, year)
    if not result["found"]:
        normalized = _normalize_title(lookup)
        if normalized != lookup:
            result = _omdb_fetch(normalized, year)
    if not result["found"] or (result["found"] and result.get("imdb") is None and result.get("rt") is None):
        search = _omdb_search(_normalize_title(lookup))
        if search["found"] and (search.get("imdb") is not None or search.get("rt") is not None):
            result = search
        elif not result["found"]:
            result = search

    # TMDb fallback — when OMDb has nothing, use TMDb rating + metadata as primary source
    tmdb_already_fetched: Optional[dict] = None
    if not result["found"] and TMDB_TOKEN:
        tmdb_already_fetched = _tmdb_fetch(lookup, year)
        if tmdb_already_fetched.get("tmdb_rating") is not None:
            result = {
                "found":       True,
                "title":       tmdb_already_fetched.get("title") or lookup,
                "year":        tmdb_already_fetched.get("year") or year,
                "imdb":        None,
                "rt":          None,
                "tmdb_rating": tmdb_already_fetched["tmdb_rating"],
                "poster":      tmdb_already_fetched.get("poster"),
                "plot":        tmdb_already_fetched.get("plot"),
                "imdb_id":     tmdb_already_fetched.get("imdb_id"),
            }
            rt = _rt_fetch(result["title"], result.get("year"))
            if rt.get("rt") is not None:
                result["rt"] = rt["rt"]

    if result.get("found"):
        tmdb = tmdb_already_fetched if tmdb_already_fetched is not None else _tmdb_fetch(lookup, year)
        if tmdb.get("poster"):
            result["poster"] = tmdb.get("poster")
        elif not result.get("poster"):
            result["poster"] = tmdb.get("poster")
        # If no IMDb rating yet but TMDb has the IMDb ID, re-fetch from OMDb by that ID
        if result.get("imdb") is None and OMDB_KEY and tmdb.get("imdb_id"):
            data = SESSION.get("https://www.omdbapi.com/",
                               params={"i": tmdb["imdb_id"], "apikey": OMDB_KEY}, timeout=10).json()
            time.sleep(0.12)
            if data.get("Response") == "True":
                result = _omdb_parse(data, title)
                if not result.get("poster"):
                    result["poster"] = tmdb.get("poster")
        if result.get("imdb") is None and "tmdb_rating" in tmdb:
            result["tmdb_rating"] = tmdb["tmdb_rating"]
        if tmdb.get("original_title") and not result.get("original_title"):
            result["original_title"] = tmdb["original_title"]
        # If OMDb has no RT score, try fetching it directly from RT
        if result.get("rt") is None:
            rt = _rt_fetch(result.get("title") or lookup, result.get("year"))
            if rt.get("rt") is not None:
                result["rt"] = rt["rt"]

    if cache is not None:
        cache[key] = result
    return result


# ── Cinema scrapers ─────────────────────────────────────────────────────────────

_NL_DAY_TO_EN = {
    "zo": "Sun", "ma": "Mon", "di": "Tue", "wo": "Wed",
    "do": "Thu", "vr": "Fri", "za": "Sat",
}
_MON_NL_MAP = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}

def _parse_nl_date(date_str: str) -> tuple[str, str]:
    """'Zo 7 jun' → ('Sun 7 Jun', '2026-06-07') — Dutch cinema date to English + ISO sort key."""
    parts = date_str.lower().split()
    if len(parts) != 3:
        return date_str, ""
    day_nl, day_num, mon_nl = parts
    day_en = _NL_DAY_TO_EN.get(day_nl, day_nl.capitalize())
    mon_num = _MON_NL_MAP.get(mon_nl, 0)
    today = datetime.now()
    year = today.year if mon_num >= today.month - 1 else today.year + 1
    try:
        sort_date = f"{year}-{mon_num:02d}-{int(day_num):02d}"
        mon_en = datetime(year, mon_num, int(day_num)).strftime("%b")
    except ValueError:
        return date_str, ""
    return f"{day_en} {day_num} {mon_en}", sort_date

_CINEVILLE_API        = "https://api.cineville.nl"
_FILMHALLEN_VENUE_ID  = "500f04ec-a10e-4f92-a8e6-d7f98b3b2d51"
_FILMKOEPEL_VENUE_ID  = "f030b2cb-60d6-45ae-b1e0-719ed1c104f1"


def _scrape_cineville_venue(venue_id: str, films_base_url: str) -> list[dict]:
    """Fetch upcoming films at a Cineville partner venue via api.cineville.nl."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    params = {
        "venueId[eq]": venue_id,
        "startDate[gte]": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startDate[lte]": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page[limit]": 200,
    }
    resp = SESSION.get(f"{_CINEVILLE_API}/events", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    all_events: list[dict] = list(data["_embedded"]["events"])

    # Paginate if needed
    while data.get("_links", {}).get("next"):
        m = re.search(r'page\[after\]=([^&]+)', data["_links"]["next"]["href"])
        if not m:
            break
        resp = SESSION.get(f"{_CINEVILLE_API}/events", params={**params, "page[after]": m.group(1)}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        all_events.extend(data["_embedded"]["events"])

    # Group events by production, skip hidden and non-film
    prod_groups: dict[str, list[dict]] = {}
    for ev in all_events:
        if ev.get("productionTypeId") != "film" or ev.get("isHidden"):
            continue
        prod_groups.setdefault(ev["productionId"], []).append(ev)

    films: list[dict] = []
    for prod_id, evs in prod_groups.items():
        try:
            pr = SESSION.get(f"{_CINEVILLE_API}/productions/{prod_id}", timeout=10)
            if pr.status_code != 200:
                continue
            prod = pr.json()
        except Exception:
            continue
        title = prod.get("title", "").strip()
        slug  = prod.get("slug",  "").strip()
        if not title:
            continue
        link = f"{films_base_url}/{slug}/" if slug else films_base_url
        showtimes: list[dict] = []
        for ev in evs:
            start = ev.get("startDate", "")
            if not start:
                continue
            try:
                dt_utc = datetime.strptime(start[:16], "%Y-%m-%dT%H:%M")
                offset = 2 if 4 <= dt_utc.month <= 10 else 1  # CEST/CET
                dt = dt_utc + timedelta(hours=offset)
                showtimes.append({
                    "date":      f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}",
                    "time":      dt.strftime("%H:%M"),
                    "sort_date": dt.strftime("%Y-%m-%d"),
                })
            except ValueError:
                continue
        if showtimes:
            films.append({"title": title, "link": link, "showtimes": showtimes})
    return films


def scrape_filmkoepel() -> list[dict]:
    """Filmkoepel Haarlem — via Cineville API (api.cineville.nl)."""
    return _scrape_cineville_venue(_FILMKOEPEL_VENUE_ID, "https://filmkoepel.nl/films")


def scrape_schuur() -> list[dict]:
    """
    Filmschuur Haarlem — schedule: schuur.nl/agenda/
    Page structure: <h3> date → <span> time → <h4><a /film/slug> title.
    State machine walks DFS element order to associate each screening with its date+time.
    """
    resp = SESSION.get("https://www.schuur.nl/agenda/", timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
    DATE_RE = re.compile(r"^\w{2}\s+\d{1,2}\s+\w{3}$", re.IGNORECASE)

    films_map: dict[str, dict] = {}  # title → {link, showtimes}
    current_date = ""
    current_time = ""

    for el in soup.find_all(True):
        if el.name == "h3":
            text = el.get_text(strip=True)
            if DATE_RE.match(text):
                current_date = text

        elif el.name == "span":
            text = el.get_text(strip=True)
            if TIME_RE.match(text):
                current_time = text

        elif el.name == "h4":
            a = el.find("a", href=True)
            if not a or "/film/" not in a["href"]:
                continue
            title = re.sub(r"\s*\d+\+\s*$", "", a.get_text(strip=True)).strip()
            if not title or title.startswith("VR "):
                continue
            href = a["href"]
            link = href if href.startswith("http") else f"https://www.schuur.nl{href}"
            if title not in films_map:
                films_map[title] = {"link": link, "showtimes": []}
            if current_date or current_time:
                en_date, sort_date = _parse_nl_date(current_date) if current_date else ("", "")
                films_map[title]["showtimes"].append({
                    "date": en_date,
                    "time": current_time,
                    "sort_date": sort_date,
                })

    return [{"title": t, "link": d["link"], "showtimes": d["showtimes"]}
            for t, d in films_map.items()]



_EYE_GRAPHQL_URL = "https://service.eyefilm.nl/graphql"
_EYE_GRAPHQL_QUERY = (
    "query shows($site:String!,$startDateTime:String,$limit:Int,$sort:ShowSortEnum)"
    "{items:show(site:$site,startDateTime:$startDateTime,limit:$limit,sort:$sort)"
    "{startDateTime production{url title} relatedProduction{productionType}}}"
)

def scrape_eye() -> list[dict]:
    """
    Eye Filmmuseum Amsterdam — all screenings via the GraphQL API used by eyefilm.nl/en/whats-on.
    Returns individual films with full showtime data; covers all programmes including Eye Classics.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    resp = SESSION.post(_EYE_GRAPHQL_URL, json={
        "query": _EYE_GRAPHQL_QUERY,
        "variables": {
            "site": "eyeEnglish",
            "startDateTime": f"> {today} 00:00",
            "limit": 500,
            "sort": "DATE",
        },
        "operationName": "shows",
    }, timeout=20)
    resp.raise_for_status()
    items = resp.json().get("data", {}).get("items", [])

    films_map: dict[str, dict] = {}
    for item in items:
        if (item.get("relatedProduction") or {}).get("productionType") != "1":
            continue  # skip events, talks, closures — only include films (type 1)
        prod = (item.get("production") or [{}])[0]
        title = prod.get("title", "").strip()
        if not title:
            continue
        url = prod.get("url", "")
        link = url if url.startswith("http") else f"https://www.eyefilm.nl{url}"
        start = item.get("startDateTime", "")
        if not start:
            continue
        dt = datetime.fromisoformat(start)
        date_label = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"
        if title not in films_map:
            films_map[title] = {"link": link, "showtimes": []}
        films_map[title]["showtimes"].append({
            "date": date_label,
            "time": dt.strftime("%H:%M"),
            "sort_date": dt.strftime("%Y-%m-%d"),
        })

    return [{"title": t, "link": d["link"], "showtimes": d["showtimes"]}
            for t, d in films_map.items()]


def scrape_lab111() -> list[dict]:
    """
    Lab111 Amsterdam — programma: lab111.nl/programma/
    Schedule is embedded in the page: each film block has an h2.hidemobile title,
    a /movie/{slug}/ page link, and ticket anchors with text "do 18 jun 20:30".
    """
    BASE = "https://www.lab111.nl"
    resp = SESSION.get(f"{BASE}/programma/", timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    _DT_RE = re.compile(r"^(\w{2})\s+(\d{1,2})\s+(\w{3})\s+(\d{2}:\d{2})$")

    films: list[dict] = []
    for block in soup.find_all("div", class_="col-md-8"):
        title_el = block.find("h2", class_="hidemobile")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue
        film_link_el = block.find("a", href=re.compile(r"/movie/"))
        link = film_link_el["href"] if film_link_el else f"{BASE}/programma/"
        showtimes: list[dict] = []
        for a in block.find_all("a", href=True):
            if "/show/" not in a["href"]:
                continue
            text = " ".join(a.get_text(strip=True).split())  # normalise whitespace
            m = _DT_RE.match(text)
            if not m:
                continue
            date_str = f"{m.group(1)} {m.group(2)} {m.group(3)}"
            time_str = m.group(4)
            en_date, sort_date = _parse_nl_date(date_str)
            if not sort_date:
                continue
            showtimes.append({"date": en_date, "time": time_str, "sort_date": sort_date})
        if showtimes:
            films.append({"title": title, "link": link, "showtimes": showtimes})
    return films


def scrape_filmhallen() -> list[dict]:
    """Filmhallen Amsterdam — via Cineville API (api.cineville.nl)."""
    return _scrape_cineville_venue(_FILMHALLEN_VENUE_ID, "https://filmhallen.nl/films")


# ── Filter ─────────────────────────────────────────────────────────────────────

def passes_filter(r: dict) -> bool:
    if not r.get("found"):
        return True
    runtime = r.get("runtime")
    if runtime is not None and runtime < 30:
        return False
    imdb = r.get("imdb") if r.get("imdb") is not None else r.get("tmdb_rating")
    rt   = r.get("rt")
    if imdb is None and rt is None:
        return True
    if imdb is not None and rt is not None:
        return imdb >= IMDB_MIN and rt >= RT_MIN
    return (imdb is not None and imdb >= IMDB_MIN) or (rt is not None and rt >= RT_MIN)


# ── HTML generation ─────────────────────────────────────────────────────────────

def _showtimes_html(showtimes: list[dict], links: dict = {}) -> str:
    if not showtimes:
        return ""
    groups: dict[tuple, list[str]] = {}
    for st in showtimes:
        key = (st.get("sort_date", ""), st.get("date", ""), st.get("cinema", ""))
        groups.setdefault(key, []).append(st["time"])
    rows = []
    for (sort_date, date, cinema), times in sorted(groups.items(), key=lambda kv: (kv[0][0], sorted(set(kv[1]))[0])):
        times = sorted(set(times))[-4:]  # cap at 4, keeping the latest
        times_str = " · ".join(times)
        date_html = f'<span class="st-date">{date}</span>' if date else ""
        if cinema:
            href = links.get(cinema, "#")
            cinema_html = f'<a class="ctag" href="{href}" target="_blank">{cinema}</a>'
        else:
            cinema_html = ""
        cinema_attr = f' data-cinema="{cinema}"' if cinema else ""
        rows.append(f'<div class="st-row"{cinema_attr}>{date_html}<span class="st-times">{times_str}</span>{cinema_html}</div>')
    return f'<div class="showtimes">{"".join(rows)}</div>'


def _badge(label: str, value, threshold) -> str:
    if value is None:
        return f'<span class="badge gray" style="visibility:hidden">{label} —</span>'
    cls = "green" if value >= threshold else "red"
    disp = f"{value:.1f}" if isinstance(value, float) else f"{value}%"
    return f'<span class="badge {cls}">{label} {disp}</span>'


def _card(title: str, r: dict, links: dict, showtimes: list[dict] = None) -> str:
    poster_html = f'<img src="{r["poster"]}" alt="" loading="lazy">' if r.get("poster") else ""
    en_title = r.get("title", title) if r.get("found") else title
    orig_title = r.get("original_title") or ""
    show_en_sub = (
        r.get("found")
        and not _is_english_only(r.get("country", ""))
        and orig_title
        and orig_title.lower().strip() != en_title.lower().strip()
        and not _CJK_RE.search(orig_title)
    )
    main_title = orig_title if show_en_sub else en_title
    if r.get("imdb_id"):
        title_html = f'<a href="https://www.imdb.com/title/{r["imdb_id"]}/" target="_blank">{main_title}</a>'
    else:
        title_html = main_title
    year_html  = f' <span class="year">({r["year"]})</span>' if r.get("year") else ""
    en_sub_html = f'<span class="en-title">{en_title}</span>' if show_en_sub else ""
    plot_html  = f'<p class="plot">{r["plot"]}</p>' if r.get("plot") else ""
    if r.get("found"):
        imdb  = r.get("imdb")
        tmdb  = r.get("tmdb_rating")
        rt    = r.get("rt")
        if imdb is not None:
            badges_html = _badge("IMDb", imdb, IMDB_MIN) + _badge("RT", rt, RT_MIN)
        elif rt is not None:
            badges_html = _badge("RT", rt, RT_MIN) + _badge("TMDb", tmdb, IMDB_MIN)
        elif tmdb is not None:
            badges_html = _badge("TMDb", tmdb, IMDB_MIN) + _badge("RT", None, RT_MIN)
        else:
            badges_html = _badge("IMDb", None, IMDB_MIN) + _badge("RT", None, RT_MIN)
    else:
        badges_html = '<span class="badge gray">not in OMDb</span>'
    st_html = _showtimes_html(showtimes or [], links)
    return f"""<div class="card">
  <div class="thumb">{poster_html}</div>
  <div class="body">
    <h3>{title_html}{year_html}{en_sub_html}</h3>
    <div class="badges">{badges_html}</div>
    {plot_html}
    {st_html}
  </div>
</div>"""


def generate_html(movies_by_cinema: dict) -> str:
    # Merge films that play in multiple cinemas into one entry
    merged: dict[str, dict] = {}  # keyed by title.lower() for case-insensitive dedup
    titles: dict[str, str] = {}   # key → first-seen display title
    for cinema, films in movies_by_cinema.items():
        short = CINEMA_SHORT.get(cinema, cinema)
        for f in films:
            t = f["title"]
            key = _strip_qualifiers(t).lower()
            if key not in merged:
                merged[key] = {"r": f["ratings"], "cinemas": [], "links": {}, "showtimes": []}
                titles[key] = _strip_qualifiers(t)
            if short not in merged[key]["cinemas"]:
                merged[key]["cinemas"].append(short)
            merged[key]["links"][short] = f["link"]
            for st in f.get("showtimes", []):
                merged[key]["showtimes"].append({**st, "cinema": short})

    # Second pass: merge entries that resolved to the same IMDb ID under different titles
    imdb_to_key: dict[str, str] = {}
    for key, d in list(merged.items()):
        imdb_id = d["r"].get("imdb_id")
        if not imdb_id:
            continue
        if imdb_id in imdb_to_key:
            primary = imdb_to_key[imdb_id]
            for c in d["cinemas"]:
                if c not in merged[primary]["cinemas"]:
                    merged[primary]["cinemas"].append(c)
            merged[primary]["links"].update(d["links"])
            merged[primary]["showtimes"].extend(d["showtimes"])
            del merged[key]
            del titles[key]
        else:
            imdb_to_key[imdb_id] = key

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    cutoff_str = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT23:59")
    for d in merged.values():
        d["showtimes"] = [
            st for st in d["showtimes"]
            if now_str <= f"{st.get('sort_date', '')}T{st.get('time', '')}" <= cutoff_str
        ]

    EXCLUDED: set[str] = set()
    merged = {k: d for k, d in merged.items() if k not in EXCLUDED and d["showtimes"]}

    good = [(titles[k], d) for k, d in merged.items() if d["r"].get("found") and d["r"].get("poster") and passes_filter(d["r"])]
    misc = [(titles[k], d) for k, d in merged.items() if not d["r"].get("found") or not d["r"].get("poster")]

    def earliest_showtime(item: tuple) -> str:
        candidates = [
            f"{st['sort_date']}T{st['time']}"
            for st in item[1].get("showtimes", [])
            if st.get("sort_date") and st.get("time")
        ]
        return min(candidates) if candidates else "9999-99-99T99:99"

    good.sort(key=earliest_showtime)
    misc.sort(key=earliest_showtime)

    def cards_html(lst: list) -> str:
        return "\n".join(_card(t, d["r"], d["links"], d.get("showtimes", [])) for t, d in lst)

    misc_section = f"""
<details>
  <summary>Misc ({len(misc)} film{"s" if len(misc) != 1 else ""})</summary>
  <div class="grid">{cards_html(misc)}</div>
</details>""" if misc else ""

    all_cinemas = sorted({st["cinema"] for d in merged.values() for st in d["showtimes"] if st.get("cinema")})
    filter_btns = "".join(f'<button class="cf-btn active" data-cinema="{c}">{c}</button>' for c in all_cinemas)
    filter_html = f'<div class="cinema-filters">{filter_btns}</div>' if len(all_cinemas) > 1 else ""

    now   = datetime.now().strftime("%d %b %Y, %H:%M")
    total = len(merged)
    good_count = len(good)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Now Playing — {now}</title>
<style>
:root {{
  --bg: #0f172a; --surface: #1e293b; --accent: #60a5fa;
  --green: #4ade80; --red: #f87171; --gray: #64748b;
  --text: #f1f5f9; --muted: #94a3b8;
  --green-bg: #052e16; --red-bg: #450a0a;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: system-ui, -apple-system, sans-serif;
  background: var(--bg); color: var(--text);
  padding: 2rem 1.5rem; max-width: 1280px; margin: 0 auto;
}}
header {{ margin-bottom: 2rem; border-bottom: 1px solid #1e293b; padding-bottom: 1.5rem; }}
h1 {{ font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; }}
.meta {{ color: var(--muted); font-size: 0.85rem; margin-top: 0.4rem; }}
.section-label {{
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: .1em;
  color: var(--muted); margin: 1.5rem 0 0.75rem;
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 0.85rem;
}}
.card {{
  background: var(--surface); border-radius: 10px;
  display: flex; overflow: hidden;
  transition: box-shadow 0.15s, transform 0.15s;
}}
.card:hover {{
  box-shadow: 0 0 0 1px var(--accent);
  transform: translateY(-1px);
}}
.thumb {{ width: 80px; min-width: 80px; background: #111827; }}
.thumb img {{ width: 80px; height: 120px; object-fit: cover; display: block; }}
.body {{
  padding: 0.8rem 0.9rem;
  display: flex; flex-direction: column; gap: 0.4rem; flex: 1; min-width: 0;
}}
h3 {{ font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.en-title {{ display: block; font-size: 0.75rem; color: var(--muted); font-weight: 400; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }}
h3 a {{ color: var(--accent); text-decoration: none; }}
h3 a:hover {{ text-decoration: underline; }}
.year {{ color: var(--muted); font-weight: 400; font-size: 0.85rem; }}
.badges {{ display: flex; gap: 0.3rem; flex-wrap: wrap; }}
.badge {{
  font-size: 0.68rem; padding: 2px 8px; border-radius: 999px;
  font-weight: 700; white-space: nowrap;
}}
.badge.green {{ background: var(--green-bg); color: var(--green); }}
.badge.red   {{ background: var(--red-bg);   color: var(--red); }}
.badge.gray  {{ background: var(--surface);  color: var(--gray); border: 1px solid #334155; }}
.plot {{
  font-size: 0.78rem; color: var(--muted); line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}}
.showtimes {{ font-size: 0.72rem; display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; max-height: 7rem; overflow-y: auto; padding-right: 0.25rem; scrollbar-width: thin; scrollbar-color: #475569 transparent; }}
.showtimes::-webkit-scrollbar {{ width: 4px; }}
.showtimes::-webkit-scrollbar-track {{ background: transparent; }}
.showtimes::-webkit-scrollbar-thumb {{ background: #475569; border-radius: 2px; }}
.showtimes::-webkit-scrollbar-thumb:hover {{ background: #64748b; }}
.st-row {{ display: flex; align-items: baseline; gap: 0; }}
.st-date {{ color: var(--muted); min-width: 4.5rem; flex-shrink: 0; }}
.st-times {{ color: var(--text); letter-spacing: 0.02em; }}
.ctag {{
  font-size: 0.67rem; padding: 2px 7px; border-radius: 4px;
  background: #1e3a5f; color: #93c5fd; text-decoration: none; white-space: nowrap;
}}
.ctag:hover {{ background: #1d4ed8; color: #fff; }}
details {{ margin-top: 2.5rem; }}
summary {{
  cursor: pointer; color: var(--muted); font-size: 0.85rem;
  padding: 0.5rem 0; user-select: none; list-style: none;
}}
summary::before {{ content: "▶  "; font-size: 0.7rem; }}
details[open] summary::before {{ content: "▼  "; }}
summary:hover {{ color: var(--text); }}
details[open] > summary {{ margin-bottom: 0.75rem; }}
.empty {{ color: var(--muted); font-style: italic; font-size: 0.9rem; }}
.cinema-filters {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1.5rem; }}
.cf-btn {{
  font-size: 0.75rem; padding: 4px 12px; border-radius: 999px;
  border: 1px solid #334155; background: transparent; color: var(--muted);
  cursor: pointer; transition: background 0.15s, color 0.15s, border-color 0.15s;
}}
.cf-btn.active {{ background: #1e3a5f; color: #93c5fd; border-color: #1d4ed8; }}
.cf-btn:hover {{ border-color: var(--accent); color: var(--text); }}
</style>
</head>
<body>
<header>
  <h1>Now Playing</h1>
  <p class="meta">Generated {now}&nbsp;&nbsp;·&nbsp;&nbsp;<span id="film-count">{good_count}</span> films found</p>
</header>

{filter_html}
{"<p class='empty'>No films found.</p>" if not good else f'<div class="grid">{cards_html(good)}</div>'}

{misc_section}
<script>
(function(){{
  const btns = document.querySelectorAll('.cf-btn');
  if (!btns.length) return;
  btns.forEach(btn => btn.addEventListener('click', function(){{
    this.classList.toggle('active');
    filter();
  }}));
  function filter(){{
    const active = new Set([...btns].filter(b => b.classList.contains('active')).map(b => b.dataset.cinema));
    const all = active.size === btns.length;
    document.querySelectorAll('.st-row[data-cinema]').forEach(row => {{
      row.style.display = all || active.has(row.dataset.cinema) ? '' : 'none';
    }});
    document.querySelectorAll('.card').forEach(card => {{
      const rows = card.querySelectorAll('.st-row[data-cinema]');
      if (!rows.length) return;
      card.style.display = [...rows].some(r => r.style.display !== 'none') ? '' : 'none';
    }});
    const det = document.querySelector('details');
    if (det) {{
      const any = [...det.querySelectorAll('.card')].some(c => c.style.display !== 'none');
      det.style.display = any ? '' : 'none';
    }}
    const grid = document.querySelector('.grid');
    if (grid) {{
      const visible = [...grid.querySelectorAll('.card')].filter(c => c.style.display !== 'none').length;
      const el = document.getElementById('film-count');
      if (el) el.textContent = visible;
    }}
  }}
}})();
</script>
</body>
</html>"""


# ── Entry point ─────────────────────────────────────────────────────────────────

CINEMAS: dict = {
    "Filmkoepel Haarlem":       scrape_filmkoepel,
    "Filmschuur Haarlem":       scrape_schuur,
    "Eye Filmmuseum Amsterdam": scrape_eye,
    "Filmhallen Amsterdam":     scrape_filmhallen,
    "Lab111 Amsterdam":         scrape_lab111,
}

CINEMA_SHORT: dict[str, str] = {
    "Filmkoepel Haarlem":       "Filmkoepel",
    "Filmschuur Haarlem":       "Filmschuur",
    "Eye Filmmuseum Amsterdam": "Eye Filmmuseum",
    "Filmhallen Amsterdam":     "Filmhallen",
    "Lab111 Amsterdam":         "Lab111",
}


def main() -> None:
    if not OMDB_KEY:
        print("Warning: OMDB_API_KEY not set - films will appear without ratings.")
        print("Get a free key at https://www.omdbapi.com/apikey.aspx\n")

    cache = load_cache()
    movies_by_cinema: dict[str, list] = {}

    for name, scraper in CINEMAS.items():
        print(f">> {name} ...", end=" ", flush=True)
        try:
            films = scraper()
            print(f"{len(films)} film(s)")
        except Exception as exc:
            print(f"failed ({exc})")
            films = []

        for film in films:
            film["ratings"] = get_ratings(film["title"], cache=cache)
            r = film["ratings"]
            if r.get("found"):
                if r.get("imdb") is not None:
                    imdb_s = f"IMDb {r['imdb']:.1f}"
                elif r.get("tmdb_rating") is not None:
                    imdb_s = f"TMDb {r['tmdb_rating']:.1f}"
                else:
                    imdb_s = "no IMDb"
                rt_s   = f"RT {r['rt']}%"         if r.get("rt")   else "no RT"
                status = f"  {imdb_s}, {rt_s}"
            else:
                status = "  not in OMDb"
            print(f"   {film['title']}{status}")

        movies_by_cinema[name] = films
        save_cache(cache)

    html = generate_html(movies_by_cinema)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"\nDone. Output: {OUTPUT}")
    print("Open movies.html in your browser.")


if __name__ == "__main__":
    main()
