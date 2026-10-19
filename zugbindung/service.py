"""Suche und Prüfung als Funktionen mit strukturiertem Ergebnis.

Kommandozeile und Weboberfläche nutzen beide dieses Modul.
"""
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import bahnde, transitous
from .history import History
from .model import Leg
from .score import MIN_DAYS, Result, min_transfer, score

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


# Ketten mit einem Umstieg, dessen Anschluss oft reißt, rücken in der
# Rangfolge um so viel nach vorn (angezeigt wird weiter der echte Wert).
# Sie haben einen zweiten, unabhängigen Auslöser: Fällt einer weg (Baustelle
# vorbei, neuer Fahrplan mit mehr Puffer), bleibt der andere. Und "Anschluss
# nicht erreichbar" ist eindeutiger als eine Verspätungsprognose.
ANSCHLUSS_BONUS = 0.05
ANSCHLUSS_MIN = 0.10


def second_trigger(nav: Result) -> str:
    """Bahnhof des wackligsten Anschlusses, wenn er in mindestens
    ANSCHLUSS_MIN der Tage reißt, sonst ""."""
    best = None
    for c in nav.components:
        m = re.match(r"Anschluss (.+) \((-?\d+) min\)$", c.label)
        if m and c.p is not None and c.n >= MIN_DAYS and c.p >= ANSCHLUSS_MIN and (best is None or c.p > best[0]):
            best = (c.p, m.group(1))
    return best[1] if best else ""


def rank_key(r: dict) -> float:
    """Wert für die Rangfolge: echte Wahrscheinlichkeit plus Bonus für einen
    wackligen Anschluss und für einen Bauhinweis von bahn.de."""
    return (r["p_kombiniert"] + (ANSCHLUSS_BONUS if r.get("zweiter_ausloeser") else 0)
            + (0.05 if r.get("baustelle") else 0))


def result_dict(h: History, nav: Result, strict: Result, ermaessigung: str = "bc25",
                link: str | None = None) -> dict:
    trains = [l for l in nav.legs if not l.walk]
    first, last = trains[0], trains[-1]
    return {
        "zweiter_ausloeser": second_trigger(nav),
        "p_kombiniert": nav.p_kombiniert,
        "p_navigator": nav.p_unabhaengig,
        "fernverkehr": any(l.long_distance() for l in trains),
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
                  "an": l.arr.isoformat(), "fussweg": l.walk, "fern": l.long_distance()} for l in nav.legs],
        "risiken": [{"teil": c.label, "anteil": c.p, "tage": c.n} for c in nav.components],
        "ohne_daten": nav.unbekannt,
        "ersatz": nav.ersatz,
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
            if not any(l.long_distance() for l in trains):
                continue   # reine Nahverkehrskette: Nahverkehrsticket, für ICE/IC später wertlos
            mt = min_transfer(legs)
            if mt is not None and mt < p.min_umstieg:
                continue
            nav = score(legs, h, days, navigator_buffer=p.navigator_puffer)
            strict = score(legs, h, days, navigator_buffer=0)
            pairs.append((nav, strict))
    pairs.sort(key=lambda r: (-r[0].p_kombiniert, -r[1].p_unabhaengig))
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
    knoten: int = 2                 # so viele Knotenbahnhöfe als Zwischenhalt erzwingen (0 = aus)
    baustellen: list[dict] = field(default_factory=list)  # {"name", "stationen": [...], "von", "bis"}
    knoten_seiten: int = 4          # Ergebnisseiten je erzwungenem Zwischenhalt
    zwischenhalte: list[str] = field(default_factory=list)   # eigene erzwungene Zwischenhalte, "A + B" = beide
    verlaengern: int = 4            # so viele wacklige Fernzüge mit Vor- und Nachlauf verlängern (0 = aus)
    verl_seiten: int = 3            # Ergebnisseiten je Verlängerung


def _norm_station(x: str) -> str:
    x = x.lower().replace("hauptbahnhof", "hbf")
    return re.sub(r"[^a-z0-9äöüß]", "", x)


SEV = re.compile(r"\bbus\b|\bsev\b|ersatzverkehr|schienenersatz|^bsv|^bus", re.IGNORECASE)


