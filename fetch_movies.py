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

import asyncio
import json
import os
import sys
import time
import unicodedata
from datetime import datetime
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
OMDB_KEY   = os.environ.get("OMDB_API_KEY", "")
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
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalize_title(title: str) -> str:
    """Strip diacritics: 'César et Rosalie' → 'Cesar et Rosalie'."""
    return unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").strip()


_NOT_FOUND: dict = {"found": False, "imdb": None, "rt": None}


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
        "poster":  data.get("Poster") if data.get("Poster") != "N/A" else None,
        "plot":    data.get("Plot"),
        "imdb_id": data.get("imdbID"),
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
                       key=lambda r: SequenceMatcher(None, search_title.lower(), r["Title"].lower()).ratio())
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


def get_ratings(title: str, year: Optional[str] = None, cache: Optional[dict] = None) -> dict:
    """Look up IMDb + RT ratings from OMDb with three fallback levels:
    1. exact title  2. diacritics stripped  3. fuzzy search (handles 'et' vs '&' etc.)"""
    key = f"{title}|||{year or ''}"
    if cache is not None and key in cache:
        return cache[key]

    if not OMDB_KEY:
        return {"found": False, "imdb": None, "rt": None}

    result = _omdb_fetch(title, year)
    if not result["found"]:
        normalized = _normalize_title(title)
        if normalized != title:
            result = _omdb_fetch(normalized, year)
    if not result["found"]:
        result = _omdb_search(_normalize_title(title))

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

def _filmkoepel_showtimes(film_url: str, film_title: str) -> list[dict]:
    """Parse ScreeningEvent JSON-LD from a Filmkoepel film page.
    Filters by film name to avoid picking up other films' showtimes on the same page."""
    try:
        resp = SESSION.get(film_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        showtimes = []
        title_lower = film_title.lower()
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, AttributeError):
                continue
            events = data if isinstance(data, list) else data.get("@graph", [data])
            for ev in events:
                if ev.get("@type") != "ScreeningEvent":
                    continue
                # Only include screenings for this film
                ev_name = ev.get("name", "").lower()
                if ev_name and ev_name != title_lower:
                    continue
                start = ev.get("startDate", "")
                if len(start) < 16:
                    continue
                # "2026-06-07T11:00+02:00" → date="za 7 jun", time="11:00"
                dt = datetime.strptime(start[:16], "%Y-%m-%dT%H:%M")
                date_label = f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"
                showtimes.append({"date": date_label, "time": start[11:16], "sort_date": dt.strftime("%Y-%m-%d")})
        return showtimes
    except Exception:
        return []


def scrape_filmkoepel() -> list[dict]:
    """
    Filmkoepel Haarlem — homepage: filmkoepel.nl
    Currently playing films have an <img> inside the <a href="/films/slug/"> tag.
    Showtimes are read from the JSON-LD on each film's detail page.
    """
    resp = SESSION.get("https://www.filmkoepel.nl/", timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    seen: set[str] = set()
    films: list[dict] = []
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "/films/" not in href:
            continue
        if not a.find("img"):
            continue  # archived entries have no poster image
        img = a.find("img")
        title = (img.get("alt") or "").strip() or a.get_text(strip=True)
        if not title or title in seen:
            continue
        seen.add(title)
        link = href if href.startswith("http") else f"https://www.filmkoepel.nl{href}"
        showtimes = _filmkoepel_showtimes(link, title)
        films.append({"title": title, "link": link, "showtimes": showtimes})
    return films


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


async def _scrape_pathe_async() -> list[dict]:
    """
    Pathé Tuschinski Amsterdam — pathe.nl blocks plain HTTP (403).
    Playwright renders the React app; we then find links to /film/slug.
    """
    from playwright.async_api import async_playwright  # type: ignore[import]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="nl-NL",
            ignore_https_errors=True,
        )
        page = await ctx.new_page()
        await page.goto(
            "https://www.pathe.nl/bioscoop/tuschinski",
            wait_until="networkidle",
            timeout=45_000,
        )
        # Extra wait for React hydration to complete
        await page.wait_for_timeout(3000)
        content = await page.content()
        await browser.close()

    soup = BeautifulSoup(content, "html.parser")

    all_film_hrefs = [a["href"] for a in soup.find_all("a", href=True) if "film" in a["href"].lower()]
    if not all_film_hrefs:
        total_links = len(soup.find_all("a", href=True))
        print(f"\n  [!] Pathe page did not render film data ({total_links} link(s) found).")
        print(    "      This usually means the site is temporarily down or blocking headless browsers.")
        return []

    seen: set[str] = set()
    films: list[dict] = []

    # Pathé film detail pages: /film/[slug] or /films/[slug]
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if not re.search(r"/films?/[^/]+", href):
            continue
        title: str = (
            a.get("data-title")
            or a.get("aria-label")
            or ""
        )
        if not title:
            h = a.find(["h1", "h2", "h3", "h4", "span"])
            title = h.get_text(strip=True) if h else a.get_text(strip=True)
        title = title.strip()
        if not title or len(title) < 2 or title in seen:
            continue
        seen.add(title)
        link = href if href.startswith("http") else f"https://www.pathe.nl{href}"
        films.append({"title": title, "link": link, "showtimes": []})
    return films


