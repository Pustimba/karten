"""Eine auf bahn.de geteilte Verbindung (Link mit ?vbid=...) einlesen.

bahn.de liefert unter /web/api/angebote/verbindung/<vbid> den "Recon"-String
der Verbindung. Er enthält jede Teilstrecke mit Start- und Zielhalt (inklusive
EVA-Nummer), Sollzeiten und Zugnummer. Mehr braucht die Bewertung nicht.

bahn.de steht hinter Akamai Bot Manager. curl_cffi ahmt den TLS-Fingerabdruck
eines echten Browsers nach. Von einem normalen Heimanschluss klappt das in der
Regel, von Cloud-Servern wird es oft blockiert (HTTP 403 OPS_BLOCKED).
"""
import base64
import re
from datetime import datetime

from .model import Leg

API = "https://www.bahn.de/web/api/angebote/verbindung/"
PROFILES = ["firefox135", "safari17_0", "chrome"]


def vbid_from(text: str) -> str:
    m = re.search(r"vbid=([0-9a-f-]{36})", text) or re.search(r"\b([0-9a-f]{8}-[0-9a-f-]{27})\b", text)
    if not m:
        raise SystemExit(f"Keine vbid in '{text}' gefunden.")
    return m.group(1)


def fetch(vbid: str) -> dict:
    from curl_cffi import requests  # erst hier, damit die Suche ohne curl_cffi läuft

    last = None
    for prof in PROFILES:
        try:
            r = requests.get(API + vbid, impersonate=prof, timeout=30,
                             headers={"Accept": "application/json"})
        except Exception as e:
            last = e
            continue
        if 200 <= r.status_code < 300:
            return r.json()
        last = f"HTTP {r.status_code}: {r.text[:120]}"
    raise SystemExit(f"bahn.de hat die Verbindung nicht geliefert ({last}).")


def _loc(s: str) -> tuple[str, str]:
    name = re.search(r"O=([^@]*)", s)
    eva = re.search(r"L=(\d+)", s)
    return (name.group(1) if name else "?"), (eva.group(1).rjust(8, "0") if eva else "")


def _categories(recon: str) -> list[str]:
    """Zuggattungen aus dem base64-Block hinter ¶KCC¶, in Fahrtreihenfolge."""
    try:
        blob = recon.split("¶KCC¶")[1].split("¶")[0]
        text = base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("latin1")
    except Exception:
        return []
    cats = []
    for line in text.split("\n"):
        if line.startswith("Z#"):
            m = re.search(r"#CA#([^#]*)#", line)
            cat = m.group(1) if m else ""
            # DB-intern: DRB/NRB = RB, NRE/DRE = RE, Bsv = Bus
            cat = re.sub(r"^[DN](?=R[BE]$)", "", cat)
            cats.append({"Bsv": "Bus"}.get(cat, cat))
    return cats


def recon_to_legs(recon: str) -> list[Leg]:
    body = recon.split("¶HKI¶", 1)[1].split("¶", 1)[0]
    cats = iter(_categories(recon))
    legs = []
    for seg in body.split("§"):
        parts = seg.split("$")
        if len(parts) < 6:
            continue
        kind, a, b, t0, t1, name = parts[:6]
        from_name, from_eva = _loc(a)
        to_name, to_eva = _loc(b)
        dep = datetime.strptime(t0, "%Y%m%d%H%M")
        arr = datetime.strptime(t1, "%Y%m%d%H%M")
        if kind == "W":
            legs.append(Leg("Fußweg", "", "", from_name, to_name, dep, arr, walk=True))
            continue
        tokens = name.split()
        num = tokens[-1].lstrip("0") if tokens else ""
        cat = next(cats, "") or (tokens[0] if len(tokens) > 1 else "")
        num_digits = num if num.isdigit() else ""
        line = "" if num_digits else num        # Bus "X1" oder S-Bahn "S5": Linie statt Nummer
        legs.append(Leg(f"{cat} {num}".strip(), num_digits, line, from_name, to_name, dep, arr,
                        from_eva=from_eva, to_eva=to_eva, product=cat.upper()))
    return legs


