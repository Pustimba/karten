"""Lädt die DB-Rohdaten und parst die Tage parallel.

Läuft im delay_bahn-Ordner und in dessen Python-Umgebung (siehe
`zugbindung db-bauen`). Das Original-Skript pipeline/build_delay_db.py parst
die Tage nacheinander, ca. 4 bis 5 Minuten pro Tag. Hier laufen mehrere Tage
gleichzeitig. Danach übernimmt build_delay_db.py die fertigen Tage aus dem
Cache und führt nur noch den Merge aus.
"""
import argparse
import json
import sys
from datetime import date, timedelta
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, "pipeline")
import build_delay_db as B  # noqa: E402  (aus delay_bahn/pipeline)
from huggingface_hub import snapshot_download  # noqa: E402

DATA = Path("data")
PARSED = DATA / "de" / "parsed"


def work(d: date) -> str:
    files = B.get_window_parquet_files(DATA / "raw_data", [d])
    if not files:
        return f"{d}: keine Rohdaten"
    day_dir = PARSED / d.isoformat()
    if day_dir.exists() and day_dir.stat().st_mtime > max(f.stat().st_mtime for f in files):
        return f"{d}: schon geparst"
    eva = json.load(open(B.SUBMODULE_ROOT / "config" / "eva_to_station_name.json"))
    xml, plan, fchg = B.parse_day(d, files, eva, PARSED)
    return f"{d}: {xml:_} XML-Antworten, {plan:_} Plan-, {fchg:_} Änderungszeilen"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tage", type=int, default=31)
    p.add_argument("--bis", required=True)
    p.add_argument("--prozesse", type=int, default=4)
    a = p.parse_args()
    end = date.fromisoformat(a.bis)
    dates = [end - timedelta(days=i) for i in range(a.tage)]
    print(f"Download {dates[-1]} bis {end} von HuggingFace (piebro/deutsche-bahn-data) ...", flush=True)
    snapshot_download(repo_id=B.HF_REPO, repo_type="dataset", local_dir=DATA,
                      allow_patterns=[f"raw_data/year={d.year}/month={d.month}/day={d.day}/*" for d in dates])
    PARSED.mkdir(parents=True, exist_ok=True)
    with Pool(a.prozesse) as pool:
        for i, msg in enumerate(pool.imap_unordered(work, dates), 1):
            print(f"[{i}/{len(dates)}] {msg}", flush=True)


if __name__ == "__main__":
    main()
