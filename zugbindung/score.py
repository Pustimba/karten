"""Wahrscheinlichkeit, dass die Zugbindung einer Verbindung entfällt.

Auslöser laut DB-FAQ "Zugbindung aufgehoben":
  * Ausfall eines Zuges oder Halts der Verbindung
  * verpasster Anschluss
  * voraussichtlich mindestens 20 Minuten Verspätung am Ziel
  * Fahrplanänderung mit früherer Abfahrt (aus Echtzeitdaten nicht messbar)

Für jeden historischen Tag wird die Verbindung mit den echten Verspätungen
dieses Tages nachgespielt. Daraus entstehen zwei Kennzahlen:

  p_unabhaengig  1 - Produkt(1 - p_i) über alle Einzelrisiken, wobei jedes
                 Einzelrisiko auf allen Tagen mit Daten für genau diesen Teil
                 geschätzt wird. Nutzt viele Tage, nimmt aber Unabhängigkeit an.
  tage_mit/tage  echte Tagessimulation, nur Tage, an denen jeder Teil der
                 Kette Daten hat. Ehrlicher, aber oft nur wenige Tage.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

from .history import History
from .model import Leg

MIN_DAYS = 5          # weniger Tage mit Daten: Einzelrisiko gilt als unbekannt
TARGET_DELAY = 20     # Minuten Verspätung am Ziel, ab der die Zugbindung entfällt


@dataclass
class Component:
    label: str
    events: list          # pro historischem Tag True/False/None
    p: float | None = None
    n: int = 0

    def __post_init__(self):
        known = [e for e in self.events if e is not None]
        self.n = len(known)
        self.p = sum(known) / self.n if self.n else None


@dataclass
class Result:
    legs: list[Leg]
    components: list[Component]
    navigator_buffer: int
    p_unabhaengig: float = 0.0
    tage_mit: int = 0
    tage: int = 0
    unbekannt: list[str] = field(default_factory=list)

    @property
    def transfers(self) -> int:
        return max(0, len([l for l in self.legs if not l.walk]) - 1)

    def drivers(self, threshold: float = 0.1) -> list[Component]:
        return [c for c in self.components if c.p is not None and c.n >= MIN_DAYS and c.p >= threshold]


def drop_planned_blocks(s: dict[date, tuple[int, bool]], min_run: int = 3) -> dict:
    """Ausfälle an min_run oder mehr Tagen am Stück sind fast immer geplante
    Bauarbeiten. Die stehen bei der Buchung schon im Fahrplan und sagen nichts
    über spontane Ausfälle aus, deshalb fallen diese Tage weg."""
    canc = sorted(d for d, (_, c) in s.items() if c)
    canc_set, block = set(canc), set()
    for d in canc:
        run = [d]
        while run[-1] + timedelta(days=1) in canc_set:
            run.append(run[-1] + timedelta(days=1))
        if len(run) >= min_run:
            block.update(run)
    return {d: v for d, v in s.items() if d not in block}


class _TrainLeg:
    """Eine Zugfahrt mit ihren historischen Reihen an Start- und Zielhalt."""

    def __init__(self, leg: Leg, hist: History, start_day: date):
        self.leg = leg
        self.dep_off = (leg.dep.date() - start_day).days
        self.arr_off = (leg.arr.date() - start_day).days
        a = hist.find_stop(leg.num, leg.line, "arrival", leg.arr, leg.to_name, leg.to_eva)
        d = hist.find_stop(leg.num, leg.line, "departure", leg.dep, leg.from_name, leg.from_eva)
        self.arr = drop_planned_blocks(hist.series(a[0], a[1], "arrival", leg.arr)) if a else {}
        self.dep = drop_planned_blocks(hist.series(d[0], d[1], "departure", leg.dep)) if d else {}

    def arr_on(self, day: date):
        return self.arr.get(day + timedelta(days=self.arr_off))

    def dep_on(self, day: date):
        return self.dep.get(day + timedelta(days=self.dep_off))


def score(legs: list[Leg], hist: History, days: list[date], navigator_buffer: int = 3) -> Result:
    """navigator_buffer: ein Anschluss gilt als verpasst, sobald weniger als so
    viele Minuten Umstiegszeit bleiben (0 = Anschluss tatsächlich weg)."""
    trains_idx = [i for i, l in enumerate(legs) if not l.walk]
    start_day = legs[trains_idx[0]].dep.date()
    T = [_TrainLeg(legs[i], hist, start_day) for i in trains_idx]
    comps: list[Component] = []

    # 1. Ausfall an Start- oder Zielhalt jeder Fahrt
    for t in T:
        ev = []
        for d in days:
            a, dp = t.arr_on(d), t.dep_on(d)
            if a is None and dp is None:
                ev.append(None)
            else:
                ev.append(bool((a and a[1]) or (dp and dp[1])))
        comps.append(Component(f"Ausfall {t.leg.name}", ev))

    # 2. Anschlüsse
    for k in range(len(T) - 1):
        a, b = T[k], T[k + 1]
        ia, ib = trains_idx[k], trains_idx[k + 1]
        walk = sum((w.arr - w.dep).total_seconds() / 60 for w in legs[ia + 1:ib] if w.walk)
        buf = (b.leg.dep - a.leg.arr).total_seconds() / 60 - walk
        need = min(navigator_buffer, max(buf, 0))
        ev = []
        for d in days:
            arr = a.arr_on(d)
            if arr is None or arr[1]:
                ev.append(None)
                continue
            dep = b.dep_on(d) or (0, False)
            if dep[1]:
                ev.append(None)
                continue
            ev.append(buf + max(dep[0], 0) - arr[0] < need)
        comps.append(Component(f"Anschluss {a.leg.to_name} ({buf:.0f} min)", ev))

    # 3. Verspätung am Ziel
    last = T[-1]
    ev = []
    for d in days:
        arr = last.arr_on(d)
        ev.append(None if arr is None or arr[1] else arr[0] >= TARGET_DELAY)
    comps.append(Component(f">= {TARGET_DELAY} min am Ziel", ev))

    res = Result(legs=legs, components=comps, navigator_buffer=navigator_buffer)
    stay = 1.0
    for c in comps:
        if c.p is not None and c.n >= MIN_DAYS:
            stay *= 1 - c.p
        else:
            res.unbekannt.append(c.label)
    res.p_unabhaengig = 1 - stay
    # Teile ganz ohne Daten (z. B. Busse) blockieren die Tagessimulation nicht,
    # sie stehen in res.unbekannt und zählen einfach nicht mit
    usable = [c for c in comps if c.n >= MIN_DAYS]
    full = [any(c.events[i] for c in usable) for i in range(len(days))
            if all(c.events[i] is not None for c in usable)]
    res.tage_mit, res.tage = sum(full), len(full)
    return res


def min_transfer(legs: list[Leg]) -> float | None:
    """Kürzeste Umstiegszeit (ohne Fußwege) in Minuten."""
    trains_idx = [i for i, l in enumerate(legs) if not l.walk]
    out = []
    for ia, ib in zip(trains_idx, trains_idx[1:]):
        walk = sum((w.arr - w.dep).total_seconds() / 60 for w in legs[ia + 1:ib] if w.walk)
        out.append((legs[ib].dep - legs[ia].arr).total_seconds() / 60 - walk)
    return min(out) if out else None
