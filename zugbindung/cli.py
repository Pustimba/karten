"""Kommandozeile: zugbindung [app] | db-bauen | suche | pruefe"""
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



def _history(args):
    from .history import History
    db = Path(args.db)
    if not db.exists():
        sys.exit(f"Verspätungsdatenbank {db} fehlt. Zuerst 'uv run zugbindung db-bauen' ausführen "
                 f"oder mit --db den Pfad angeben.")
    h = History(str(db))
    print(f"Verspätungsdaten: {h.first_day:%d.%m.%Y} bis {h.last_day:%d.%m.%Y}", file=sys.stderr)
    return h


# ---------------------------------------------------------------- db-bauen
def _unzip_github(repo: str, ref: str, dest: Path) -> None:
    """Lädt ein GitHub-Repo als ZIP und entpackt es ohne den obersten Ordner nach dest."""
    import io
    import urllib.request
    import zipfile
    url = f"https://codeload.github.com/{repo}/zip/{ref}"
    print(f"Lade {repo} ({ref.rsplit('/', 1)[-1][:12]}) als ZIP ...")
    with urllib.request.urlopen(url, timeout=300) as r:
        zf = zipfile.ZipFile(io.BytesIO(r.read()))
    for info in zf.infolist():
        rel = info.filename.split("/", 1)[1] if "/" in info.filename else ""
        if not rel:
            continue
        target = dest / rel
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))


def _fetch_delay_bahn_without_git(db_dir: Path) -> None:
    """Ausweg für Rechner ohne git: delay_bahn und das Submodul als ZIP holen."""
    import json
    import urllib.request
    _unzip_github("sha2nkt/delay_bahn", "refs/heads/main", db_dir)
    ref = "refs/heads/main"
    try:   # Stand des Submoduls, den delay_bahn erwartet
        req = urllib.request.Request("https://api.github.com/repos/sha2nkt/delay_bahn/contents/deutsche-bahn-data",
                                     headers={"User-Agent": "zugbindung-tool"})
        with urllib.request.urlopen(req, timeout=60) as r:
            ref = json.load(r).get("sha") or ref
    except Exception:
        pass
    _unzip_github("piebro/deutsche-bahn-data", ref, db_dir / "deutsche-bahn-data")


def cmd_db_bauen(args):
    db_dir = Path(args.delay_bahn)
    # delay_bahn hat eine eigene Umgebung; die dieses Tools darf nicht durchsickern
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    import shutil
    has_git = shutil.which("git") is not None
    if not has_git and not (db_dir / "pyproject.toml").exists():
        print(f"git ist nicht installiert, lade delay_bahn nach {db_dir} ...")
        _fetch_delay_bahn_without_git(db_dir)
    elif not has_git:
        pass   # ZIP-Stand von früher weiterverwenden
    elif not db_dir.exists():
        print(f"Klone delay_bahn nach {db_dir} ...")
        subprocess.run(["git", "clone", "--recurse-submodules", "--shallow-submodules", "--depth", "1",
                        "https://github.com/sha2nkt/delay_bahn.git", str(db_dir)], check=True, env=env)
    elif (db_dir / ".git").exists():
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
    from . import report, service

    h = _history(args)
    p = service.SearchParams(
        von=args.von, nach=args.nach, daten=args.datum, ab=args.ab, bis=args.bis, raster=args.raster,
        modi=[m.strip() for m in args.modi.split(",")], max_umstiege=args.max_umstiege,
        min_umstieg=args.min_umstieg, alle_betreiber=args.alle_betreiber,
        navigator_puffer=args.navigator_puffer, wochentage=args.wochentage or "",
        gleicher_wochentag=args.gleicher_wochentag)
    res = service.search(h, p)
    rows = res["pairs"]

    out = Path(args.ausgabe)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    slug = re.sub(r"\W+", "_", f"{res['von']}_{res['nach']}").strip("_")
    header = (f"# Rangliste {res['von']} → {res['nach']}\n\n"
              f"Reisetage: {', '.join(args.datum)}. Verspätungsdaten {h.first_day:%d.%m.%Y} bis "
              f"{h.last_day:%d.%m.%Y}. Navigator-Puffer {args.navigator_puffer} min, "
              f"Mindestumstieg {args.min_umstieg} min. Erstellt {datetime.now():%d.%m.%Y %H:%M}.")
    md = out / f"rangliste_{slug}_{stamp}.md"
    md.write_text(report.markdown_table(rows, header), encoding="utf-8")
    (out / f"rangliste_{slug}_{stamp}.json").write_text(report.to_json(rows), encoding="utf-8")

    print(f"\n{len(rows)} Verbindungen bewertet. Die besten {min(args.top, len(rows))}:\n")
    for i, ((nav, strict), d) in enumerate(zip(rows[:args.top], res["ergebnisse"]), 1):
        print(report.detail(strict, nav, title=f"#{i}"))
        if d["zwischenhalte"]:
            print("Auf bahn.de als Zwischenhalt: " + ", ".join(d["zwischenhalte"]))
        print(f"bahn.de: {d['bahn_link']}")
        print()
    print(f"Tabelle: {md}")


# ---------------------------------------------------------------- pruefe
def cmd_pruefe(args):
    from . import report, service

    h = _history(args)
    for d in service.check(h, args.links, args.navigator_puffer, args.wochentage or "",
                           args.gleicher_wochentag):
        nav, strict = d["pairs"]
        print(report.detail(strict, nav, title=f"=== {d['titel']} (vbid {d['vbid']})"))
        print()


# ---------------------------------------------------------------- app
def cmd_app(args):
    from . import web
    web.serve(db_path=Path(args.db), delay_bahn=Path(args.delay_bahn), port=args.port,
              open_browser=not args.kein_browser)


def main(argv=None):
    p = argparse.ArgumentParser(prog="zugbindung", description=__doc__)
    sub = p.add_subparsers(dest="cmd")

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

    a = sub.add_parser("app", help="Weboberfläche im Browser starten (Standard ohne Befehl)")
    a.add_argument("--db", default=str(DEFAULT_DB), help="Pfad zu delays.duckdb")
    a.add_argument("--delay-bahn", default=str(DEFAULT_DELAY_BAHN), help="Pfad zum delay_bahn-Checkout")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--kein-browser", action="store_true", help="Browser nicht automatisch öffnen")
    a.set_defaults(func=cmd_app)

    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv or ["app"])
    args.func(args)


if __name__ == "__main__":
    main()
