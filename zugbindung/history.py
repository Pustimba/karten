"""Zugriff auf die Verspätungsdatenbank (data/delays.duckdb aus delay_bahn).

Die Tabelle `delays` enthält pro Zug und Halt die geplante und die tatsächliche
Ankunfts- und Abfahrtszeit sowie ein Ausfall-Flag. Grundlage sind die
IRIS-Daten der DB, gesammelt vom Projekt piebro/deutsche-bahn-data.
"""
import difflib
import re
from datetime import date, datetime, timedelta

import duckdb


def _norm(s: str) -> str:
    s = s.lower().replace("hauptbahnhof", "hbf")
    s = re.sub(r"[(),]", " ", s)
    # Präfixe aus Verbundnamen (S+U, Bhf, Berlin ...) stören den Namensvergleich
    s = re.sub(r"\b(s\+u|s|u|bhf|bahnhof|berlin)\b", " ", s)
    return " ".join(s.split())


def name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class History:
    def __init__(self, db_path: str):
        self.con = duckdb.connect(db_path, read_only=True)
        lo, hi = self.con.execute(
            "SELECT min(CAST(coalesce(arrival_planned_time, departure_planned_time) AS DATE)),"
            "       max(CAST(coalesce(arrival_planned_time, departure_planned_time) AS DATE)) FROM delays"
        ).fetchone()
        self.first_day: date = lo
        self.last_day: date = hi
        self._stop_cache: dict = {}
        self._series_cache: dict = {}

    def days(self, weekdays: set[int] | None = None) -> list[date]:
        """Historische Starttage für die Simulation. Der erste und letzte Tag
        fallen weg, weil Fahrten über Mitternacht dort nur halb erfasst sind."""
        out, d = [], self.first_day + timedelta(days=1)
        while d <= self.last_day - timedelta(days=1):
            if weekdays is None or d.weekday() in weekdays:
                out.append(d)
            d += timedelta(days=1)
        return out

    def find_stop(self, num: str, line: str, kind: str, when: datetime, name: str,
                  eva: str = "") -> tuple[str, str] | None:
        """Sucht (train_no, eva) eines Zuges an einem Halt.

        Zuerst direkt über Zugnummer und EVA (wenn bekannt), sonst über
        Zugnummer oder Linie plus minutengenaue Sollzeit plus Bahnhofsname.
        kind ist "arrival" oder "departure".
        """
        key = (num, line, kind, when.strftime("%H:%M"), name, eva)
        if key in self._stop_cache:
            return self._stop_cache[key]
        col = f"{kind}_planned_time"
        tod = when.strftime("%H:%M:00")
        result = None
        if num and eva:
            hit = self.con.execute(
                f"SELECT 1 FROM delays WHERE train_no = ? AND eva = ? AND {col} IS NOT NULL LIMIT 1",
                [num, eva]).fetchone()
            if hit:
                result = (num, eva)
        if result is None and (num or line):
            for tol in (0, 3):
                rows = self.con.execute(f"""
                    SELECT train_no, eva, any_value(station_name), count(DISTINCT CAST({col} AS DATE))
                    FROM delays
                    WHERE (train_no = ? OR (? <> '' AND line_number = ?))
                      AND {col} IS NOT NULL
                      AND abs(date_diff('minute', CAST({col} AS TIME), CAST(? AS TIME))) <= ?
                    GROUP BY 1, 2""", [num, line, line, tod, tol]).fetchall()
                cands = []
                for train_no, ev, st_name, n in rows:
                    sim = 1.0 if eva and ev == eva else name_similarity(st_name or "", name)
                    if sim >= 0.5:
                        cands.append((sim, train_no == num, n, train_no, ev))
                if cands:
                    best = max(cands)
                    result = (best[3], best[4])
                    break
        self._stop_cache[key] = result
        return result

    def series(self, train_no: str, eva: str, kind: str, when: datetime) -> dict[date, tuple[int, bool]]:
        """Tag -> (Verspätung in Minuten, ausgefallen) für einen Zug an einem Halt.
        Pro Kalendertag zählt die Fahrt, deren Sollzeit der gesuchten am nächsten liegt."""
        key = (train_no, eva, kind, when.strftime("%H:%M"))
        if key in self._series_cache:
            return self._series_cache[key]
        col, chg = f"{kind}_planned_time", f"{kind}_change_time"
        tod = when.strftime("%H:%M:00")
        diff = (f"least(abs(date_diff('minute', CAST({col} AS TIME), CAST(? AS TIME))),"
                f" 1440 - abs(date_diff('minute', CAST({col} AS TIME), CAST(? AS TIME))))")
        rows = self.con.execute(f"""
            SELECT DISTINCT ON (d) CAST({col} AS DATE) AS d,
                   date_diff('minute', {col}, {chg}), is_canceled
            FROM delays
            WHERE train_no = ? AND eva = ? AND {col} IS NOT NULL AND {diff} <= 60
            ORDER BY d, {diff}""", [train_no, eva, tod, tod, tod, tod]).fetchall()
        out = {d: (int(dl or 0), bool(c)) for d, dl, c in rows}
        self._series_cache[key] = out
        return out