def construction_hits(legs: list[Leg], d: date, baustellen: list[dict], notices: list[str] = ()) -> list[dict]:
    """Eingetragene Baustellen, die die Kette an diesem Tag befährt: eine
    Teilstrecke hält an mindestens zwei der Baustellen-Bahnhöfe (bei nur einem
    eingetragenen Bahnhof: an diesem).

    Je Treffer: {"name", "eingeplant"}. eingeplant=True heißt, bahn.de hat die
    Baustelle schon im Fahrplan: auf dem Abschnitt fährt ein Bus bzw.
    Ersatzverkehr, oder bahn.de meldet zur Verbindung Bauarbeiten. Dann bringt
    sie nichts mehr, denn die Zugbindung entfällt nur bei einer Änderung nach
    der Buchung. eingeplant=False: dort fährt laut bahn.de der normale Zug."""
    from .bahnde import BAU
    hits = []
    for b in baustellen or []:
        try:
            if not (date.fromisoformat(b["von"]) <= d <= date.fromisoformat(b["bis"])):
                continue
        except Exception:
            continue
        wanted = [_norm_station(x) for x in b.get("stationen") or [] if x.strip()]
        need = min(2, len(wanted))
        if not need:
            continue
        for l in legs:
            if l.walk:
                continue
            names = [_norm_station(n) for n in (l.stops or [l.from_name, l.to_name])]
            matched = set()
            for n in names:   # jeder Halt zählt für den am besten passenden Eintrag ("Lübbenau" vor "Lübben")
                cands = [w for w in wanted if n.startswith(w) or w in n]
                if cands:
                    matched.add(max(cands, key=len))
            if len(matched) >= need:
                bus = l.product.upper() == "BUS" or bool(SEV.search(f"{l.name} {l.operator}"))
                hits.append({"name": b.get("name") or ", ".join(b.get("stationen")),
                             "eingeplant": bus or any(BAU.search(t) for t in notices)})
                break
    return hits


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


# pauschale Wahrscheinlichkeit, dass ein zusätzlicher Umstieg selbst reißt
UMSTIEG_RISIKO = 0.08


def _city(n: str) -> str:
    # "Frankfurt (Main) Hbf" -> "frankfurt(main)", "Berlin-Spandau" -> "berlin"
    m = re.match(r"^([^\W\d_]+)(\s*\([^)]*\))?", n.strip())
    return re.sub(r"\s+", "", m.group(0)).lower() if m else n.lower()


class Corridor:
    """Geometrie der Wunschstrecke S → E und Plausibilität von Ticket-Enden.

    Ein Ticket-Start X ist plausibel, wenn der Weg X → S → E höchstens `umweg`
    (30 %) länger ist als die Luftlinie X → E. Für ein Ticket-Ziel Y
    entsprechend mit S → E → Y. Lage und Richtung spielen sonst keine Rolle
    (Potsdam und Bonn sind für Berlin → Köln plausibel); max_winkel ist
    standardmäßig aus."""

    def __init__(self, h: History, von: str, nach: str, umweg: float = 1.3, max_winkel: float = 180):
        from . import geo
        self.geo = geo
        s_hit, e_hit = h.station_by_name(von), h.station_by_name(nach)
        if not s_hit or not e_hit:
            raise RuntimeError(f"'{von if not s_hit else nach}' nicht in den Verspätungsdaten gefunden.")
        self.s_name, self.e_name = s_hit[1], e_hit[1]
        self.cs, self.ce = geo.coords(self.s_name), geo.coords(self.e_name)
        if not self.cs or not self.ce:
            raise RuntimeError("Koordinaten für die Wunschstrecke nicht gefunden.")
        self.d_se = geo.km(self.cs, self.ce)
        self.umweg, self.max_winkel = umweg, max_winkel
        self.skip = {_city(self.s_name), _city(self.e_name)}

    def check(self, name: str, before: bool, minutes: float | None = None) -> dict:
        """Plausibilität eines Ticket-Starts (before=True) oder -Ziels."""
        geo = self.geo
        c = geo.coords(name)
        if not c:
            return {"ok": False, "grund": "Ort nicht gefunden"}
        if before:
            d_near, d_far = geo.km(c, self.cs), geo.km(c, self.ce)
            ang = geo.angle(c, self.cs, self.cs, self.ce)
        else:
            d_near, d_far = geo.km(c, self.ce), geo.km(c, self.cs)
            ang = geo.angle(self.cs, self.ce, self.ce, c)
        ratio = (d_near + self.d_se) / d_far if d_far else 9
        out = {"km": round(d_near), "umweg": round((ratio - 1) * 100), "winkel": round(ang), "_c": c}
        # allein der Umweg zählt: Potsdam → Berlin → Köln (12 %) oder
        # Berlin → Köln → Bonn (5 %) sind plausibel, Rostock → Berlin → Köln (35 %) nicht
        if ratio > self.umweg:
            out.update(ok=False, grund=f"{out['umweg']} % Umweg")
        elif ang > self.max_winkel:
            out.update(ok=False, grund=f"falsche Richtung ({out['winkel']}°)")
        elif minutes is not None and d_near > 10 + minutes * 3.5:
            out.update(ok=False, grund="Ortsname mehrdeutig")
        else:
            out.update(ok=True, grund="")
        return out


