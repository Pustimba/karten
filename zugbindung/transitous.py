"""Verbindungen aus Transitous (https://transitous.org) holen.

Transitous ist ein offener Routenplaner auf Basis der deutschen Sollfahrplan-
Daten (DELFI). Er liefert Verbindungen für beliebige Tage im Fahrplanzeitraum,
auch dann, wenn bahn.de automatisierte Anfragen blockiert.
Bitte sparsam abfragen: zwischen zwei Anfragen liegt eine Pause.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .history import name_similarity
from .model import Leg

BASE = "https://api.transitous.org/api"
# Ohne eigenen User-Agent antwortet Transitous mit 403.
HEADERS = {"User-Agent": "zugbindung-tool/1.0 (private Auswertung)"}
BERLIN = ZoneInfo("Europe/Berlin")
PAUSE = 1.5

MODES = {
    "alle": None,
    "ohne_ICE": "LONG_DISTANCE,REGIONAL_FAST_RAIL,REGIONAL_RAIL,SUBURBAN,BUS,TRAM,SUBWAY,FERRY",
}


def _get(path: str, params: dict) -> dict | list:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # Netzwerk, 429, 5xx: kurz warten und erneut
            last = e
            time.sleep(10 * (attempt + 1))
    print(f"  Transitous-Anfrage fehlgeschlagen: {last}", file=sys.stderr)
    return {}


def find_stop(name: str) -> tuple[str, str]:
    """Bahnhofsname -> (Transitous-ID, Name).

    Der Geocoder von Transitous sucht unscharf. Deshalb werden mehrere
    Schreibweisen abgefragt und nur Halte behalten, deren Name mit dem Ort
    (erstes Wort des Suchbegriffs) beginnt. Davon gewinnt ein Hauptbahnhof,
    wenn danach gesucht wurde, dann ein Bahnhof, dann ein deutscher
    DELFI-Halt, dann die größte Namensähnlichkeit.
    Eine Transitous-ID (z. B. "de-DELFI_de:12071:900445010") wird unverändert
    übernommen."""
    if re.match(r"^[a-z]{2}-[A-Za-z]", name) and ":" in name:
        return name, name
    queries = dict.fromkeys([name, re.sub(r"\bHbf\b", "Hauptbahnhof", name), f"{name} Bahnhof"])
    res = []
    for q in queries:
        res += [r for r in (_get("/v1/geocode", {"text": q, "type": "STOP"}) or [])
                if r.get("type") == "STOP" and "flixbus" not in str(r.get("id", "")).lower()]
        time.sleep(PAUSE)
    city = re.findall(r"[^\W\d_]+", name.lower())[0]
    same = [r for r in res if re.match(rf"{re.escape(city)}\b", r.get("name", "").lower())]
    want_hbf = bool(re.search(r"hbf|hauptbahnhof", name, re.IGNORECASE))
    if not same:
        found = "\n".join(f"  {r.get('name')}  ->  {r.get('id')}" for r in res[:8])
        raise SystemExit(f"Kein Halt in '{city}' für '{name}' gefunden. Gefunden wurde:\n{found}\n"
                         f"Andere Schreibweise versuchen oder die ID direkt angeben.")

    def rank(r):
        n = r.get("name", "")
        station = bool(re.search(r"bahnhof|hbf|\bbf\b", n, re.IGNORECASE)) and "bus" not in n.lower()
        hbf = bool(re.search(r"hbf|hauptbahnhof", n, re.IGNORECASE)) == want_hbf
        return (hbf, station, str(r.get("id", "")).startswith("de-DELFI"), name_similarity(n, name))

    best = max(same, key=rank)
    return best["id"], best["name"]


def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BERLIN).replace(tzinfo=None)


def itinerary_to_legs(it: dict) -> list[Leg]:
    legs = []
    for l in it["legs"]:
        dep, arr = _local(l["startTime"]), _local(l["endTime"])
        if l["mode"] == "WALK":
            legs.append(Leg("Fußweg", "", "", l["from"]["name"], l["to"]["name"], dep, arr, walk=True))
            continue
        num = re.sub(r"\D", "", l.get("tripShortName") or "").lstrip("0")
        line = l.get("routeShortName") or ""
        if line.isdigit():      # Fernverkehrs-Liniennummern ("10") kennt IRIS nicht
            line = ""
        display = l.get("displayName") or line or l.get("mode", "")
        name = display if num and num in display else f"{display} {num}".strip()
        legs.append(Leg(name, num, line, l["from"]["name"], l["to"]["name"], dep, arr,
                        operator=l.get("agencyName") or ""))
    return legs


def collect(from_id: str, to_id: str, day: str, start: str, end: str, step_min: int,
            modes: list[str], max_transfers: int = 12, per_query: int = 8) -> list[list[Leg]]:
    """Fragt im Raster von step_min Minuten zwischen start und end (Ortszeit)
    Verbindungen ab und gibt die eindeutigen als Leg-Listen zurück."""
    t = datetime.fromisoformat(f"{day}T{start}").replace(tzinfo=BERLIN)
    t_end = datetime.fromisoformat(f"{day}T{end}").replace(tzinfo=BERLIN)
    seen: dict[tuple, list[Leg]] = {}
    while t <= t_end:
        for m in modes:
            params = {"fromPlace": from_id, "toPlace": to_id,
                      "time": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"),
                      "numItineraries": per_query, "maxTransfers": max_transfers}
            if MODES.get(m):
                params["transitModes"] = MODES[m]
            data = _get("/v5/plan", params) or {}
            for it in data.get("itineraries", []):
                legs = itinerary_to_legs(it)
                first = next((l for l in legs if not l.walk), None)
                if first is None or first.dep.date().isoformat() != day:
                    continue   # Transitous springt am Abend auf den Folgetag
                key = tuple((l.name, l.dep) for l in legs if not l.walk)
                seen.setdefault(key, legs)
            time.sleep(PAUSE)
        print(f"  {day} {t:%H:%M}: {len(seen)} Verbindungen", file=sys.stderr, flush=True)
        t += timedelta(minutes=step_min)
    return list(seen.values())
