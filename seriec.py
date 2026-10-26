"""Partite di Serie C del giorno lette dai calendari pubblici di Virgilio Sport (gironi A, B, C).

La pagina mostra la giornata in corso: intestazioni tipo «ven 23 OTT ore 20:30» / «oggi ore 20:30»
e sotto le partite «Casa  Ospite  2 - 1» (o «-» se non giocata). Leggo il testo dei nodi HTML
in ordine e ricostruisco data, ora, squadre e risultato.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

log = logging.getLogger("seriec")
TZ = ZoneInfo("Europe/Rome")
URL = "https://www.virgilio.it/sport/calcio/calcio-italiano/serie-c/calendario-girone-{g}/"
GIRONI = ("a", "b", "c")
MONTHS = {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12}
DAY_RE = re.compile(r"\b(?:lun|mar|mer|gio|ven|sab|dom)\w*\.?\s+(\d{1,2})\s+([a-z]{3})\w*", re.I)
REL_RE = re.compile(r"\b(oggi|domani|ieri)\b", re.I)
TIME_RE = re.compile(r"\bore\s+(\d{1,2})[:.](\d{2})\b", re.I)
SCORE_RE = re.compile(r"^(\d{1,2})\s*[-–]\s*(\d{1,2})$")
NOT_TEAM = re.compile(r"giornata|classifica|calendario|risultat|serie c|girone|marcatori|notizie|video|^ore\b|\d", re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            t = " ".join(data.split())
            if t:
                self.nodes.append(t)


def text_nodes(html: str) -> list[str]:
    p = _Text()
    p.feed(html)
    # Unisco i risultati spezzati in più nodi: «2», «-», «1» → «2 - 1»
    out: list[str] = []
    for n in p.nodes:
        if out and re.fullmatch(r"\d{1,2}", n) and len(out) >= 2 and out[-1] in ("-", "–") and re.fullmatch(r"\d{1,2}", out[-2]):
            out[-2:] = [f"{out[-2]} - {n}"]
        else:
            out.append(n)
    return out


def parse(html: str, today: date, girone: str) -> list[dict]:
    nodes = text_nodes(html)
    cur_day: date | None = None
    cur_time: str | None = None
    names: list[str] = []
    matches = []
    for n in nodes:
        d, rel, t = DAY_RE.search(n), REL_RE.search(n), TIME_RE.search(n)
        if d or (rel and len(n) < 25) or (t and len(n) < 25):
            if d and d.group(2).lower()[:3] in MONTHS:
                month = MONTHS[d.group(2).lower()[:3]]
                year = today.year + (1 if month < today.month - 6 else -1 if month > today.month + 6 else 0)
                try:
                    cur_day = date(year, month, int(d.group(1)))
                except ValueError:
                    pass
            elif rel:
                cur_day = today + timedelta(days={"oggi": 0, "domani": 1, "ieri": -1}[rel.group(1).lower()])
            if t:
                cur_time = f"{int(t.group(1)):02d}:{t.group(2)}"
            names = []
            continue
        if cur_day is None:
            continue
        sc = SCORE_RE.match(n)
        if sc or n in ("-", "–", "vs", "-:-"):
            if len(names) >= 2:
                matches.append({"day": cur_day, "time": cur_time, "home": names[-2], "away": names[-1],
                                "score": (sc.group(1), sc.group(2)) if sc else None})
            names = []
            continue
        if 2 <= len(n) <= 32 and not NOT_TEAM.search(n):
            names.append(n)
        elif len(n) > 40:
            names = []
    out = []
    for m in matches:
        if m["day"] != today:
            continue
        hh, mm = (m["time"] or "00:00").split(":")
        start = datetime(today.year, today.month, today.day, int(hh), int(mm), tzinfo=TZ)
        played = m["score"] is not None
        now = datetime.now(TZ)
        state = "post" if played and now > start + timedelta(hours=2) else "in" if played or start <= now < start + timedelta(hours=2) else "pre"
        out.append({
            "id": f"vg-{girone}-{m['home']}-{m['away']}",
            "league": f"Serie C · {girone.upper()}",
            "priority": 10,
            "start": start.isoformat(),
            "time": m["time"] or "",
            "home": m["home"], "away": m["away"],
            "home_score": m["score"][0] if played else None,
            "away_score": m["score"][1] if played else None,
            "state": state,
            "detail": "Live" if state == "in" else "",
        })
    return out
