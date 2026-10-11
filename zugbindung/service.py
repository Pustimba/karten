"""Suche und Prüfung als Funktionen mit strukturiertem Ergebnis.

Kommandozeile und Weboberfläche nutzen beide dieses Modul.
"""
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date

from . import bahnde, transitous
from .history import History
from .model import Leg
from .score import Result, min_transfer, score

# Züge, die sich nicht mit einem DB-Sparpreis buchen lassen (FlixTrain, Nachtzüge
# anderer Bahnen, ausländische Betreiber). Treffer im Namen oder Betreiber.
EXCLUDE = re.compile(r"flix|\bFLX|nightjet|\bNJ\b|\bEN\b|\b\d+N\b|european sleeper|\bDRF\b|"
                     r"\bRDC\b|\bIntercity\b|\bSprinter\b|\bStoptrein\b|\bThalys\b|Eurostar",
                     re.IGNORECASE)
LONG_DISTANCE = re.compile(r"^(ICE|IC|EC|ECE|IRE)\b")
WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}
WD_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Ermäßigung -> Reisenden-Code der bahn.de-Suchmaske (Alter:Ermäßigung:Klasse:Anzahl)
DISCOUNT_CODES = {"keine": "", "bc25": "13:17:KLASSE_2:1", "bc50": "13:23:KLASSE_2:1"}


@dataclass
class SearchParams:
    von: str
    nach: str
    daten: list[str]
    ab: str = "04:00"
    bis: str = "19:00"
    raster: int = 30
    modi: list[str] = field(default_factory=lambda: ["alle", "ohne_ICE"])
    max_umstiege: int = 12
    min_umstieg: float = 4
    alle_betreiber: bool = False
    navigator_puffer: int = 3
    wochentage: str = ""
    gleicher_wochentag: bool = False
    ermaessigung: str = "bc25"
    von_name: str = ""        # Anzeigename, wenn von/nach eine Transitous-ID ist
    nach_name: str = ""


def history_days(h: History, wochentage: str = "", gleicher_wochentag: bool = False,
                 travel_day: date | None = None):
    wd = None
    if wochentage:
        wd = {WEEKDAYS[w.strip().lower()[:2]] for w in wochentage.split(",") if w.strip()}
    elif gleicher_wochentag and travel_day:
        wd = {travel_day.weekday()}
    return h.days(wd)


def bahn_url(h: History, nav: Result, ermaessigung: str = "bc25") -> str:
    """Vorbefüllte bahn.de-Suche (Start, Ziel, Datum, Uhrzeit, Ermäßigung)."""
    trains = [l for l in nav.legs if not l.walk]
    first, last = trains[0], trains[-1]
    from_eva = (nav.matched[0][0] if nav.matched else "") or first.from_eva
    to_eva = (nav.matched[-1][1] if nav.matched else "") or last.to_eva
    st = h.stations()
    if not from_eva:
        hit = h.station_by_name(first.from_name)
        from_eva = hit[0] if hit else ""
    if not to_eva:
        hit = h.station_by_name(last.to_name)
        to_eva = hit[0] if hit else ""
    so = st.get(from_eva, first.from_name)
    zo = st.get(to_eva, last.to_name)
    q = urllib.parse.quote
    url = (f"https://www.bahn.de/buchung/fahrplan/suche#sts=true&so={q(so)}&zo={q(zo)}"
           f"&soid={q(f'A=1@O={so}@L={from_eva.lstrip("0")}@')}&zoid={q(f'A=1@O={zo}@L={to_eva.lstrip("0")}@')}"
           f"&hd={first.dep:%Y-%m-%dT%H:%M:%S}&kl=2")
    code = DISCOUNT_CODES.get(ermaessigung, "")
    return url + (f"&r={code}" if code else "")


def via_suggestions(h: History, nav: Result) -> list[str]:
    """Bis zu zwei Zwischenhalte, mit denen bahn.de genau diese Kette findet:
    der Bahnhof, an dem der erste Fernzug beginnt, und der Umstieg mit dem
    höchsten Risiko. Fehlt einer davon, füllt ein weiterer Fernzug-Umstieg auf."""
    trains = [l for l in nav.legs if not l.walk]
    if len(trains) < 2:
        return []
    matched = nav.matched or [("", "")] * len(trains)
    st = h.stations()

    def name(k):   # Umstieg nach Zug k, in DB-Schreibweise wenn bekannt
        eva = matched[k][1] or matched[k + 1][0]
        return st.get(eva, trains[k].to_name)

    def risk(k):
        lbl = f"Anschluss {trains[k].to_name} ("
        return max([c.p or 0 for c in nav.components if c.label.startswith(lbl)] or [0])

    transfers = range(len(trains) - 1)
    fv = [k for k in transfers if LONG_DISTANCE.match(trains[k + 1].name) or LONG_DISTANCE.match(trains[k].name)]
    picks = []
    first_fv = next((k for k in transfers if LONG_DISTANCE.match(trains[k + 1].name)), None)
    if first_fv is not None:
        picks.append(first_fv)
    picks.append(max(transfers, key=risk))
    picks += fv
    out, used = [], []
    for k in picks:
        if k not in used:
            used.append(k)
        if len(used) == 2:
            break
    for k in sorted(used):
        n = name(k)
        if n not in out:
            out.append(n)
    return out