def suggest_endpoints(h: History, von: str, nach: str, progress=_print, limit: int = 14,
                      umweg: float = 1.3, max_winkel: float = 180) -> dict:
    """Bahnhöfe vor dem Wunsch-Start und hinter dem Wunsch-Ziel, über die ein
    Ticket laufen kann, das die Wunschstrecke plausibel einschließt.

    Stufe 1: Bahnhöfe mit direkten Regional- oder Fernzügen zum Start (bzw.
    vom Ziel). Stufe 2: Bahnhöfe, die mit einem Umstieg an einem Bahnhof aus
    Stufe 1 angebunden sind (z. B. Forst über Cottbus). Alle müssen die
    Plausibilitätsprüfung von Corridor bestehen."""
    cor = Corridor(h, von, nach, umweg, max_winkel)
    progress(f"Wunschstrecke: {cor.s_name} → {cor.e_name}")
    st = h.stations()

    def evas_of_city(name):
        # alle Bahnhöfe einer Stadt als Knoten: einzelne Bahnsteige (etwa
        # Berlin Hbf oben) fehlen in den Daten teilweise
        return [e for e, n in st.items() if _city(n) == _city(name)]

    def pick(cands, before: bool, via: str = "", base_minutes: float = 0, budget: int = 45, via_km: int = 0,
             via_c=None, via_risk: float = 0.0):
        out, looked = [], 0
        # häufig angefahren und weiter weg zuerst: Endpunkte von Regionallinien
        # vor deren nahen Zwischenhalten
        for eva, name, rides, mins, late, canc in sorted(cands, key=lambda c: -c[2] * min(c[3], 60)):
            if _city(name) in cor.skip or looked >= budget:
                continue
            looked += 1
            total = base_minutes + mins + (10 if via else 0)
            r = cor.check(name, before, minutes=total)
            if not r.get("ok") or r["km"] < 15 or r["km"] > 180 or (via and r["km"] < via_km + 10):
                continue   # mit Umstieg nur, wenn der Ort weiter weg liegt als sein Knoten
            if via_c is not None:
                d_via = cor.geo.km(r["_c"], via_c)
                # näher am Knoten als an der Wunschstadt (sonst fährt der Zug
                # nur durch sie hindurch), und die Entfernung passt zur Fahrzeit
                if d_via >= r["km"] or d_via > 10 + mins * 2.5:
                    continue
            # Zusatzrisiko dieser Etappe: verspätet (>= 5 min) oder ausgefallen,
            # bei einem Umstieg kombiniert mit der Etappe zum Knoten plus einem
            # pauschalen Risiko für den Umstieg selbst
            leg = 1 - (1 - late) * (1 - canc)
            risk = 1 - (1 - leg) * (1 - via_risk) * (1 - UMSTIEG_RISIKO) if via else leg
            out.append({"name": name, "km": r["km"], "minuten": round(total), "fahrten": rides,
                        "umweg": r["umweg"], "winkel": r["winkel"], "ueber": via, "risiko": round(risk, 3),
                        "_c": r["_c"]})
        return out

    def level(before: bool):
        hub = cor.s_name if before else cor.e_name
        lo, hi = (15, 180) if before else (10, 150)
        first = pick(h.connected(evas_of_city(hub), before, lo, hi), before, budget=70)
        second = []
        # Stufe 2 über die am besten angebundenen Knoten aus Stufe 1
        for k in sorted(first, key=lambda x: -x["fahrten"])[:4]:
            progress(f"… Anschlüsse über {k['name']}")
            cands = h.connected(h.evas_named(k["name"]), before, 15, 120, min_rides=3)
            cands = [c for c in cands if c[1] != hub]
            second += pick(cands, before, via=k["name"], base_minutes=k["minuten"], budget=20, via_km=k["km"],
                           via_c=k["_c"], via_risk=k["risiko"])
        merged, seen = [], []
        for x in sorted(first, key=lambda x: -x["fahrten"] * min(x["minuten"], 120)) + \
                 sorted(second, key=lambda x: -x["fahrten"] * min(x["minuten"], 120)):
            if x["minuten"] > 240 or any(cor.geo.km(x["_c"], o) < 5 for o in seen):
                continue   # Nachbarorte zusammenfassen
            seen.append(x["_c"])
            merged.append(x)
        # Vielfalt: Orte nach Himmelsrichtung (vom Wunschort aus, 45°-Sektoren)
        # gruppieren und reihum je Sektor den besten nehmen, dort abwechselnd
        # direkt angebunden und mit Umstieg. So stehen Aachen und Stolberg,
        # Bonn und Koblenz, Düsseldorf und Duisburg nebeneinander zur Wahl.
        center = cor.cs if before else cor.ce
        sectors: dict[int, list] = {}
        for x in merged:
            sectors.setdefault(int(cor.geo.bearing(center, x["_c"]) // 45), []).append(x)
        for k in sectors:
            d = [x for x in sectors[k] if not x["ueber"]]
            u = [x for x in sectors[k] if x["ueber"]]
            mixed = []
            while d or u:
                if d:
                    mixed.append(d.pop(0))
                if u:
                    mixed.append(u.pop(0))
            sectors[k] = mixed
        order = sorted(sectors, key=lambda k: -len(sectors[k]))
        out = []
        while len(out) < limit and any(sectors[k] for k in order):
            for k in order:
                if sectors[k] and len(out) < limit:
                    out.append(sectors[k].pop(0))
        # die vielfältige Auswahl ordnen: wackligste zuerst, sehr weite Orte
        # etwas schwächer (sie treiben den Preis über die Grenze)
        for x in out:
            x["wertung"] = round(x["risiko"] * min(1.0, 90 / max(x["km"], 1)), 3)
        out.sort(key=lambda x: -x["wertung"])
        for x in out:
            x.pop("_c")
        return out

    progress("Suche Bahnhöfe vor dem Start …")
    before = level(True)
    progress(f"{len(before)} mögliche Ticket-Starts. Suche Bahnhöfe hinter dem Ziel …")
    after = level(False)
    progress(f"{len(after)} mögliche Ticket-Ziele.")
    return {"von": cor.s_name, "nach": cor.e_name, "km": round(cor.d_se), "starts": before, "ziele": after}


def check_endpoint(h: History, von: str, nach: str, name: str, before: bool) -> dict:
    """Plausibilität eines selbst eingetragenen Ticket-Starts oder -Ziels."""
    r = Corridor(h, von, nach).check(name, before)
    r.pop("_c", None)
    return r


def best_days(h: History, p: BestDaysParams, progress=_print, day_done=lambda d, n: None) -> dict:
    """Fragt bahn.de für jede Kombination aus Ticket-Start und Ticket-Ziel und
    jeden Reisetag ab ("Schnelle Verbindungen" aus), behält Verbindungen bis
    max_preis und bewertet sie. Der Wunsch-Start und das Wunsch-Ziel gehören
    immer mit zu den Kombinationen.

    Runde 2 (p.knoten > 0): Aus den Ketten der ersten Runde werden die
    Umstiegsbahnhöfe gewählt, an denen Anschlüsse am häufigsten reißen. Für
    diese Knoten fragt die App bahn.de erneut ab, mit dem Knoten als
    Zwischenhalt. So erscheinen Umwegketten, die bahn.de sonst ausblendet."""
    client = bahnde.Client()
    wunsch_von = _resolve(client, p.von, p.von_id)
    wunsch_nach = _resolve(client, p.nach, p.nach_id)
    starts = [wunsch_von] + [_resolve(client, n, "") for n in p.starts if n.strip()]
    ziele = [wunsch_nach] + [_resolve(client, n, "") for n in p.ziele if n.strip()]
    pairs = [(a, b) for a in starts for b in ziele]
    dates = travel_dates(p)
    progress(f"Wunschstrecke {wunsch_von['name']} → {wunsch_nach['name']}, {len(pairs)} Ticket-Variante{'n' if len(pairs) != 1 else ''}")
    days_hist = h.days()
    found: dict[tuple, dict] = {}
    per_day: dict[str, dict] = {}
    state = {"fehler_folge": 0, "uebersprungen": 0, "abbruch": ""}

    def run(d, a, b, via=None, pages=None, via_list=None, when=None, require=None, stop_after=None, core=""):
        if state["abbruch"]:
            return
        vl = via_list if via_list is not None else ([via] if via else [])
        name = (f"{WD_NAMES[d.weekday()]} {d:%d.%m.} {a['name']} → {b['name']}"
                + (f" über {', '.join(v['name'] for v in vl)}" if vl else "")
                + (f" mit {core}" if core else ""))
        try:
            n_all, n_cheap, cheapest = _scan(client, h, p, a, b, d, days_hist, found, vias=vl, max_pages=pages,
                                             when=when, require=require, stop_after=stop_after, core=core)
        except bahnde.Unreachable as e:
            # einzelne Variante überspringen; erst mehrere Ausfälle hintereinander beenden die Suche
            state["fehler_folge"] += 1
            state["uebersprungen"] += 1
            progress(f"{name}: übersprungen, {e}")
            if state["fehler_folge"] >= 3:
                state["abbruch"] = f"{e} Die Suche wurde nach drei Ausfällen in Folge beendet."
            return
        except bahnde.Blocked as e:
            state["abbruch"] = str(e)
            progress(f"{name}: {e}")
            return
        state["fehler_folge"] = 0
        t = per_day.setdefault(d.isoformat(), {"datum": d.isoformat(), "verbindungen": 0, "bis_max_preis": 0,
                                               "guenstigster": None})
        if require:   # Verlängerungen zählen nicht in die Tagesübersicht
            progress(f"{name}: {n_cheap} neue Kette{'n' if n_cheap != 1 else ''} bis {eur(p.max_preis)}"
                     + (f", günstigste {eur(cheapest)}" if cheapest is not None else ""))
            return
        t["verbindungen"] += n_all
        t["bis_max_preis"] += n_cheap
        if cheapest is not None and (t["guenstigster"] is None or cheapest < t["guenstigster"]):
            t["guenstigster"] = cheapest
        msg = f"{name}: {n_all} Verbindungen, {n_cheap} bis {eur(p.max_preis)}"
        progress(msg + (f", günstigste {eur(cheapest)}" if cheapest is not None else ""))

    for d in dates:
        for a, b in pairs:
            run(d, a, b)
        day_done(d, (per_day.get(d.isoformat()) or {}).get("bis_max_preis", 0))

    hubs = _pick_hubs(found.values(), p.knoten, exclude={_city(x["name"]) for x in starts + ziele}) \
        if p.knoten > 0 and found else []
    # eingetragene Baustellen im Suchzeitraum: Ketten gezielt hindurchführen,
    # über einen Bahnhof in der Mitte der Baustelle
    active = [b for b in p.baustellen if b.get("stationen") and any(
        b.get("von", "") <= d.isoformat() <= b.get("bis", "") for d in dates)]
    bau_vias = [(b["stationen"][len(b["stationen"]) // 2], b["von"], b["bis"]) for b in active]
    # eigene Zwischenhalte: "Stendal" = eine Abfrage über Stendal,
    # "Stendal + Wolfsburg Hbf" = eine Abfrage über beide (bahn.de erlaubt bis zu zwei)
    own = [[n.strip() for n in e.split("+") if n.strip()][:2] for e in p.zwischenhalte if e.strip()]
    if (hubs or bau_vias or own) and not state["abbruch"]:
        progress("Runde 2, erzwungene Zwischenhalte: "
                 + ", ".join([" + ".join(e) + " (eigener)" for e in own]
                             + [f"{n} ({pct:.0%} verpasst)" for n, pct in hubs]
                             + [f"{n} (Baustelle)" for n, _, _ in bau_vias]))
        vias = []   # ([Orte], gültig von, gültig bis); Knoten und eigene gelten immer
        for names, von, bis in ([(e, "", "9999") for e in own] + [([n], "", "9999") for n, _ in hubs]
                                + [([n], v, b) for n, v, b in bau_vias]):
            hits = []
            for name in names:
                try:
                    hit = client.locations(name)
                except bahnde.Blocked as e:
                    hit = []
                    progress(f"Zwischenhalt {name}: {e}")
                if hit:
                    hits.append(hit[0])
                else:
                    progress(f"Zwischenhalt {name} bei bahn.de nicht gefunden, übersprungen.")
            key = [h["id"] for h in hits]
            if hits and key not in [[h["id"] for h in v[0]] for v in vias]:
                vias.append((hits, von, bis))
        for d in dates:
            for a, b in pairs:
                for hits, von, bis in vias:
                    # ein Zwischenhalt am Ticket-Start oder -Ziel ergibt keine neue Kette
                    vl = [v for v in hits if v["id"] not in (a["id"], b["id"])]
                    if vl and von <= d.isoformat() <= bis:
                        run(d, a, b, via_list=vl, pages=p.knoten_seiten)
            day_done(d, 0)

    # Runde 3: Die wackligsten Fernzüge (oft ein einzelner ICE, der fast
    # täglich 20 min zu spät kommt) bekommen Vor- und Nachlauf zu den
    # weiteren Ticket-Enden. bahn.de bietet diese Kombination von sich aus
    # selten an; mit beiden Fernzug-Enden als Zwischenhalt und passender
    # Uhrzeit erscheint sie. Ein knapper Anschluss am Ende des ICE macht die
    # Kette wackliger, meist ohne höheren Preis.
    cores = _pick_cores(found.values(), p.verlaengern) if p.verlaengern > 0 and len(pairs) > 1 else []
    if cores and not state["abbruch"]:
        progress("Runde 3, Ketten verlängern: " + ", ".join(f"{c['label']} ({c['p']:.0%})" for c in cores))
        ids: dict[str, dict | None] = {}

        def loc(name):
            if name not in ids:
                try:
                    hit = client.locations(name)
                except bahnde.Blocked:
                    hit = []
                ids[name] = hit[0] if hit else None
            return ids[name]

        for c in cores:
            board, alight = loc(c["einstieg"]), loc(c["ausstieg"])
            for a, b in pairs:
                if state["abbruch"]:
                    break
                if (a["name"], b["name"]) == (c["ticket_von"], c["ticket_nach"]):
                    continue
                # Zwischenhalte nur, wo das Ticket nicht ohnehin dort beginnt oder endet
                vias = [v for v, end in ((board, a), (alight, b))
                        if v and _city(v["name"]) != _city(end["name"])]
                vor = _city(c["einstieg"]) != _city(a["name"])
                when = c["dep"] - timedelta(minutes=150) if vor else c["dep"] - timedelta(minutes=1)
                run(c["datum"], a, b, via_list=vias, pages=p.verl_seiten, when=when,
                    require=c["require"], stop_after=c["dep"], core=c["label"])
            day_done(c["datum"], 0)

    # Baustellenhinweis als Bonus: dort ist eine spätere Fahrplanänderung,
    # selbst ein Auslöser, am wahrscheinlichsten
    # zuerst Ketten durch eine Baustelle, die bahn.de noch nicht eingeplant hat
    results = sorted(found.values(), key=lambda r: (not any(not g["eingeplant"] for g in r.get("geplant") or []),
                                                     -rank_key(r),
                                                     r["preis"], r["ab"]))
    warnung = []
    if state["abbruch"]:
        warnung.append(f"Suche vorzeitig beendet: {state['abbruch']} Angezeigt sind die bis dahin gefundenen "
                       "Verbindungen.")
    if state["uebersprungen"]:
        n = state["uebersprungen"]
        warnung.append(f"{n} {'Abfrage wurde' if n == 1 else 'Abfragen wurden'} übersprungen, weil bahn.de "
                       "nicht antwortete (Details im Protokoll).")
    # Tage ohne Fahrplan (Fahrplanjahr noch nicht veröffentlicht) und Tage mit
    # Fahrplan, aber ohne einen einzigen Preis (noch nicht im Vorverkauf)
    leer = [t["datum"] for t in per_day.values() if not t["verbindungen"]]
    ohne_preis = [t["datum"] for t in per_day.values() if t["verbindungen"] and t["guenstigster"] is None]
    fmt = lambda ds: ", ".join(f"{d[8:10]}.{d[5:7]}." for d in sorted(ds))
    if ohne_preis:
        warnung.append(f"Für {fmt(ohne_preis)} zeigt bahn.de zwar Verbindungen, aber keinen einzigen Preis. Diese "
                       "Tage sind noch nicht im Vorverkauf. Auf bahn.de nachsehen, bis zu welchem Tag schon Preise "
                       "erscheinen, und die Suche später wiederholen.")
    if leer:
        warnung.append(f"Für {fmt(leer)} liefert bahn.de gar keine Verbindungen. Meist ist der Fahrplan für diese "
                       "Tage noch nicht veröffentlicht (z. B. nach dem Fahrplanwechsel im Dezember).")
    priced = [t for t in per_day.values() if t["guenstigster"] is not None]
    if not found and not state["abbruch"] and priced and not any(t["bis_max_preis"] for t in priced):
        warnung.append(f"An den Tagen mit Preisen gab es keine Verbindung bis {eur(p.max_preis)}. Andere Tage "
                       "oder einen etwas höheren Höchstpreis versuchen; kurz vor der Reise sind die günstigen "
                       "Sparpreise meist ausverkauft.")
    return {"von": wunsch_von["name"], "nach": wunsch_nach["name"], "tage": list(per_day.values()),
            "warnung": warnung,
            "ergebnisse": results, "daten_von": h.first_day.isoformat(), "daten_bis": h.last_day.isoformat(),
            "max_preis": p.max_preis, "varianten": [f"{a['name']} → {b['name']}" for a, b in pairs]}


def _pick_hubs(results, k: int, exclude: set[str]) -> list[tuple[str, float]]:
    """Umstiegsbahnhöfe aus gefundenen Ketten, an denen Anschlüsse am
    häufigsten reißen (gewichtet mit der Häufigkeit des Bahnhofs).
    exclude: Städte (wie _city sie schreibt), deren Bahnhöfe nicht taugen."""
    import math
    stats: dict[str, list[float]] = {}
    for r in results:
        for c in r["risiken"]:
            m = re.match(r"Anschluss (.+) \((-?\d+) min\)$", c["teil"])
            if m and c["anteil"] is not None and c["tage"] >= 5:
                stats.setdefault(m.group(1), []).append(c["anteil"])
    ranked = []
    for name, ps in stats.items():
        # Knoten in der Start- oder Zielstadt erzwingen keinen Umweg
        flat = re.sub(r"\s+", "", name.lower())
        if _city(re.sub(r"^S\+?U? ", "", name)) in exclude or any(c in flat for c in exclude):
            continue
        mean = sum(ps) / len(ps)
        ranked.append((mean * math.log(1 + len(ps)), name, mean))
    ranked.sort(reverse=True)
    return [(name, mean) for _, name, mean in ranked[:k]]


def _pick_cores(results, k: int) -> list[dict]:
    """Die wackligsten Fernzug-Abschnitte aus den bisherigen Treffern, je
    Reisetag und Zugfolge nur einmal. Ein Kern ist die Folge aller Fernzüge
    einer Kette; Vor- und Nachlauf werden in Runde 3 neu gesucht."""
    out, seen = [], set()
    for r in sorted(results, key=lambda r: -r["p_kombiniert"]):
        if r.get("verlaengert") or r["p_kombiniert"] < 0.5:
            continue
        fern = [l for l in r["legs"] if l.get("fern")]
        if not fern:
            continue
        dep = datetime.fromisoformat(fern[0]["ab"])
        key = (dep.date(), tuple(l["name"] for l in fern))
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": " + ".join(l["name"] for l in fern), "p": r["p_kombiniert"], "datum": dep.date(),
                    "dep": dep, "einstieg": fern[0]["von"], "ausstieg": fern[-1]["nach"],
                    "require": {(l["name"], l["ab"]) for l in fern},
                    "ticket_von": r["ticket_von"], "ticket_nach": r["ticket_nach"]})
        if len(out) >= k:
            break
    return out


def _scan(client, h: History, p: BestDaysParams, a: dict, b: dict, d: date, days_hist, found: dict,
          vias: list[dict] = (), max_pages: int | None = None, when: datetime | None = None,
          require: set | None = None, stop_after: datetime | None = None, core: str = ""):
    """Eine Ticket-Variante a → b an einem Tag durchblättern, optional mit
    erzwungenen Zwischenhalten. require: nur Ketten behalten, die alle diese
    Fahrten (Name, Abfahrt) enthalten; stop_after: Blättern beenden, sobald
    die erste Fahrt einer Seite später abfährt."""
    when_s = when.strftime("%Y-%m-%dT%H:%M:00") if when else f"{d.isoformat()}T{p.ab}:00"
    paging, pages, n_all, n_cheap, cheapest = None, 0, 0, 0, None
    while pages < (max_pages or p.max_seiten):
        data = client.journeys(a["id"], b["id"], when_s, p.ermaessigung, paging=paging,
                               vias=[v["id"] for v in vias])
        pages += 1
        vs = data.get("verbindungen") or []
        last_dep = None
        for v in vs:
            legs = bahnde.verbindung_to_legs(v)
            trains = [l for l in legs if not l.walk]
            if not trains or bahnde.cancelled(v):
                continue
            last_dep = trains[0].dep
            if require is not None:
                if not require <= {(l.name, l.dep.isoformat()) for l in trains}:
                    continue
            elif trains[0].dep.date() != d or trains[0].dep.strftime("%H:%M") > p.bis:
                continue
            # FlixTrain und andere Nicht-DB-Züge: mit einem DB-Sparpreis nicht nutzbar
            if any(EXCLUDE.search(f"{l.name} {l.operator}") for l in trains):
                continue
            # ohne Fernzug ist es ein Nahverkehrsticket: nach aufgehobener
            # Zugbindung nur für Nahverkehrszüge gültig, für den ICE wertlos
            if not any(l.long_distance() for l in trains):
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
            hinweise = bahnde.notices(v)
            geplant = construction_hits(legs, d, p.baustellen, hinweise)
            r.update(preis=price, ticket_von=a["name"], ticket_nach=b["name"],
                     ueber=", ".join(v["name"] for v in vias) if vias and not core else "",
                     verlaengert=core, hinweise=hinweise, baustelle=any(bahnde.BAU.search(t) for t in hinweise),
                     geplant=geplant)
            for v in reversed(vias):
                if v["name"] not in r["zwischenhalte"]:
                    r["zwischenhalte"] = [v["name"]] + r["zwischenhalte"]
            r["zwischenhalte"] = r["zwischenhalte"][:2]
            found[key] = r
        paging = (data.get("verbindungReference") or {}).get("later")
        if stop_after is not None:
            if not vs or not paging or (last_dep and last_dep > stop_after):
                break
        elif not vs or not paging or (last_dep and (last_dep.date() > d or last_dep.strftime("%H:%M") > p.bis)):
            break
    return n_all, n_cheap, cheapest
