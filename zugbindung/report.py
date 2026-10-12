"""Ausgabe als Markdown-Tabelle, Detailtext und JSON."""
import json

from .score import Result


WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def day(dt) -> str:
    return f"{WD[dt.weekday()]} {dt:%d.%m.}"


def pct(x: float | None) -> str:
    return "?" if x is None else f"{x:.0%}"


def chain(res: Result) -> str:
    return " → ".join(f"{l.name} ({l.from_name} {l.dep:%H:%M})" for l in res.legs if not l.walk)


def detail(res: Result, nav: Result | None = None, title: str = "") -> str:
    """Ausführliche Textansicht einer Verbindung. nav ist das Ergebnis mit
    Navigator-Puffer, res das strenge (oder umgekehrt, je nach Aufruf)."""
    out = []
    if title:
        out.append(title)
    first = next(l for l in res.legs if not l.walk)
    last = [l for l in res.legs if not l.walk][-1]
    out.append(f"Abfahrt {day(first.dep)}{first.dep:%Y %H:%M} {first.from_name}, Ankunft {last.arr:%d.%m. %H:%M} "
               f"{last.to_name}, {res.transfers} Umstiege")
    for l in res.legs:
        out.append(f"   {'  ' if l.walk else ''}{l.label()}")
    if nav is not None:
        out.append(f"Zugbindung entfällt: {pct(nav.p_kombiniert)} (kombiniert); Einzelrisiken {pct(nav.p_unabhaengig)} "
                   f"(Navigator, Restumstieg < {nav.navigator_buffer} min) / {pct(res.p_unabhaengig)} (streng)")
        if not any(l.long_distance() for l in res.legs):
            out.append("ACHTUNG: kein Fernzug in der Kette, also ein Nahverkehrsticket; nach aufgehobener "
                       "Zugbindung nicht für ICE/IC gültig.")
        out.append(f"Tagessimulation: Navigator {nav.tage_mit}/{nav.tage} Tage, streng {res.tage_mit}/{res.tage} Tage")
    base = nav or res
    out.append("Einzelrisiken (Anteil der Tage, Tage mit Daten):")
    for c in base.components:
        out.append(f"   {c.label:<55} {pct(c.p):>5}  ({c.n} Tage)")
    if base.unbekannt:
        out.append("Ohne ausreichende Daten (zählen nicht mit): " + "; ".join(base.unbekannt))
    return "\n".join(out)


def markdown_table(rows: list[tuple[Result, Result]], header: str) -> str:
    lines = [header, "",
             "| # | entfällt (Navigator) | entfällt (streng) | Tagessimulation | Wochentag | Ab | An | Umst. | Hauptrisiken | Kette |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, (nav, strict) in enumerate(rows, 1):
        trains = [l for l in nav.legs if not l.walk]
        drivers = "; ".join(f"{c.label} {pct(c.p)}" for c in nav.drivers(0.15))
        lines.append(
            f"| {i} | {pct(nav.p_unabhaengig)} | {pct(strict.p_unabhaengig)} | {nav.tage_mit}/{nav.tage} | "
            f"{day(trains[0].dep)} | {trains[0].dep:%H:%M} | {trains[-1].arr:%H:%M} | {nav.transfers} | "
            f"{drivers} | {chain(nav)} |")
    return "\n".join(lines) + "\n"


def to_json(rows: list[tuple[Result, Result]]) -> str:
    data = []
    for nav, strict in rows:
        data.append({
            "p_navigator": round(nav.p_unabhaengig, 3),
            "p_streng": round(strict.p_unabhaengig, 3),
            "tagessimulation_navigator": [nav.tage_mit, nav.tage],
            "tagessimulation_streng": [strict.tage_mit, strict.tage],
            "legs": [{"name": l.name, "von": l.from_name, "ab": l.dep.isoformat(), "nach": l.to_name,
                      "an": l.arr.isoformat(), "fussweg": l.walk} for l in nav.legs],
            "risiken": [{"teil": c.label, "anteil": c.p, "tage": c.n} for c in nav.components],
            "ohne_daten": nav.unbekannt,
        })
    return json.dumps(data, ensure_ascii=False, indent=1)
