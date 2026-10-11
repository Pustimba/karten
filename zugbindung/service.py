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
    von_name: str = ""        # Anzeigename zur gewählten ID
    nach_name: str = ""
    von_id: str = ""          # aus der Vorschlagsliste gewählte Halte-ID
    nach_id: str = ""


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
    from_id, from_name = (p.von_id, p.von_name or p.von) if p.von_id else transitous.find_stop(p.von)
    to_id, to_name = (p.nach_id, p.nach_name or p.nach) if p.nach_id else transitous.find_stop(p.nach)
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


# ---------------------------------------------------------------- beste Reisetage (bahn.de, mit Preis)
@dataclass
class BestDaysParams:
    von: str                        # Wunschstrecke: hier willst du wirklich einsteigen
    nach: str                       # ... und hier aussteigen
    datum_von: str
    datum_bis: str
    wochentage: list[int] = field(default_factory=list)   # 0 = Montag; leer = alle
    ab: str = "05:00"
    bis: str = "19:00"
    max_preis: float = 20.0
    ermaessigung: str = "bc25"
    navigator_puffer: int = 3
    min_umstieg: float = 0          # bahn.de verkauft nur machbare Umstiege; 0 = alles behalten
    max_seiten: int = 12            # Ergebnisseiten pro Tag (je ca. 5 Verbindungen)
    von_id: str = ""                # bahn.de-Orts-ID aus der Vorschlagsliste
    nach_id: str = ""
    starts: list[str] = field(default_factory=list)   # Ticket darf hier früher beginnen
    ziele: list[str] = field(default_factory=list)    # Ticket darf hier später enden


def _resolve(client, text: str, given_id: str) -> dict:
    if given_id:
        return {"id": given_id, "name": text}
    hits = client.locations(text)
    if not hits:
        raise RuntimeError(f"bahn.de kennt keinen Halt '{text}'.")
    return hits[0]


def eur(x: float) -> str:
    return f"{x:.2f} €".replace(".", ",")


def travel_dates(p: BestDaysParams) -> list[date]:
    from datetime import timedelta
    d, end, out = date.fromisoformat(p.datum_von), date.fromisoformat(p.datum_bis), []
    while d <= end:
        if not p.wochentage or d.weekday() in p.wochentage:
            out.append(d)
        d += timedelta(days=1)
    return out


def suggest_endpoints(h: History, von: str, nach: str, progress=_print, limit: int = 8,
                      umweg: float = 1.3, max_winkel: float = 75) -> dict:
    """Bahnhöfe vor dem Wunsch-Start und hinter dem Wunsch-Ziel, über die ein
    Ticket laufen kann, das die Wunschstrecke einschließt.

    Kandidaten sind Bahnhöfe mit direkten Zügen zum Start (bzw. vom Ziel) aus
    den Verspätungsdaten. Sie bleiben nur, wenn der Umweg klein ist: der Weg
    Kandidat → Start → Ziel darf höchstens 30 % (umweg) länger sein als die Luftlinie
    Kandidat → Ziel (für Ziele entsprechend), und die Richtung muss passen: der
    Weg Kandidat → Start darf höchstens max_winkel Grad von der Richtung
    Start → Ziel abweichen (für Ziele entsprechend)."""
    from . import geo
    s_hit, e_hit = h.station_by_name(von), h.station_by_name(nach)
    if not s_hit or not e_hit:
        raise RuntimeError(f"'{von if not s_hit else nach}' nicht in den Verspätungsdaten gefunden.")
    s_name, e_name = s_hit[1], e_hit[1]
    progress(f"Wunschstrecke: {s_name} → {e_name}")
    cs, ce = geo.coords(s_name), geo.coords(e_name)
    if not cs or not ce:
        raise RuntimeError("Koordinaten für die Wunschstrecke nicht gefunden.")
    d_se = geo.km(cs, ce)

    def city(n):
        # "Frankfurt (Main) Hbf" -> "frankfurt(main)", "Berlin-Spandau" -> "berlin"
        m = re.match(r"^([^\W\d_]+)(\s*\([^)]*\))?", n.strip())
        return re.sub(r"\s+", "", m.group(0)).lower() if m else n.lower()

    skip = {city(s_name), city(e_name)}

    def pick(cands, before: bool):
        out, looked = [], 0
        # häufig angefahren und weiter weg zuerst: Endpunkte von Regionallinien
        # (Cottbus, Frankfurt (Oder)) vor deren nahen Zwischenhalten
        cands = sorted(cands, key=lambda c: -c[2] * min(c[3], 60))
        for eva, name, rides, mins in cands:
            if city(name) in skip or looked >= 45:
                continue
            looked += 1
            c = geo.coords(name)
            if not c:
                continue
            if before:   # Kandidat X vor dem Start S: X → S soll Richtung Ziel zeigen
                d_near, d_far = geo.km(c, cs), geo.km(c, ce)
                ang = geo.angle(c, cs, cs, ce)
            else:        # Kandidat Y hinter dem Ziel E: E → Y soll die Fahrtrichtung fortsetzen
                d_near, d_far = geo.km(c, ce), geo.km(c, cs)
                ang = geo.angle(cs, ce, ce, c)
            if d_near < 15 or d_near > 160 or d_far <= d_se or (d_near + d_se) / d_far > umweg or ang > max_winkel:
                continue
            if d_near > 10 + mins * 3.5:   # schneller als 210 km/h: falscher Ort gleichen Namens
                continue
            if any(geo.km(c, o["_c"]) < 12 for o in out):   # Nachbarorte zusammenfassen
                continue
            out.append({"name": name, "km": round(d_near), "minuten": round(mins), "fahrten": rides,
                        "winkel": round(ang), "_c": c})
            if len(out) >= limit:
                break
        for o in out:
            o.pop("_c")
        return out

    # alle Bahnhöfe der Wunschstädte als Knoten: einzelne Bahnsteige (etwa
    # Berlin Hbf oben) fehlen in den Daten teilweise
    st = h.stations()
    s_evas = [e for e, n in st.items() if city(n) == city(s_name)]
    e_evas = [e for e, n in st.items() if city(n) == city(e_name)]
    progress("Suche Zubringer vor dem Start …")
    before = pick(h.connected(s_evas, True, 15, 180), True)
    progress(f"{len(before)} mögliche Ticket-Starts. Suche Bahnhöfe hinter dem Ziel …")
    after = pick(h.connected(e_evas, False, 10, 150), False)
    progress(f"{len(after)} mögliche Ticket-Ziele.")
    return {"von": s_name, "nach": e_name, "km": round(d_se), "starts": before, "ziele": after}


