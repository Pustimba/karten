"""Koordinaten von Bahnhöfen (über Transitous) und Abstände.

Koordinaten werden in ~/.cache/zugbindung/koordinaten.json zwischengespeichert,
damit jeder Bahnhof nur einmal nachgeschlagen wird.
"""
import json
import math
import re
from pathlib import Path

from . import transitous

CACHE = Path.home() / ".cache" / "zugbindung" / "koordinaten.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save():
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def coords(name: str) -> tuple[float, float] | None:
    """(Breite, Länge) eines Bahnhofs, oder None."""
    c = _load()
    if name in c:
        return tuple(c[name]) if c[name] else None
    words = re.findall(r"[^\W\d_]+", name.lower())
    city = words[0] if words else ""
    res = []
    for q in (name, f"{name} Bahnhof"):
        res = [r for r in (transitous._get("/v1/geocode", {"text": q, "type": "STOP"}) or [])
               if r.get("lat") is not None and r.get("name", "").lower().startswith(city)]
        if res:
            break
    found = (res[0]["lat"], res[0]["lon"]) if res else None
    c[name] = found
    _save()
    return found


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def angle(frm: tuple[float, float], to: tuple[float, float], frm2: tuple[float, float], to2: tuple[float, float]) -> float:
    """Winkel in Grad zwischen den Richtungen frm → to und frm2 → to2
    (flache Näherung, für Entfernungen innerhalb Deutschlands ausreichend)."""
    lat0 = math.radians((frm[0] + to[0] + frm2[0] + to2[0]) / 4)

    def vec(a, b):
        return ((b[1] - a[1]) * math.cos(lat0) * 111.32, (b[0] - a[0]) * 110.57)

    (x1, y1), (x2, y2) = vec(frm, to), vec(frm2, to2)
    n = math.hypot(x1, y1) * math.hypot(x2, y2)
    if n == 0:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, (x1 * x2 + y1 * y2) / n))))


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Himmelsrichtung von a nach b in Grad (0 = Norden, 90 = Osten)."""
    lat0 = math.radians((a[0] + b[0]) / 2)
    dx = (b[1] - a[1]) * math.cos(lat0)
    dy = b[0] - a[0]
    return math.degrees(math.atan2(dx, dy)) % 360
