"""Kommandozeile: zugbindung db-bauen | suche | pruefe"""
import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DELAY_BAHN = Path(os.environ.get("DELAY_BAHN_DIR", REPO.parent / "delay_bahn"))
DEFAULT_DB = Path(os.environ.get("ZUGBINDUNG_DB", DEFAULT_DELAY_BAHN / "data" / "delays.duckdb"))

# Züge, die sich nicht mit einem DB-Sparpreis buchen lassen (FlixTrain, Nachtzüge
# anderer Bahnen, ausländische Betreiber). Treffer im Namen oder Betreiber.
EXCLUDE = re.compile(r"flix|\bFLX|nightjet|\bNJ\b|\bEN\b|\b\d+N\b|european sleeper|\bDRF\b|"
                     r"\bRDC\b|\bIntercity\b|\bSprinter\b|\bStoptrein\b|\bThalys\b|Eurostar",
                     re.IGNORECASE)

WEEKDAYS = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}


def _history(args):
    from .history import History
    db = Path(args.db)
    if not db.exists():
        sys.exit(f"Verspätungsdatenbank {db} fehlt. Zuerst 'uv run zugbindung db-bauen' ausführen "
                 f"oder mit --db den Pfad angeben.")
    h = History(str(db))
    print(f"Verspätungsdaten: {h.first_day:%d.%m.%Y} bis {h.last_day:%d.%m.%Y}", file=sys.stderr)
    return h


def _days(h, args, travel_day: date | None = None):
    wd = None
    if args.wochentage:
        wd = {WEEKDAYS[w.strip().lower()[:2]] for w in args.wochentage.split(",")}
    elif args.gleicher_wochentag and travel_day:
        wd = {travel_day.weekday()}
    days = h.days(wd)
    if len(days) < 4:
        print(f"Achtung: nur {len(days)} historische Tage passen zum Filter.", file=sys.stderr)
    return days


# ---------------------------------------------------------------- db-bauen
def cmd_db_bauen(args):
    db_dir = Path(args.delay_bahn)
    # delay_bahn hat eine eigene Umgebung; die dieses Tools darf nicht durchsickern
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    if not db_dir.exists():
        print(f"Klone delay_bahn nach {db_dir} ...")
        subprocess.run(["git", "clone", "--recurse-submodules", "--shallow-submodules", "--depth", "1",
                        "https://github.com/sha2nkt/delay_bahn.git", str(db_dir)], check=True, env=env)
    else:
        subprocess.run(["git", "submodule", "update", "--init", "--depth", "1"], cwd=db_dir, check=True, env=env)
    subprocess.run(["uv", "sync"], cwd=db_dir, check=True, env=env)
    end = args.bis or (datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=1)).isoformat()
    env["PYTHONPATH"] = os.pathsep.join([".", "pipeline"])
    print(f"Lade und parse {args.tage} Tage bis {end} mit {args.prozesse} Prozessen ...")
    subprocess.run(["uv", "run", "python", str(REPO / "tools" / "parallel_parse.py"),
                    "--tage", str(args.tage), "--bis", end, "--prozesse", str(args.prozesse)],
                   cwd=db_dir, env=env, check=True)
    print("Führe die Tage zusammen ...")
    subprocess.run(["uv", "run", "python", "pipeline/build_delay_db.py", "--days", str(args.tage),
                    "--end-date", end], cwd=db_dir, env=env, check=True)
    subprocess.run(["uv", "run", "python", "pipeline/merge_delays.py"], cwd=db_dir, env=env, check=True)
    print(f"Fertig: {db_dir / 'data' / 'delays.duckdb'}")


# ---------------------------------------------------------------- suche
def cmd_suche(args):
    from . import report, transitous
    from .score import min_transfer, score

    h = _history(args)
    from_id, from_name = transitous.find_stop(args.von)
    to_id, to_name = transitous.find_stop(args.nach)
    print(f"Suche {from_name} → {to_name}", file=sys.stderr)
    modes = [m.strip() for m in args.modi.split(",")]

    rows = []
    for day in args.datum:
        travel_day = date.fromisoformat(day)
        days = _days(h, args, travel_day)
        conns = transitous.collect(from_id, to_id, day, args.ab, args.bis, args.raster, modes,
                                   max_transfers=args.max_umstiege)
        for legs in conns:
            trains = [l for l in legs if not l.walk]
            if not trains:
                continue
            if not args.alle_betreiber and any(EXCLUDE.search(f"{l.name} {l.operator}") for l in trains):
                continue
            mt = min_transfer(legs)
            if mt is not None and mt < args.min_umstieg:
                continue
            nav = score(legs, h, days, navigator_buffer=args.navigator_puffer)
            strict = score(legs, h, days, navigator_buffer=0)
            rows.append((nav, strict))

    rows.sort(key=lambda r: (-r[0].p_unabhaengig, -r[1].p_unabhaengig))
    out = Path(args.ausgabe)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    slug = re.sub(r"\W+", "_", f"{from_name}_{to_name}").strip("_")
    header = (f"# Rangliste {from_name} → {to_name}\n\n"
              f"Reisetage: {', '.join(args.datum)}. Verspätungsdaten {h.first_day:%d.%m.%Y} bis "
              f"{h.last_day:%d.%m.%Y}. Navigator-Puffer {args.navigator_puffer} min, "
              f"Mindestumstieg {args.min_umstieg} min. Erstellt {datetime.now():%d.%m.%Y %H:%M}.")
    md = out / f"rangliste_{slug}_{stamp}.md"
    md.write_text(report.markdown_table(rows, header), encoding="utf-8")
    (out / f"rangliste_{slug}_{stamp}.json").write_text(report.to_json(rows), encoding="utf-8")

    print(f"\n{len(rows)} Verbindungen bewertet. Die besten {min(args.top, len(rows))}:\n")
    for i, (nav, strict) in enumerate(rows[:args.top], 1):
        print(report.detail(strict, nav, title=f"#{i}"))
        print()
    print(f"Tabelle: {md}")