def best_days(h: History, p: BestDaysParams, progress=_print, day_done=lambda d, n: None) -> dict:
    """Fragt bahn.de für jede Kombination aus Ticket-Start und Ticket-Ziel und
    jeden Reisetag ab ("Schnelle Verbindungen" aus), behält Verbindungen bis
    max_preis und bewertet sie. Der Wunsch-Start und das Wunsch-Ziel gehören
    immer mit zu den Kombinationen."""
    client = bahnde.Client()
    wunsch_von = _resolve(client, p.von, p.von_id)
    wunsch_nach = _resolve(client, p.nach, p.nach_id)
    starts = [wunsch_von] + [_resolve(client, n, "") for n in p.starts if n.strip()]
    ziele = [wunsch_nach] + [_resolve(client, n, "") for n in p.ziele if n.strip()]
    pairs = [(a, b) for a in starts for b in ziele]
    progress(f"Wunschstrecke {wunsch_von['name']} → {wunsch_nach['name']}, {len(pairs)} Ticket-Varianten")
    days_hist = h.days()
    found: dict[tuple, dict] = {}
    per_day: dict[str, dict] = {}
    for d in travel_dates(p):
        for a, b in pairs:
            n_all, n_cheap, cheapest = _scan(client, h, p, a, b, d, days_hist, found)
            t = per_day.setdefault(d.isoformat(), {"datum": d.isoformat(), "verbindungen": 0, "bis_max_preis": 0,
                                                   "guenstigster": None})
            t["verbindungen"] += n_all
            t["bis_max_preis"] += n_cheap
            if cheapest is not None and (t["guenstigster"] is None or cheapest < t["guenstigster"]):
                t["guenstigster"] = cheapest
            msg = (f"{WD_NAMES[d.weekday()]} {d:%d.%m.} {a['name']} → {b['name']}: {n_all} Verbindungen, "
                   f"{n_cheap} bis {eur(p.max_preis)}")
            progress(msg + (f", günstigste {eur(cheapest)}" if cheapest is not None else ""))
        day_done(d, per_day[d.isoformat()]["bis_max_preis"])
    results = sorted(found.values(), key=lambda r: (-r["p_navigator"], r["preis"], r["ab"]))
    return {"von": wunsch_von["name"], "nach": wunsch_nach["name"], "tage": list(per_day.values()),
            "ergebnisse": results, "daten_von": h.first_day.isoformat(), "daten_bis": h.last_day.isoformat(),
            "max_preis": p.max_preis, "varianten": [f"{a['name']} → {b['name']}" for a, b in pairs]}


def _scan(client, h: History, p: BestDaysParams, a: dict, b: dict, d: date, days_hist, found: dict):
    """Eine Ticket-Variante a → b an einem Tag durchblättern."""
    when = f"{d.isoformat()}T{p.ab}:00"
    paging, pages, n_all, n_cheap, cheapest = None, 0, 0, 0, None
    while pages < p.max_seiten:
        data = client.journeys(a["id"], b["id"], when, p.ermaessigung, paging=paging)
        pages += 1
        vs = data.get("verbindungen") or []
        last_dep = None
        for v in vs:
            legs = bahnde.verbindung_to_legs(v)
            trains = [l for l in legs if not l.walk]
            if not trains or bahnde.cancelled(v):
                continue
            last_dep = trains[0].dep
            if trains[0].dep.date() != d or trains[0].dep.strftime("%H:%M") > p.bis:
                continue
            n_all += 1
            price = bahnde.price_of(v)
            if price is not None:
                cheapest = price if cheapest is None else min(cheapest, price)
            if price is None or price > p.max_preis:
                continue
            mt = min_transfer(legs)
            if mt is not None and mt < p.min_umstieg:
                continue
            key = (a["id"], b["id"]) + tuple((l.name, l.dep) for l in trains)
            if key in found:
                continue
            n_cheap += 1
            nav = score(legs, h, days_hist, navigator_buffer=p.navigator_puffer)
            strict = score(legs, h, days_hist, navigator_buffer=0)
            r = result_dict(h, nav, strict, p.ermaessigung)
            r.update(preis=price, ticket_von=a["name"], ticket_nach=b["name"])
            found[key] = r
        paging = (data.get("verbindungReference") or {}).get("later")
        if not vs or not paging or (last_dep and (last_dep.date() > d or last_dep.strftime("%H:%M") > p.bis)):
            break
    return n_all, n_cheap, cheapest