# ---------------------------------------------------------------- Fahrplan mit Preisen
#
# Gleiche Anfrage wie die bahn.de-Suchmaske (und wie delay_bahn sie stellt).
# Von einem Heimanschluss klappt das in der Regel; Cloud-IPs blockt Akamai.
API_BASE = "https://www.bahn.de/web/api"
HEADERS = {
    "Accept": "application/json",
    # ohne Accept-Language antwortet bahn.de seit 09/2026 mit 403 OPS_BLOCKED
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
ALL_PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]
DISCOUNTS = {
    "keine": ("KEINE_ERMAESSIGUNG", "KLASSENLOS"),
    "bc25": ("BAHNCARD25", "KLASSE_2"),
    "bc50": ("BAHNCARD50", "KLASSE_2"),
}


class Blocked(Exception):
    """bahn.de verweigert den Zugriff (403 oder anhaltend 429)."""


class Client:
    """Kleiner, höflicher bahn.de-Client: eine Sitzung, Pausen zwischen den
    Anfragen, Profilwechsel bei 403, einmaliges Warten bei 429."""

    def __init__(self, pause: float = 2.0):
        self.pause = pause
        self._prof = 0
        self._session = None
        self._last = 0.0

    def _sess(self):
        from curl_cffi import requests
        if self._session is None:
            self._session = requests.Session(impersonate=PROFILES[self._prof], headers=HEADERS, timeout=30)
        return self._session

    def _call(self, method: str, path: str, **kw) -> dict | list:
        import random
        import time
        tried_429 = False
        while True:
            wait = self.pause + random.uniform(0, 0.8) - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = getattr(self._sess(), method)(API_BASE + path, **kw)
            except Exception as e:
                raise Blocked(f"bahn.de nicht erreichbar ({e.__class__.__name__})")
            if 200 <= r.status_code < 300:   # die Fahrplansuche antwortet mit 201 Created
                try:
                    return r.json()
                except Exception:
                    raise Blocked(f"bahn.de hat eine unlesbare Antwort geschickt (HTTP {r.status_code}).")
            if r.status_code == 403:
                self._prof += 1
                self._session = None
                if self._prof >= len(PROFILES):
                    raise Blocked("bahn.de blockiert die Anfragen (403). Von einem Heimanschluss ohne VPN "
                                  "klappt es meist; sonst später erneut versuchen.")
                continue
            if r.status_code == 429 and not tried_429:
                tried_429 = True
                time.sleep(min(60, float(r.headers.get("Retry-After") or 20)))
                continue
            if r.status_code == 429:
                raise Blocked("bahn.de bremst die Anfragen (429). Bitte einige Minuten warten.")
            raise Blocked(f"bahn.de antwortet mit Fehler {r.status_code}.")

    def locations(self, query: str) -> list[dict]:
        res = self._call("get", "/reiseloesung/orte", params={"suchbegriff": query, "typ": "ALL", "limit": 8})
        return [{"id": r["id"], "eva": str(r.get("extId", "")), "name": r["name"]}
                for r in res or [] if r.get("id") and r.get("name")]

    def journeys(self, from_id: str, to_id: str, when: str, discount: str = "bc25",
                 paging: str | None = None, fast: bool = False, vias: list[str] = ()) -> dict:
        art, klasse = DISCOUNTS.get(discount, DISCOUNTS["bc25"])
        body = {
            "abfahrtsHalt": from_id, "ankunftsHalt": to_id, "anfrageZeitpunkt": when,
            "ankunftSuche": "ABFAHRT", "klasse": "KLASSE_2", "produktgattungen": ALL_PRODUCTS,
            "reisende": [{"typ": "ERWACHSENER", "ermaessigungen": [{"art": art, "klasse": klasse}],
                          "alter": [], "anzahl": 1}],
            # aus: langsame Nahverkehrsketten sollen ausdrücklich mit in die Liste
            "schnelleVerbindungen": fast, "sitzplatzOnly": False, "bikeCarriage": False,
            "reservierungsKontingenteVorhanden": False, "deutschlandTicketVorhanden": False,
            "nurDeutschlandTicketVerbindungen": False,
        }
        if vias:
            body["zwischenhalte"] = [{"id": v, "aufenthaltsdauer": 0} for v in vias]
        if paging:
            body["pagingReference"] = paging
        return self._call("post", "/angebote/fahrplan", json=body)


