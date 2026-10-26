"""Partite di calcio del giorno dai principali campionati (dati pubblici ESPN, senza chiave).

Restituisce le partite di oggi (ora italiana) ordinate per orario, con stato e risultato.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("matches")

TZ = ZoneInfo("Europe/Rome")
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard"
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
    ("ita.2", "Serie B", 9),
    ("ita.3", "Serie C", 10),          # se ESPN non ce l'ha, la Serie C arriva da TheSportsDB
]

# Seconda fonte gratuita (TheSportsDB) per le serie minori italiane che ESPN non copre bene.
SPORTSDB_URL = "https://www.thesportsdb.com/api/v1/json/{key}/eventsday.php?d={day}&s=Soccer"
SPORTSDB_KEYS = ("123", "3")       # chiave gratuita attuale, poi quella vecchia
MINOR = [(re.compile(r"serie\s*c|lega\s*pro", re.I), "Serie C", 10),
         (re.compile(r"serie\s*b", re.I), "Serie B", 9)]

_cache: dict[str, tuple[float, dict]] = {}


def _ssl_context() -> ssl.SSLContext:
    # Il Python del Mac spesso non trova i certificati di sistema: uso quelli di certifi.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
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


def _norm_team(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())[:6]


def parse_sportsdb(data: dict, day) -> list[dict]:
    out = []
    for ev in (data or {}).get("events") or []:
        league = ev.get("strLeague") or ""
        hit = next(((n, p) for rx, n, p in MINOR if rx.search(league) and "women" not in league.lower()
                    and "femminile" not in league.lower() and "ital" in (league.lower() + (ev.get("strCountry") or "").lower())), None)
        if not hit:
            continue
        ts = ev.get("strTimestamp") or (f"{ev.get('dateEvent')}T{ev.get('strTime') or '00:00:00'}")
        try:
            start = datetime.fromisoformat(ts.replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        local = start.astimezone(TZ)
        if local.date() != day:
            continue
        status = (ev.get("strStatus") or "").upper()
        hs, as_ = ev.get("intHomeScore"), ev.get("intAwayScore")
        if status in ("FT", "AET", "PEN", "MATCH FINISHED", "FINISHED"):
            state = "post"
        elif status in ("", "NS", "NOT STARTED", "TBD", "POSTP", "POSTPONED", "CANC") or hs in (None, ""):
            state = "pre"
        else:
            state = "in"
        out.append({
            "id": "tsdb-" + str(ev.get("idEvent")),
            "league": hit[0] + (f" · {m.group(1).upper()}" if (m := re.search(r"(?:girone|group)\s*([abc])", league, re.I)) else ""),
            "priority": hit[1],
            "start": start.isoformat(),
            "time": local.strftime("%H:%M"),
            "home": ev.get("strHomeTeam") or "?",
            "away": ev.get("strAwayTeam") or "?",
            "home_score": str(hs) if state != "pre" and hs not in (None, "") else None,
            "away_score": str(as_) if state != "pre" and as_ not in (None, "") else None,
            "state": state,
            "detail": ((f"{ev['strProgress']}'" if str(ev.get("strProgress") or "").isdigit() else ev.get("strProgress"))
                       or status) if state == "in" else "",
        })
    return out


async def today_matches() -> dict:
    now = datetime.now(TZ)
    day = now.date()
    key = day.isoformat()
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_S:
        return cached[1]
    # Le partite europee cadono nello stesso giorno anche in fuso USA: chiedo la data di oggi
    # e filtro comunque sull'ora italiana. Se ESPN rifiuta il parametro, uso la giornata corrente.
    dates = f"{day:%Y%m%d}"

    async def one(code, name, prio):
        base = URL.format(code=code)
        try:
            try:
                data = await asyncio.to_thread(_get_json, f"{base}?dates={dates}")
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                data = await asyncio.to_thread(_get_json, base)
            return parse_events(data, name, prio, day)
        except Exception as e:  # noqa: BLE001
            log.info("Partite %s non disponibili: %s", code, e)
            return None

    async def sportsdb():
        last = None
        for key in SPORTSDB_KEYS:
            try:
                data = await asyncio.to_thread(_get_json, SPORTSDB_URL.format(key=key, day=day.isoformat()))
                res = parse_sportsdb(data, day)
                log.info("Serie minori da TheSportsDB: %d partite (su %d del giorno)", len(res),
                         len((data or {}).get("events") or []))
                return res
            except Exception as e:  # noqa: BLE001
                last = e
        log.info("TheSportsDB non disponibile: %s", last)
        return None

    async def serie_c():
        import seriec

        def get_html(url):
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "it-IT,it;q=0.9"})
            with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as r:
                return r.read().decode("utf-8", "replace")

        out, ok = [], False
        for g in seriec.GIRONI:
            try:
                html = await asyncio.to_thread(get_html, seriec.URL.format(g=g))
                ok = True
                found = seriec.parse(html, day, g)
                out += found
                if not found:
                    nodes = seriec.text_nodes(html)
                    i = next((k for k, n in enumerate(nodes) if "oggi" in n.lower() or " ore " in f" {n.lower()} "), None)
                    log.info("Serie C girone %s: nessuna partita oggi. Testo vicino: %s", g.upper(),
                             nodes[i:i + 12] if i is not None else nodes[:12])
            except Exception as e:  # noqa: BLE001
                log.info("Serie C girone %s non disponibile: %s", g.upper(), e)
        log.info("Serie C da Virgilio: %d partite oggi", len(out))
        return out if ok else None

    results = await asyncio.gather(*(one(*lg) for lg in LEAGUES), serie_c(), sportsdb())
    if all(r is None for r in results) and cached:
        return cached[1]
    matches = [m for r in results if r for m in r]
    seen, pairs = set(), set()
    unique = []
    for m in matches:                     # ESPN prima, poi TheSportsDB: niente doppioni
        pair = (_norm_team(m["home"]), _norm_team(m["away"]))
        if m["id"] in seen or pair in pairs:
            continue
        seen.add(m["id"])
        pairs.add(pair)
        unique.append(m)
    unique.sort(key=lambda m: (m["start"], m["priority"]))
    res = {"date": key, "updated": now.strftime("%H:%M"), "matches": unique,
           "error": None if any(r is not None for r in results) else "Partite non disponibili al momento."}
    _cache[key] = (time.monotonic(), res)
    return res