def result_dict(h: History, nav: Result, strict: Result, ermaessigung: str = "bc25",
                link: str | None = None) -> dict:
    trains = [l for l in nav.legs if not l.walk]
    first, last = trains[0], trains[-1]
    return {
        "p_navigator": nav.p_unabhaengig,
        "p_streng": strict.p_unabhaengig,
        "tage_navigator": [nav.tage_mit, nav.tage],
        "tage_streng": [strict.tage_mit, strict.tage],
        "navigator_puffer": nav.navigator_buffer,
        "wochentag": WD_NAMES[first.dep.weekday()],
        "ab": first.dep.isoformat(),
        "an": last.arr.isoformat(),
        "dauer_min": int((last.arr - first.dep).total_seconds() // 60),
        "umstiege": nav.transfers,
        "legs": [{"name": l.name, "von": l.from_name, "nach": l.to_name, "ab": l.dep.isoformat(),
                  "an": l.arr.isoformat(), "fussweg": l.walk} for l in nav.legs],
        "risiken": [{"teil": c.label, "anteil": c.p, "tage": c.n} for c in nav.components],
        "ohne_daten": nav.unbekannt,
        "zwischenhalte": via_suggestions(h, nav),
        "bahn_link": link or bahn_url(h, nav, ermaessigung),
    }


def _print(msg: str) -> None:
    import sys
    print(f"  {msg}", file=sys.stderr, flush=True)


def search(h: History, p: SearchParams, progress=_print) -> dict:
    from_id, from_name = transitous.find_stop(p.von)
    to_id, to_name = transitous.find_stop(p.nach)
    from_name, to_name = p.von_name or from_name, p.nach_name or to_name
    progress(f"Suche {from_name} → {to_name}")
    pairs: list[tuple[Result, Result]] = []
    for day in p.daten:
        days = history_days(h, p.wochentage, p.gleicher_wochentag, date.fromisoformat(day))
        conns = transitous.collect(from_id, to_id, day, p.ab, p.bis, p.raster, p.modi,
                                   max_transfers=p.max_umstiege, progress=progress)
        progress(f"{day}: bewerte {len(conns)} Verbindungen …")
        for legs in conns:
            trains = [l for l in legs if not l.walk]
            if not trains:
                continue
            if not p.alle_betreiber and any(EXCLUDE.search(f"{l.name} {l.operator}") for l in trains):
                continue
            mt = min_transfer(legs)
            if mt is not None and mt < p.min_umstieg:
                continue
            nav = score(legs, h, days, navigator_buffer=p.navigator_puffer)
            strict = score(legs, h, days, navigator_buffer=0)
            pairs.append((nav, strict))
    pairs.sort(key=lambda r: (-r[0].p_unabhaengig, -r[1].p_unabhaengig))
    return {
        "von": from_name, "nach": to_name, "daten": p.daten,
        "daten_von": h.first_day.isoformat(), "daten_bis": h.last_day.isoformat(),
        "pairs": pairs,
        "ergebnisse": [result_dict(h, n, s, p.ermaessigung) for n, s in pairs],
    }


def check(h: History, links: list[str], navigator_puffer: int = 3, wochentage: str = "",
          gleicher_wochentag: bool = False, progress=_print) -> list[dict]:
    out = []
    for text in links:
        text = text.strip()
        if not text:
            continue
        vbid = bahnde.vbid_from(text)
        progress(f"Hole Verbindung {vbid} von bahn.de …")
        data = bahnde.fetch(vbid)
        legs: list[Leg] = bahnde.recon_to_legs(data["hinfahrtRecon"])
        days = history_days(h, wochentage, gleicher_wochentag, legs[0].dep.date())
        nav = score(legs, h, days, navigator_buffer=navigator_puffer)
        strict = score(legs, h, days, navigator_buffer=0)
        link = text if text.startswith("http") else f"https://www.bahn.de/buchung/start?vbid={vbid}"
        d = result_dict(h, nav, strict, link=link)
        d.update({"titel": f"{data.get('startOrt')} → {data.get('zielOrt')}", "vbid": vbid,
                  "pairs": (nav, strict)})
        out.append(d)
    return out
