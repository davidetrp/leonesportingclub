"""Partite di calcio del giorno dai principali campionati (dati pubblici ESPN, senza chiave).

Restituisce le partite di oggi (ora italiana) ordinate per orario, con stato e risultato.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("matches")

TZ = ZoneInfo("Europe/Rome")
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard?dates={dates}"
CACHE_S = 120

# (codice ESPN, nome mostrato, priorità: più basso = più importante)
LEAGUES = [
    ("uefa.champions", "Champions League", 0),
    ("ita.1", "Serie A", 1),
    ("uefa.europa", "Europa League", 2),
    ("uefa.europa.conf", "Conference League", 3),
    ("ita.coppa_italia", "Coppa Italia", 4),
    ("eng.1", "Premier League", 5),
    ("esp.1", "LaLiga", 6),
    ("ger.1", "Bundesliga", 7),
    ("fra.1", "Ligue 1", 8),
    ("fifa.world", "Mondiali", 0),
    ("uefa.nations", "Nations League", 2),
]

_cache: dict[str, tuple[float, dict]] = {}


def _ssl_context() -> ssl.SSLContext:
    # Il Python del Mac spesso non trova i certificati di sistema: uso quelli di certifi.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 CanaliSport"})
    with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_events(data: dict, league: str, priority: int, day) -> list[dict]:
    out = []
    for ev in data.get("events", []) or []:
        try:
            start = datetime.strptime(ev["date"].replace("Z", "+0000"), "%Y-%m-%dT%H:%M%z")
        except (KeyError, ValueError):
            try:
                start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
        local = start.astimezone(TZ)
        if local.date() != day:
            continue
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        home, away = teams.get("home", {}), teams.get("away", {})
        st = (ev.get("status") or {}).get("type", {})
        state = st.get("state", "pre")            # pre | in | post
        tv = []
        for b in comp.get("broadcasts", []) or []:
            tv += b.get("names", [])
        out.append({
            "id": ev.get("id"),
            "league": league,
            "priority": priority,
            "start": start.astimezone(timezone.utc).isoformat(),
            "time": local.strftime("%H:%M"),
            "home": (home.get("team") or {}).get("shortDisplayName") or (home.get("team") or {}).get("displayName", "?"),
            "away": (away.get("team") or {}).get("shortDisplayName") or (away.get("team") or {}).get("displayName", "?"),
            "home_score": home.get("score") if state != "pre" else None,
            "away_score": away.get("score") if state != "pre" else None,
            "state": state,
            "detail": (ev.get("status") or {}).get("displayClock") if state == "in" else st.get("shortDetail", ""),
        })
    return out


async def today_matches() -> dict:
    now = datetime.now(TZ)
    day = now.date()
    key = day.isoformat()
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_S:
        return cached[1]
    # ESPN usa giorni in fuso USA: chiedo ieri-oggi-domani e filtro sull'ora italiana.
    dates = f"{(day - timedelta(days=1)):%Y%m%d}-{(day + timedelta(days=1)):%Y%m%d}"

    async def one(code, name, prio):
        try:
            data = await asyncio.to_thread(_get_json, URL.format(code=code, dates=dates))
            return parse_events(data, name, prio, day)
        except Exception as e:  # noqa: BLE001
            log.info("Partite %s non disponibili: %s", code, e)
            return None

    results = await asyncio.gather(*(one(*lg) for lg in LEAGUES))
    if all(r is None for r in results) and cached:
        return cached[1]
    matches = [m for r in results if r for m in r]
    seen = set()
    unique = []
    for m in matches:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        unique.append(m)
    unique.sort(key=lambda m: (m["start"], m["priority"]))
    res = {"date": key, "updated": now.strftime("%H:%M"), "matches": unique,
           "error": None if any(r is not None for r in results) else "Partite non disponibili al momento."}
    _cache[key] = (time.monotonic(), res)
    return res