def scrape_pathe() -> list[dict]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("  [!] playwright not installed — Pathé Tuschinski skipped.")
        print("      Fix: pip install playwright && playwright install chromium")
        return []
    return asyncio.run(_scrape_pathe_async())


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


# ── Filter ─────────────────────────────────────────────────────────────────────

def passes_filter(r: dict) -> bool:
    """
    Include if:
      - not found in OMDb at all (no rating data exists)
      - found but both IMDb and RT scores are absent
      - IMDb >= 7.0
      - RT >= 70%
    Exclude only when at least one score is present and all scores fall below threshold.
    """
    if not r.get("found"):
        return True
    imdb = r.get("imdb")
    rt   = r.get("rt")
    if imdb is None and rt is None:
        return True
    if imdb is not None and rt is not None:
        return imdb >= IMDB_MIN and rt >= RT_MIN
    return (imdb is not None and imdb >= IMDB_MIN) or (rt is not None and rt >= RT_MIN)


# ── HTML generation ─────────────────────────────────────────────────────────────

def _showtimes_html(showtimes: list[dict]) -> str:
    if not showtimes:
        return ""
    # Group by (date, cinema) → sorted list of times
    groups: dict[tuple, list[str]] = {}
    for st in showtimes:
        key = (st.get("sort_date", ""), st.get("date", ""), st.get("cinema", ""))
        groups.setdefault(key, []).append(st["time"])
    rows = []
    for (sort_date, date, cinema), times in sorted(groups.items()):
        times_str = " · ".join(sorted(set(times)))
        date_html = f'<span class="st-date">{date}</span>' if date else ""
        cinema_html = f'<span class="st-cinema">{cinema}</span>' if cinema else ""
        rows.append(f'<div class="st-row">{date_html}<span class="st-times">{times_str}</span>{cinema_html}</div>')
    return f'<div class="showtimes">{"".join(rows)}</div>'


def _badge(label: str, value, threshold) -> str:
    if value is None:
        return f'<span class="badge gray">{label} —</span>'
    cls = "green" if value >= threshold else "red"
    disp = f"{value:.1f}" if isinstance(value, float) else f"{value}%"
    return f'<span class="badge {cls}">{label} {disp}</span>'


