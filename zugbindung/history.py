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
        self._stations: dict[str, str] | None = None

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

    def stations(self) -> dict[str, str]:
        """EVA -> Bahnhofsname (DB-Schreibweise), einmal geladen."""
        if self._stations is None:
            rows = self.con.execute(
                "SELECT eva, any_value(station_name) FROM delays WHERE station_name IS NOT NULL GROUP BY eva"
            ).fetchall()
            self._stations = {eva: name for eva, name in rows}
        return self._stations

    def station_by_name(self, name: str) -> tuple[str, str] | None:
        """Bahnhofsname (beliebige Schreibweise) -> (EVA, DB-Name), oder None."""
        city = (re.findall(r"[^\W\d_]+", name.lower()) or [""])[0]
        best = None
        for eva, st in self.stations().items():
            if not st.lower().startswith(city):
                continue
            sim = name_similarity(st, name)
            if best is None or sim > best[0]:
                best = (sim, eva, st)
        return (best[1], best[2]) if best and best[0] >= 0.5 else None

    def evas_named(self, name: str) -> list[str]:
        """Alle EVA-Nummern mit genau diesem Bahnhofsnamen (Berlin Hbf hat z. B.
        getrennte Nummern für oben, tief und S-Bahn)."""
        return [eva for eva, st in self.stations().items() if st == name]

    def connected(self, evas: list[str], before: bool, min_minutes: int, max_minutes: int,
                  days: int = 10, min_rides: int = 5) -> list[tuple[str, str, int, float]]:
        """Bahnhöfe, die mit direkten Zügen vor (before=True) bzw. nach einem der
        evas erreicht werden, mit Anzahl Fahrten und typischer Fahrzeit.
        Ergebnis: (eva, name, fahrten, minuten), häufigste zuerst."""
        since = self.last_day - timedelta(days=days)
        ph = ",".join("?" * len(evas))
        if before:
            hub_col, other_col, cond = "arrival_planned_time", "departure_planned_time", "o.t < h.t"
            diff = "date_diff('minute', o.t, h.t)"
        else:
            hub_col, other_col, cond = "departure_planned_time", "arrival_planned_time", "o.t > h.t"
            diff = "date_diff('minute', h.t, o.t)"
        rows = self.con.execute(f"""
            WITH h AS (
                SELECT train_line_ride_id AS rid, {hub_col} AS t FROM delays
                WHERE eva IN ({ph}) AND {hub_col} >= ?
                  -- S-Bahn, Bus und Tram bringen nur Vororte; gesucht sind Regional- und Fernzüge
                  AND coalesce(train_type, '') NOT IN ('S', 'Bus', 'BUS', 'STR', 'U', 'Tram', 'FLX')
            ), o AS (
                SELECT eva, station_name, train_line_ride_id AS rid, {other_col} AS t FROM delays
                WHERE {other_col} >= ? AND eva NOT IN ({ph})
            )
            SELECT o.eva, any_value(o.station_name), count(DISTINCT o.rid), median({diff})
            FROM h JOIN o USING (rid)
            WHERE {cond} AND {diff} BETWEEN ? AND ?
            GROUP BY 1 HAVING count(DISTINCT o.rid) >= ?
            ORDER BY 3 DESC LIMIT 200""",
            [*evas, since, since - timedelta(days=1), *evas, min_minutes, max_minutes, min_rides]).fetchall()
        return [(e, n, int(r), float(m)) for e, n, r, m in rows]
