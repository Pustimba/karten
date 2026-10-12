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
            # 10 min fängt kleine Verschiebungen nach einem Fahrplanwechsel ab
            for tol in (0, 3, 10):
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

    def proxy_train(self, from_eva: str, to_eva: str, dep: datetime, arr: datetime, kind: str = "",
                    dep_tol: int = 20, arr_tol: int = 30) -> tuple[str, str, str] | None:
        """Ersatz für einen Zug, den es in den Daten nicht gibt (z. B. neue
        Nummer nach dem Fahrplanwechsel): der bisherige Zug, der zur ähnlichen
        Zeit von from_eva abfuhr und danach zur ähnlichen Zeit in to_eva ankam.
        Gleiche Gattung (ICE, RE, ...) wird bevorzugt, dann der Zug mit den
        meisten Tagen und der kleinsten Zeitabweichung.
        Ergebnis: (Zugnummer, EVA Abfahrt, EVA Ankunft) oder None."""
        key = ("proxy", from_eva, to_eva, dep.strftime("%H:%M"), arr.strftime("%H:%M"), kind)
        if key in self._stop_cache:
            return self._stop_cache[key]
        tod = lambda col: (f"least(abs(date_diff('minute', CAST({col} AS TIME), CAST(? AS TIME))),"
                           f" 1440 - abs(date_diff('minute', CAST({col} AS TIME), CAST(? AS TIME))))")
        t0, t1 = dep.strftime("%H:%M:00"), arr.strftime("%H:%M:00")
        # alle Nummern desselben Bahnhofs (Berlin Hbf: oben und tief getrennt)
        st = self.stations()
        fe = self.evas_named(st.get(from_eva, "")) or [from_eva]
        te = self.evas_named(st.get(to_eva, "")) or [to_eva]
        hours = max(1, int((arr - dep).total_seconds() // 3600) + 3)
        row = self.con.execute(f"""
            WITH d AS (SELECT train_no, train_type, eva, departure_planned_time AS t, {tod('departure_planned_time')} AS dd
                       FROM delays WHERE eva IN ({",".join("?" * len(fe))}) AND departure_planned_time IS NOT NULL
                         AND {tod('departure_planned_time')} <= ?),
                 a AS (SELECT train_no, eva, arrival_planned_time AS t, {tod('arrival_planned_time')} AS ad
                       FROM delays WHERE eva IN ({",".join("?" * len(te))}) AND arrival_planned_time IS NOT NULL
                         AND {tod('arrival_planned_time')} <= ?)
            SELECT d.train_no, any_value(d.train_type) = ? AS same_kind,
                   count(DISTINCT CAST(d.t AS DATE)) AS n, avg(d.dd + a.ad) AS diff,
                   mode(d.eva) AS from_eva, mode(a.eva) AS to_eva
            FROM d JOIN a ON a.train_no = d.train_no AND a.t > d.t AND a.t < d.t + INTERVAL ({hours}) HOUR
            GROUP BY d.train_no
            ORDER BY same_kind DESC, n DESC, diff
            LIMIT 1""", [t0, t0, *fe, t0, t0, dep_tol, t1, t1, *te, t1, t1, arr_tol, kind]).fetchone()
        result = (row[0], row[4], row[5]) if row and row[2] >= 5 else None
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
        """Bahnhofsname (beliebige Schreibweise) -> (EVA, DB-Name), oder None.
        Ein reiner Stadtname wie "BERLIN" (so liefert bahn.de Sammelhalte)
        wird zum Hauptbahnhof dieser Stadt."""
        def simple(x):
            x = x.lower().replace("hauptbahnhof", "hbf")
            return " ".join(re.sub(r"[(),]", " ", x).split())

        q = simple(name)
        city = (re.findall(r"[^\W\d_]+", q) or [""])[0]
        same = [(eva, st) for eva, st in self.stations().items() if st.lower().startswith(city)]
        if not same:
            return None
        if q == city:   # nur der Stadtname: Hauptbahnhof bevorzugen
            hbf = [x for x in same if re.search(r"\b(hbf|hauptbahnhof)\b", x[1].lower())]
            pool = hbf or same
            return min(pool, key=lambda x: len(x[1]))
        best = max(same, key=lambda x: difflib.SequenceMatcher(None, simple(x[1]), q).ratio())
        sim = difflib.SequenceMatcher(None, simple(best[1]), q).ratio()
        return best if sim >= 0.5 else None

    def evas_named(self, name: str) -> list[str]:
        """Alle EVA-Nummern mit genau diesem Bahnhofsnamen (Berlin Hbf hat z. B.
        getrennte Nummern für oben, tief und S-Bahn)."""
        return [eva for eva, st in self.stations().items() if st == name]

    def connected(self, evas: list[str], before: bool, min_minutes: int, max_minutes: int,
                  days: int = 10, min_rides: int = 5) -> list[tuple]:
        """Bahnhöfe, die mit direkten Zügen vor (before=True) bzw. nach einem der
        evas erreicht werden, mit Anzahl Fahrten, typischer Fahrzeit und der
        Unzuverlässigkeit dieser Etappe (gemessen am Ankunftshalt: vor dem
        Knoten am Knoten, nach dem Knoten am anderen Bahnhof).
        Ergebnis: (eva, name, fahrten, minuten, anteil >= 5 min verspätet,
        anteil ausgefallen), häufigste zuerst."""
        since = self.last_day - timedelta(days=days)
        ph = ",".join("?" * len(evas))
        arr = ("date_diff('minute', arrival_planned_time, arrival_change_time) AS dl, "
               "coalesce(is_canceled, false) AS cx")
        none = "NULL::INTEGER AS dl, NULL::BOOLEAN AS cx"
        if before:
            hub_col, other_col, cond = "arrival_planned_time", "departure_planned_time", "o.t < h.t"
            diff = "date_diff('minute', o.t, h.t)"
            h_extra, o_extra, side = arr, none, "h"
        else:
            hub_col, other_col, cond = "departure_planned_time", "arrival_planned_time", "o.t > h.t"
            diff = "date_diff('minute', h.t, o.t)"
            h_extra, o_extra, side = none, arr, "o"
        rows = self.con.execute(f"""
            WITH h AS (
                SELECT train_line_ride_id AS rid, {hub_col} AS t, {h_extra} FROM delays
                WHERE eva IN ({ph}) AND {hub_col} >= ?
                  -- S-Bahn, Bus und Tram bringen nur Vororte; gesucht sind Regional- und Fernzüge
                  AND coalesce(train_type, '') NOT IN ('S', 'Bus', 'BUS', 'STR', 'U', 'Tram', 'FLX')
            ), o AS (
                SELECT eva, station_name, train_line_ride_id AS rid, {other_col} AS t, {o_extra} FROM delays
                WHERE {other_col} >= ? AND eva NOT IN ({ph})
            )
            SELECT o.eva, any_value(o.station_name), count(DISTINCT o.rid), median({diff}),
                   avg(CASE WHEN {side}.dl >= 5 AND NOT {side}.cx THEN 1.0 ELSE 0.0 END),
                   avg(CASE WHEN {side}.cx THEN 1.0 ELSE 0.0 END)
            FROM h JOIN o USING (rid)
            WHERE {cond} AND {diff} BETWEEN ? AND ?
            GROUP BY 1 HAVING count(DISTINCT o.rid) >= ?
            ORDER BY 3 DESC LIMIT 200""",
            [*evas, since, since - timedelta(days=1), *evas, min_minutes, max_minutes, min_rides]).fetchall()
        return [(e, n, int(r), float(m), float(late or 0), float(cx or 0)) for e, n, r, m, late, cx in rows]
