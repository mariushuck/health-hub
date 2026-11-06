"""Holt Daten je Quelle und schreibt erst Rohdaten, dann Tageswerte.

python sync.py                         einmal die letzten 3 Tage
python sync.py --backfill 2025-02-01   Historie ab Datum in kleinen Fenstern
python sync.py --daemon                dauerhaft zu den Zeiten aus der Config
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import config as config_module
import db
import providers
from config import ACTIVITIES, Config
from providers import Provider

log = logging.getLogger("sync")

SYNC_DAYS = 3
BACKFILL_PAUSE_S = 1
PER_DAY_MAX_CALLS = 30


def windows(start: date, end: date, size: int | None) -> Iterator[tuple[date, date]]:
    if size is None:
        yield start, end
        return
    while start <= end:
        stop = min(start + timedelta(days=size - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


def rebuild(
    conn: sqlite3.Connection, cfg: Config, sources: dict[str, Provider], days: set[date]
) -> None:
    """Berechnet Tage aus allen Rohdaten neu. So setzt sich die Quellen-Reihenfolge auch
    durch, wenn die bevorzugte Quelle erst nach der nachrangigen Daten liefert."""
    for day in sorted(days):
        raw = {name: db.load_raw(conn, name, day) for name in sources}
        parsed = {name: sources[name].parse_day(raw[name]) for name in sources}
        values: dict = {}
        for group, columns in db.GROUPS.items():
            for name in cfg.sources.get(group, []):
                candidate = {c: parsed[name].get(c) for c in columns}
                if any(v is not None for v in candidate.values()):
                    values.update(candidate)
                    break
        db.upsert_daily(conn, day, values)
        for name in cfg.sources.get(ACTIVITIES, []):
            db.upsert_activities(conn, name, day, sources[name].parse_activities(raw[name]))


def sync_per_day(
    conn: sqlite3.Connection,
    cfg: Config,
    sources: dict[str, Provider],
    provider: Provider,
    start: date,
    end: date,
) -> None:
    """Holt die aktuellen Tage und danach fehlende ältere, zusammen höchstens PER_DAY_MAX_CALLS."""
    current = [end - timedelta(days=n) for n in range((end - start).days + 1)]
    missing = db.days_missing(
        conn,
        provider.name,
        provider.per_day_endpoint,
        provider.per_day_anchor,
        before=start,
        limit=PER_DAY_MAX_CALLS,
    )
    for row in provider.fetch_per_day((current + missing)[:PER_DAY_MAX_CALLS]):
        db.upsert_raw(conn, provider.name, [row])
        rebuild(conn, cfg, sources, {row.day})


def run(
    conn: sqlite3.Connection,
    cfg: Config,
    sources: dict[str, Provider],
    start: date,
    end: date,
    backfill: bool = False,
) -> bool:
    """Ein Ausfall einer Quelle hält die andere nicht auf. True, wenn alles geklappt hat."""
    ok = True
    for name, provider in sources.items():
        state = db.get_state(conn, name)
        last_attempt = state["last_attempt"] if state else None
        if last_attempt and time.time() - last_attempt < provider.min_interval_s:
            log.warning("%s: letzter Abruf zu kurz her, übersprungen", name)
            ok = ok and not backfill  # Fehlende Historie muss auffallen, ein Sync kommt wieder.
            continue
        cursor = state["cursor"] if state and not backfill else None
        db.mark_attempt(conn, name)  # vor dem Netz, damit auch ein Absturz als Abruf zählt
        try:
            new_cursor = None
            for window_start, window_end in windows(start, end, provider.max_window_days):
                fetched = provider.fetch(window_start, window_end, cursor)
                db.upsert_raw(conn, name, fetched.rows)
                rebuild(conn, cfg, sources, {row.day for row in fetched.rows})
                new_cursor = fetched.cursor or new_cursor
                log.info("%s: %s bis %s geschrieben", name, window_start, window_end)
                if backfill:
                    time.sleep(BACKFILL_PAUSE_S)
            if provider.per_day_endpoint and not backfill:
                sync_per_day(conn, cfg, sources, provider, start, end)
            db.mark_success(conn, name, new_cursor)
        except Exception as error:  # Grenze je Quelle, Details stehen im Log und in sync_state
            log.exception("%s: Sync fehlgeschlagen", name)
            db.mark_error(conn, name, str(error)[:500])
            ok = False
    return ok


def create_sources(cfg: Config, data_dir: Path) -> dict[str, Provider]:
    names = sorted({name for names in cfg.sources.values() for name in names})
    return {name: providers.create(name, data_dir, cfg.timezone) for name in names}


def record_key_expiry(conn: sqlite3.Connection) -> None:
    """Die API nennt das Ablaufdatum des Keys nicht, es kommt von Hand aus der .env."""
    value = os.environ.get("GARMIN_KEY_EXPIRES") or None
    if value:
        try:
            value = date.fromisoformat(value).isoformat()
        except ValueError:
            log.error("GARMIN_KEY_EXPIRES ist kein Datum (YYYY-MM-DD): %s", value)
            value = None
    db.set_key_expires(conn, "garmin", value)


def sync_once(data_dir: Path, cfg: Config, since: date | None = None) -> bool:
    conn = db.connect(data_dir / "hub.db")
    try:
        today = datetime.now(cfg.timezone).date()
        start = since or today - timedelta(days=SYNC_DAYS - 1)
        sources = create_sources(cfg, data_dir)
        if "garmin" in sources:
            record_key_expiry(conn)  # vor dem Abruf: Gerade ein abgelaufener Key lässt ihn scheitern.
        return run(conn, cfg, sources, start, today, backfill=bool(since))
    finally:
        conn.close()


def seconds_until_next(now: datetime, cfg: Config) -> float:
    candidates = (
        datetime.combine(now.date() + timedelta(days=offset), sync_time, cfg.timezone)
        for offset in (0, 1)
        for sync_time in cfg.sync_times
    )
    # Über Zeitstempel, damit die Zeitumstellung nicht eine Stunde verschiebt.
    return min(c.timestamp() - now.timestamp() for c in candidates if c > now)


def daemon(data_dir: Path, cfg: Config) -> None:
    # shortcut: eigene Schleife, in Phase 2 durch die JobQueue des Bots ersetzen.
    while True:
        time.sleep(seconds_until_next(datetime.now(cfg.timezone), cfg))
        sync_once(data_dir, cfg)


def main() -> int:
    os.umask(0o077)  # hub.db enthält Gesundheitsdaten, nur der Besitzer darf sie lesen.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Garmin und Withings nach SQLite")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backfill", type=date.fromisoformat, metavar="YYYY-MM-DD")
    mode.add_argument("--daemon", action="store_true")
    args = parser.parse_args()

    cfg = config_module.load(args.data_dir)
    if args.daemon:
        daemon(args.data_dir, cfg)
    return 0 if sync_once(args.data_dir, cfg, args.backfill) else 1


if __name__ == "__main__":
    sys.exit(main())
