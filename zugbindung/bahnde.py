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
        if r.status_code == 200:
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
                        from_eva=from_eva, to_eva=to_eva))
    return legs