def _card(title: str, r: dict, cinemas: list[str], links: dict, showtimes: list[dict] = None) -> str:
    poster_html = f'<img src="{r["poster"]}" alt="" loading="lazy">' if r.get("poster") else ""
    if r.get("imdb_id"):
        title_html = f'<a href="https://www.imdb.com/title/{r["imdb_id"]}/" target="_blank">{r.get("title", title)}</a>'
    else:
        title_html = title
    year_html  = f' <span class="year">({r["year"]})</span>' if r.get("year") else ""
    plot_html  = f'<p class="plot">{r["plot"]}</p>' if r.get("plot") else ""
    badges_html = (
        _badge("IMDb", r.get("imdb"), IMDB_MIN) + _badge("RT", r.get("rt"), RT_MIN)
        if r.get("found")
        else '<span class="badge gray">not in OMDb</span>'
    )
    cinema_tags = " ".join(
        f'<a class="ctag" href="{links.get(c, "#")}" target="_blank">{c}</a>'
        for c in cinemas
    )
    st_html = _showtimes_html(showtimes or [])
    return f"""<div class="card">
  <div class="thumb">{poster_html}</div>
  <div class="body">
    <h3>{title_html}{year_html}</h3>
    <div class="badges">{badges_html}</div>
    {plot_html}
    {st_html}
    <div class="ctags">{cinema_tags}</div>
  </div>
</div>"""


def generate_html(movies_by_cinema: dict) -> str:
    # Merge films that play in multiple cinemas into one entry
    merged: dict[str, dict] = {}
    for cinema, films in movies_by_cinema.items():
        short = CINEMA_SHORT.get(cinema, cinema)
        for f in films:
            t = f["title"]
            if t not in merged:
                merged[t] = {"r": f["ratings"], "cinemas": [], "links": {}, "showtimes": []}
            if short not in merged[t]["cinemas"]:
                merged[t]["cinemas"].append(short)
            merged[t]["links"][short] = f["link"]
            for st in f.get("showtimes", []):
                merged[t]["showtimes"].append({**st, "cinema": short})

    EXCLUDED: set[str] = set()
    merged = {t: d for t, d in merged.items() if t not in EXCLUDED}

    good = [(t, d) for t, d in merged.items() if d["r"].get("found") and passes_filter(d["r"])]
    misc = [(t, d) for t, d in merged.items() if not d["r"].get("found")]

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
        return "\n".join(_card(t, d["r"], d["cinemas"], d["links"], d.get("showtimes", [])) for t, d in lst)

    misc_section = f"""
<details>
  <summary>Misc ({len(misc)} film{"s" if len(misc) != 1 else ""})</summary>
  <div class="grid">{cards_html(misc)}</div>
</details>""" if misc else ""

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
.showtimes {{ font-size: 0.72rem; display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; }}
.st-row {{ display: flex; align-items: baseline; gap: 0.5rem; }}
.st-date {{ color: var(--muted); min-width: 5.5rem; flex-shrink: 0; }}
.st-times {{ color: var(--text); letter-spacing: 0.02em; }}
.st-cinema {{ color: var(--gray); font-size: 0.68rem; }}
.ctags {{ display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.35rem; }}
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
</style>
</head>
<body>
<header>
  <h1>Now Playing</h1>
  <p class="meta">
    Generated {now}&nbsp;&nbsp;·&nbsp;&nbsp;
    {total} films found&nbsp;&nbsp;·&nbsp;&nbsp;
    Criteria: IMDb &ge; {IMDB_MIN} and RT &ge; {RT_MIN}% &nbsp;(unrated or single-rated films always included)
  </p>
</header>

<p class="section-label">{good_count} film{"s" if good_count != 1 else ""} matching criteria</p>
{"<p class='empty'>No films found.</p>" if not good else f'<div class="grid">{cards_html(good)}</div>'}

{misc_section}
</body>
</html>"""


# ── Entry point ─────────────────────────────────────────────────────────────────

CINEMAS: dict = {
    "Filmkoepel Haarlem":         scrape_filmkoepel,
    "Filmschuur Haarlem":         scrape_schuur,
    "Pathé Tuschinski Amsterdam": scrape_pathe,
    "Eye Filmmuseum Amsterdam":   scrape_eye,
}

CINEMA_SHORT: dict[str, str] = {
    "Filmkoepel Haarlem":         "Filmkoepel",
    "Filmschuur Haarlem":         "Filmschuur",
    "Pathé Tuschinski Amsterdam": "Pathé Tuschinski",
    "Eye Filmmuseum Amsterdam":   "Eye Filmmuseum",
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
                imdb_s = f"IMDb {r['imdb']:.1f}" if r.get("imdb") else "no IMDb"
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
