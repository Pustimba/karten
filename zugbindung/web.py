"""Lokale Weboberfläche: http://127.0.0.1:8765

Nur Standardbibliothek. Langlaufende Aufgaben (Daten bauen, Suche, Prüfung)
laufen als Hintergrundjob, die Seite fragt den Fortschritt regelmäßig ab.
Es läuft immer höchstens ein Job gleichzeitig.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).with_name("static")


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.status = "laeuft"          # laeuft | fertig | fehler | abgebrochen
        self.log: list[str] = []
        self.done = 0
        self.total = 0
        self.phase = ""
        self.result = None
        self.error = ""
        self.started = time.time()
        self.proc: subprocess.Popen | None = None

    def say(self, msg: str):
        self.log.append(msg)
        del self.log[:-300]

    def to_dict(self, with_result=True) -> dict:
        d = {"id": self.id, "art": self.kind, "status": self.status, "log": self.log[-60:],
             "fertig": self.done, "gesamt": self.total, "phase": self.phase, "fehler": self.error,
             "sekunden": int(time.time() - self.started)}
        if with_result and self.status == "fertig":
            d["ergebnis"] = self.result
        return d


class App:
    def __init__(self, db_path: Path, delay_bahn: Path):
        self.db_path = db_path
        self.delay_bahn = delay_bahn
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.lock = threading.Lock()
        self._hist = None
        self._hist_mtime = None
        self.server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ Daten
    def history(self):
        from .history import History
        if not self.db_path.exists():
            raise RuntimeError("Noch keine Verspätungsdaten. Bitte zuerst unter 'Daten' aktualisieren.")
        mtime = self.db_path.stat().st_mtime
        if self._hist is None or mtime != self._hist_mtime:
            self._hist = History(str(self.db_path))
            self._hist_mtime = mtime
        return self._hist

    def status(self) -> dict:
        d = {"db": str(self.db_path), "db_vorhanden": self.db_path.exists(), "delay_bahn": str(self.delay_bahn),
             "job": self.current.to_dict(False) if self.current and self.current.status == "laeuft" else None}
        if d["db_vorhanden"] and not (self.current and self.current.kind == "daten" and self.current.status == "laeuft"):
            try:
                h = self.history()
                d.update(daten_von=h.first_day.isoformat(), daten_bis=h.last_day.isoformat(),
                         alter_tage=(date.today() - h.last_day).days)
            except Exception as e:  # z. B. halb geschriebene Datei
                d["db_fehler"] = str(e)
        return d

    # ------------------------------------------------------------ Jobs
    def start(self, kind: str, target, *args) -> Job:
        with self.lock:
            if self.current and self.current.status == "laeuft":
                raise RuntimeError("Es läuft schon eine Aufgabe. Bitte warten, bis sie fertig ist.")
            job = Job(kind)
            self.jobs[job.id] = job
            self.current = job

        def run():
            try:
                job.result = target(job, *args)
                if job.status == "laeuft":
                    job.status = "fertig"
            except SystemExit as e:
                job.status, job.error = "fehler", str(e)
            except Exception as e:
                job.status, job.error = "fehler", str(e) or e.__class__.__name__
                job.say(traceback.format_exc())

        threading.Thread(target=run, daemon=True).start()
        return job

    def job_daten(self, job: Job, tage: int, prozesse: int):
        job.phase = "Vorbereiten"
        cmd = [sys.executable, "-m", "zugbindung.cli", "db-bauen", "--delay-bahn", str(self.delay_bahn),
               "--tage", str(tage), "--prozesse", str(prozesse)]
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", env=env, bufsize=1)
        for raw in job.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if m := re.search(r"Fetching (\d+) files:\s+(\d+)%", line):
                # build_delay_db.py prüft den Download später noch einmal; das
                # darf die Anzeige nicht auf "Download" zurücksetzen
                if job.phase in ("Vorbereiten", "delay_bahn herunterladen", "Download"):
                    job.phase, job.done, job.total = "Download", int(m.group(2)), 100
                continue  # Fortschrittsbalken nicht ins Protokoll
            if m := re.match(r"\[(\d+)/(\d+)\]", line):
                job.phase, job.done, job.total = "Tage einlesen", int(m.group(1)), int(m.group(2))
            elif line.startswith("Klone"):
                job.phase = "delay_bahn herunterladen"
            elif line.startswith("Lade und parse"):
                job.phase = "Download"
            elif line.startswith("Download "):
                job.phase, job.done, job.total = "Download", 0, 100
            elif line.startswith("Führe"):
                job.phase, job.done, job.total = "Zusammenführen", 0, 0
            job.say(line)
        code = job.proc.wait()
        if job.status == "abgebrochen":
            return None
        if code != 0:
            raise RuntimeError(f"Daten-Update mit Fehlercode {code} beendet. Details im Protokoll.")
        self._hist = None
        return self.status()

    def job_suche(self, job: Job, params: dict):
        from . import service
        h = self.history()
        p = service.SearchParams(
            von=params["von"], nach=params["nach"], daten=params["daten"],
            ab=params.get("ab") or "04:00", bis=params.get("bis") or "19:00",
            raster=int(params.get("raster") or 30),
            modi=params.get("modi") or ["alle", "ohne_ICE"],
            min_umstieg=float(params.get("min_umstieg") or 4),
            alle_betreiber=bool(params.get("alle_betreiber")),
            navigator_puffer=int(params.get("navigator_puffer") if params.get("navigator_puffer") not in (None, "") else 3),
            wochentage=",".join(params.get("wochentage") or []),
            ermaessigung=params.get("ermaessigung") or "bc25",
            von_name=params.get("von_name") or "", nach_name=params.get("nach_name") or "",
            von_id=params.get("von_id") or "", nach_id=params.get("nach_id") or "")
        if not p.daten:
            raise RuntimeError("Bitte mindestens einen Reisetag wählen.")
        a = datetime.strptime(p.ab, "%H:%M")
        b = datetime.strptime(p.bis, "%H:%M")
        steps = max(1, int((b - a).total_seconds() // 60 // p.raster) + 1)
        job.total = steps * len(p.daten)
        job.phase = "Verbindungen sammeln"

        def progress(msg):
            if re.search(r"\d{2}:\d{2}: \d+ Verbindungen", msg):
                job.done += 1
            elif "bewerte" in msg:
                job.phase = "Bewerten"
            job.say(msg)

        res = service.search(h, p, progress=progress)
        res.pop("pairs", None)
        job.say(f"{len(res['ergebnisse'])} Verbindungen bewertet.")
        return res

    def job_bestetage(self, job: Job, params: dict):
        from . import service
        h = self.history()
        p = service.BestDaysParams(
            von=params["von"], nach=params["nach"], datum_von=params["datum_von"], datum_bis=params["datum_bis"],
            wochentage=[int(x) for x in params.get("wochentage") or []],
            ab=params.get("ab") or "05:00", bis=params.get("bis") or "19:00",
            max_preis=float(str(params.get("max_preis") or 20).replace(",", ".")),
            ermaessigung=params.get("ermaessigung") or "bc25",
            navigator_puffer=int(params.get("navigator_puffer") if params.get("navigator_puffer") not in (None, "") else 3),
            von_id=params.get("von_id") or "", nach_id=params.get("nach_id") or "",
            starts=[x for x in params.get("starts") or [] if x], ziele=[x for x in params.get("ziele") or [] if x],
            knoten=int(params.get("knoten") if params.get("knoten") not in (None, "") else 2),
            baustellen=[b for b in params.get("baustellen") or [] if b.get("stationen") and b.get("von") and b.get("bis")])
        dates = service.travel_dates(p)
        if not dates:
            raise RuntimeError("Im gewählten Zeitraum liegt kein passender Reisetag.")
        if len(dates) > 62:
            raise RuntimeError("Bitte höchstens 62 Reisetage auf einmal abfragen.")
        job.total, job.phase = len(dates) * (2 if (p.knoten or p.baustellen) else 1), "bahn.de abfragen"
        pairs = (1 + len(p.starts)) * (1 + len(p.ziele))
        if pairs * len(dates) * (1 + p.knoten) > 900:
            raise RuntimeError(f"{pairs} Ticket-Varianten × {len(dates)} Tage sind zu viele Abfragen. "
                               "Bitte weniger Bahnhöfe oder einen kürzeren Zeitraum wählen.")

        def day_done(d, n):
            job.done += 1
            if job.done > len(dates):
                job.phase = "Runde 2: erzwungene Umwege"

        try:
            return service.best_days(h, p, progress=job.say, day_done=day_done)
        except service.bahnde.Blocked as e:
            raise RuntimeError(str(e))

    def job_vorschlaege(self, job: Job, params: dict):
        from . import service
        h = self.history()
        job.phase = "Bahnhöfe vor und hinter der Wunschstrecke suchen"
        return service.suggest_endpoints(h, params["von"], params["nach"], progress=job.say)

    def job_pruefe(self, job: Job, params: dict):
        from . import service
        h = self.history()
        links = [l for l in re.split(r"\s+", params.get("links", "")) if l]
        if not links:
            raise RuntimeError("Bitte mindestens einen bahn.de-Link einfügen.")
        job.total, job.phase = len(links), "bahn.de abfragen"

        def progress(msg):
            job.say(msg)

        out = []
        for link in links:
            try:
                for d in service.check(h, [link], int(params.get("navigator_puffer") or 3), progress=progress):
                    d.pop("pairs", None)
                    out.append(d)
            except SystemExit as e:
                out.append({"fehler": str(e), "link": link})
            job.done += 1
        return {"ergebnisse": out, "daten_von": h.first_day.isoformat(), "daten_bis": h.last_day.isoformat()}

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job and job.status == "laeuft" and job.proc:
            job.status = "abgebrochen"
            job.proc.terminate()
            return True
        return False


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # Terminal ruhig halten
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/status":
                    return self._json(app.status())
                if url.path == "/api/halte":
                    qs = parse_qs(url.query)
                    q = (qs.get("q") or [""])[0].strip()
                    if len(q) < 3:
                        return self._json([])
                    if (qs.get("quelle") or [""])[0] == "bahnde":
                        from . import bahnde
                        try:
                            return self._json(bahnde.Client(pause=0.3).locations(q))
                        except bahnde.Blocked as e:
                            return self._json({"fehler": str(e)}, 502)
                    from . import transitous
                    return self._json(transitous.candidates(q))
                if url.path == "/api/plausibel":
                    from . import service
                    qs = parse_qs(url.query)
                    g = lambda k: (qs.get(k) or [""])[0]
                    try:
                        return self._json(service.check_endpoint(app.history(), g("von"), g("nach"), g("name"),
                                                                 g("art") == "start"))
                    except Exception as e:
                        return self._json({"ok": None, "grund": str(e)})
                if url.path.startswith("/api/job/"):
                    job = app.jobs.get(url.path.rsplit("/", 1)[-1])
                    return self._json(job.to_dict() if job else {"fehler": "unbekannter Job"}, 200 if job else 404)
                self._send(404, b"Nicht gefunden", "text/plain; charset=utf-8")
            except Exception as e:
                self._json({"fehler": str(e)}, 500)

        def do_POST(self):
            url = urlparse(self.path)
            try:
                body = self._body()
                if url.path == "/api/daten":
                    job = app.start("daten", app.job_daten, int(body.get("tage") or 31),
                                    int(body.get("prozesse") or max(1, min(4, os.cpu_count() or 1))))
                    return self._json({"job": job.id})
                if url.path == "/api/suche":
                    app.history()   # Fehler sofort melden, wenn Daten fehlen
                    return self._json({"job": app.start("suche", app.job_suche, body).id})
                if url.path == "/api/vorschlaege":
                    app.history()
                    return self._json({"job": app.start("vorschlaege", app.job_vorschlaege, body).id})
                if url.path == "/api/bestetage":
                    app.history()
                    return self._json({"job": app.start("bestetage", app.job_bestetage, body).id})
                if url.path == "/api/pruefe":
                    app.history()
                    return self._json({"job": app.start("pruefe", app.job_pruefe, body).id})
                if url.path.startswith("/api/abbrechen/"):
                    return self._json({"ok": app.cancel(url.path.rsplit("/", 1)[-1])})
                if url.path == "/api/beenden":
                    self._json({"ok": True})
                    threading.Thread(target=app.server.shutdown, daemon=True).start()
                    return
                self._send(404, b"Nicht gefunden", "text/plain; charset=utf-8")
            except Exception as e:
                self._json({"fehler": str(e)}, 400)

    return Handler


def serve(db_path: Path, delay_bahn: Path, port: int = 8765, open_browser: bool = True):
    app = App(db_path, delay_bahn)
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"Kein freier Port zwischen {port} und {port + 19}.")
    app.server = server
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"\nZugbindung läuft unter {url}")
    print("Zum Beenden dieses Fenster schließen oder in der App auf 'Beenden' klicken.\n")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if app.current and app.current.proc and app.current.status == "laeuft":
            app.current.proc.terminate()
    print("Beendet.")