# ---------------------------------------------------------------- pruefe
def cmd_pruefe(args):
    from . import bahnde, report
    from .score import score

    h = _history(args)
    for text in args.links:
        vbid = bahnde.vbid_from(text)
        data = bahnde.fetch(vbid)
        legs = bahnde.recon_to_legs(data["hinfahrtRecon"])
        days = _days(h, args, legs[0].dep.date())
        nav = score(legs, h, days, navigator_buffer=args.navigator_puffer)
        strict = score(legs, h, days, navigator_buffer=0)
        print(report.detail(strict, nav, title=f"=== {data.get('startOrt')} → {data.get('zielOrt')} (vbid {vbid})"))
        if data.get("rueckfahrtRecon"):
            print("(Rückfahrt im Link wird ignoriert)")
        print()


def main(argv=None):
    p = argparse.ArgumentParser(prog="zugbindung", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("db-bauen", help="Verspätungsdatenbank der letzten Wochen bauen (einmalig, ca. 40 min)")
    b.add_argument("--delay-bahn", default=str(DEFAULT_DELAY_BAHN), help="Pfad für den delay_bahn-Checkout")
    b.add_argument("--tage", type=int, default=31, help="Anzahl Tage (Standard 31)")
    b.add_argument("--bis", help="letzter Tag JJJJ-MM-TT (Standard: gestern)")
    b.add_argument("--prozesse", type=int, default=max(1, min(4, os.cpu_count() or 1)),
                   help="parallele Prozesse beim Parsen (je ca. 1,5 GB RAM)")
    b.set_defaults(func=cmd_db_bauen)

    def common(sp):
        sp.add_argument("--db", default=str(DEFAULT_DB), help="Pfad zu delays.duckdb")
        sp.add_argument("--navigator-puffer", type=int, default=3,
                        help="Anschluss gilt als verpasst, wenn weniger Minuten bleiben (Standard 3)")
        sp.add_argument("--wochentage", help="nur diese historischen Wochentage nutzen, z. B. 'sa,so'")
        sp.add_argument("--gleicher-wochentag", action="store_true",
                        help="nur historische Tage mit demselben Wochentag wie die Reise nutzen")

    s = sub.add_parser("suche", help="Verbindungen über Transitous suchen und bewerten")
    s.add_argument("--von", required=True, help="Startbahnhof, z. B. 'Forst (Lausitz)'")
    s.add_argument("--nach", required=True, help="Zielbahnhof, z. B. 'Stolberg Rheinl Hbf'")
    s.add_argument("--datum", required=True, nargs="+", help="ein oder mehrere Reisetage JJJJ-MM-TT")
    s.add_argument("--ab", default="04:00", help="früheste Abfahrt (Standard 04:00)")
    s.add_argument("--bis", default="19:00", help="späteste Abfrage (Standard 19:00)")
    s.add_argument("--raster", type=int, default=30, help="Minuten zwischen zwei Abfragen (Standard 30)")
    s.add_argument("--modi", default="alle,ohne_ICE", help="'alle', 'ohne_ICE' oder beides (Standard)")
    s.add_argument("--max-umstiege", type=int, default=12)
    s.add_argument("--min-umstieg", type=float, default=4,
                   help="Verbindungen mit kürzerem Umstieg verwerfen, bahn.de verkauft sie meist nicht (Standard 4)")
    s.add_argument("--alle-betreiber", action="store_true", help="auch FlixTrain, Nachtzüge usw. behalten")
    s.add_argument("--top", type=int, default=10, help="so viele im Detail anzeigen")
    s.add_argument("--ausgabe", default=str(REPO / "ergebnisse"), help="Ordner für Tabelle und JSON")
    common(s)
    s.set_defaults(func=cmd_suche)

    c = sub.add_parser("pruefe", help="bahn.de-Links (…?vbid=…) bewerten")
    c.add_argument("links", nargs="+", help="bahn.de-Link oder vbid, mehrere möglich")
    common(c)
    c.set_defaults(func=cmd_pruefe)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