def _time(a: dict, key: str) -> str | None:
    """Sollzeit eines Abschnitts; zwei bekannte Antwortformate."""
    obj = a.get(key) or {}
    if isinstance(obj, dict) and obj.get("sollzeit"):
        return obj["sollzeit"]
    return a.get(f"{key}sZeitpunkt") or a.get(f"{key}Zeitpunkt")


def verbindung_to_legs(v: dict) -> list[Leg]:
    legs = []
    for a in v.get("verbindungsAbschnitte") or []:
        vm = a.get("verkehrsmittel") or {}
        t0, t1 = _time(a, "abfahrt"), _time(a, "ankunft")
        if not t0 or not t1:
            continue
        dep = datetime.fromisoformat(t0[:19])
        arr = datetime.fromisoformat(t1[:19])
        from_name, to_name = a.get("abfahrtsOrt") or "?", a.get("ankunftsOrt") or "?"
        from_eva = str(a.get("abfahrtsOrtExtId") or "").rjust(8, "0") if a.get("abfahrtsOrtExtId") else ""
        to_eva = str(a.get("ankunftsOrtExtId") or "").rjust(8, "0") if a.get("ankunftsOrtExtId") else ""
        if vm.get("typ") != "PUBLICTRANSPORT":
            legs.append(Leg("Fußweg", "", "", from_name, to_name, dep, arr, walk=True))
            continue
        nummer = str(vm.get("nummer") or "").strip()
        name = (vm.get("mittelText") or vm.get("name") or nummer).strip()
        num = nummer.lstrip("0") if nummer.isdigit() else ""
        if not nummer:
            m = re.search(r"(\d+)\s*$", vm.get("name") or "")
            num = m.group(1).lstrip("0") if m else ""
        line = "" if nummer.isdigit() else nummer          # S-Bahn "S5", Bus "X1"
        if num and num not in name:
            name = f"{name} ({num})"
        operator = next((z.get("value") for z in vm.get("zugattribute") or [] if z.get("key") == "BEF"), "")
        legs.append(Leg(name, num, line, from_name, to_name, dep, arr,
                        from_eva=from_eva, to_eva=to_eva, operator=operator or "",
                        product=vm.get("produktGattung") or ""))
    return legs


def price_of(v: dict) -> float | None:
    p = (v.get("angebotsPreis") or {}).get("betrag")
    return float(p) if p is not None else None


def cancelled(v: dict) -> bool:
    if any(n.get("key") == "text.realtime.connection.cancelled" for n in v.get("risNotizen") or []):
        return True
    return any(a.get("originCancelled") or a.get("destinationCancelled")
               for a in v.get("verbindungsAbschnitte") or []
               if (a.get("verkehrsmittel") or {}).get("typ") == "PUBLICTRANSPORT")


BAU = re.compile(r"bau|ersatzverkehr|\bsev\b|fahrplanänderung|umleitung|gleisarbeit|streckensperrung|"
                 r"abweichend|geänderte fahrzeit|zugausfall|fällt aus", re.IGNORECASE)


def notices(v: dict) -> list[str]:
    """Hinweistexte, die bahn.de zur Verbindung mitliefert (Bauarbeiten,
    Ersatzverkehr, Änderungen), aus allen bekannten Feldern."""
    out = []

    def collect(obj):
        for key in ("himMeldungen", "priorisierteMeldungen", "meldungenAsObject", "risNotizen", "meldungen"):
            for m in obj.get(key) or []:
                if isinstance(m, dict):
                    t = " ".join(str(m.get(f) or "") for f in ("ueberschrift", "text", "value")).strip()
                elif isinstance(m, str):
                    t = m
                else:
                    t = ""
                if t and t not in out:
                    out.append(t)

    collect(v)
    for a in v.get("verbindungsAbschnitte") or []:
        collect(a)
    return out
