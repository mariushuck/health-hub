"""SQLite-Schema und Upserts. Jede Instanz hat genau eine Datei unter /data."""

import json
import sqlite3
import time
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

# Je Gruppe gewinnt eine Quelle pro Tag, damit sich an einem Tag nie Werte
# verschiedener Geräte mischen (etwa Gewicht von Withings, Fettanteil von Garmin).
GROUPS: dict[str, tuple[str, ...]] = {
    "sleep": (
        "sleep_seconds",
        "deep_sleep_seconds",
        "light_sleep_seconds",
        "rem_sleep_seconds",
        "awake_sleep_seconds",
        "sleep_score",
    ),
    "hrv": (
        "hrv_last_night",
        "hrv_weekly_avg",
        "hrv_status",
        "hrv_baseline_low",
        "hrv_baseline_high",
    ),
    "resting_hr": ("resting_hr",),
    "body_battery": ("body_battery_high", "body_battery_low"),
    "stress": ("stress_avg",),
    "steps": ("steps",),
    "body": ("weight_kg", "fat_ratio_pct", "muscle_mass_kg"),
}
DAILY_COLUMNS = tuple(column for columns in GROUPS.values() for column in columns)

ACTIVITY_COLUMNS = (
    "activity_id",
    "date",
    "start_local",
    "type",
    "name",
    "duration_s",
    "distance_m",
    "avg_hr",
    "max_hr",
    "aerobic_te",
    "anaerobic_te",
    "training_load",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_payload (
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    date TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (source, endpoint, date)
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    sleep_seconds INTEGER,
    deep_sleep_seconds INTEGER,
    light_sleep_seconds INTEGER,
    rem_sleep_seconds INTEGER,
    awake_sleep_seconds INTEGER,
    sleep_score INTEGER,
    hrv_last_night INTEGER,
    hrv_weekly_avg INTEGER,
    hrv_status TEXT,
    hrv_baseline_low INTEGER,
    hrv_baseline_high INTEGER,
    resting_hr INTEGER,
    body_battery_high INTEGER,
    body_battery_low INTEGER,
    stress_avg INTEGER,
    steps INTEGER,
    weight_kg REAL,
    fat_ratio_pct REAL,
    muscle_mass_kg REAL
);

CREATE TABLE IF NOT EXISTS activities (
    source TEXT NOT NULL,
    activity_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    start_local TEXT,
    type TEXT,
    name TEXT,
    duration_s REAL,
    distance_m REAL,
    avg_hr REAL,
    max_hr REAL,
    aerobic_te REAL,
    anaerobic_te REAL,
    training_load REAL,
    PRIMARY KEY (source, activity_id)
);
CREATE INDEX IF NOT EXISTS activities_date ON activities (date);

CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,
    last_attempt INTEGER,
    last_success INTEGER,
    last_error TEXT,
    cursor INTEGER,
    key_expires TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_raw(conn: sqlite3.Connection, source: str, rows: Iterable[Any]) -> None:
    now = int(time.time())
    with conn:
        conn.executemany(
            """INSERT INTO raw_payload (source, endpoint, date, payload, fetched_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (source, endpoint, date) DO UPDATE SET
                 payload = excluded.payload, fetched_at = excluded.fetched_at""",
            [(source, r.endpoint, r.day.isoformat(), json.dumps(r.payload), now) for r in rows],
        )


def load_raw(conn: sqlite3.Connection, source: str, day: date) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT endpoint, payload FROM raw_payload WHERE source = ? AND date = ?",
        (source, day.isoformat()),
    )
    return {row["endpoint"]: json.loads(row["payload"]) for row in rows}


def days_missing(
    conn: sqlite3.Connection, source: str, endpoint: str, anchor: str, before: date, limit: int
) -> list[date]:
    """Tage, für die anchor Daten hat und endpoint noch keine Zeile, die neuesten zuerst."""
    rows = conn.execute(
        """SELECT a.date FROM raw_payload a
           WHERE a.source = ? AND a.endpoint = ? AND a.payload != 'null' AND a.date < ?
             AND NOT EXISTS (
               SELECT 1 FROM raw_payload b
               WHERE b.source = a.source AND b.endpoint = ? AND b.date = a.date)
           ORDER BY a.date DESC LIMIT ?""",
        (source, anchor, before.isoformat(), endpoint, limit),
    )
    return [date.fromisoformat(row["date"]) for row in rows]


def upsert_daily(conn: sqlite3.Connection, day: date, values: dict[str, Any]) -> None:
    """Schreibt die ganze Zeile: Was in values fehlt, wird NULL."""
    updates = ", ".join(f"{c} = excluded.{c}" for c in DAILY_COLUMNS)
    with conn:
        conn.execute(
            f"""INSERT INTO daily_metrics (date, {", ".join(DAILY_COLUMNS)})
                VALUES (?, {", ".join("?" * len(DAILY_COLUMNS))})
                ON CONFLICT (date) DO UPDATE SET {updates}""",
            (day.isoformat(), *(values.get(c) for c in DAILY_COLUMNS)),
        )


def upsert_activities(
    conn: sqlite3.Connection, source: str, day: date, activities: list[dict[str, Any]]
) -> None:
    """Gleicht einen Tag ab: In Garmin gelöschte Aktivitäten verschwinden auch hier."""
    ids = [a["activity_id"] for a in activities]
    updates = ", ".join(f"{c} = excluded.{c}" for c in ACTIVITY_COLUMNS if c != "activity_id")
    with conn:
        conn.execute(
            f"""DELETE FROM activities WHERE source = ? AND date = ?
                AND activity_id NOT IN ({", ".join("?" * len(ids))})""",
            (source, day.isoformat(), *ids),
        )
        conn.executemany(
            f"""INSERT INTO activities (source, {", ".join(ACTIVITY_COLUMNS)})
                VALUES (?, {", ".join("?" * len(ACTIVITY_COLUMNS))})
                ON CONFLICT (source, activity_id) DO UPDATE SET {updates}""",
            [(source, *(a.get(c) for c in ACTIVITY_COLUMNS)) for a in activities],
        )


def get_state(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sync_state WHERE source = ?", (source,)).fetchone()


def mark_attempt(conn: sqlite3.Connection, source: str) -> None:
    with conn:
        conn.execute(
            """INSERT INTO sync_state (source, last_attempt) VALUES (?, ?)
               ON CONFLICT (source) DO UPDATE SET last_attempt = excluded.last_attempt""",
            (source, int(time.time())),
        )


def set_key_expires(conn: sqlite3.Connection, source: str, expires: str | None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO sync_state (source, key_expires) VALUES (?, ?)
               ON CONFLICT (source) DO UPDATE SET key_expires = excluded.key_expires""",
            (source, expires),
        )


def mark_success(conn: sqlite3.Connection, source: str, cursor: int | None) -> None:
    with conn:
        conn.execute(
            """UPDATE sync_state SET last_success = ?, last_error = NULL,
               cursor = COALESCE(?, cursor) WHERE source = ?""",
            (int(time.time()), cursor, source),
        )


def mark_error(conn: sqlite3.Connection, source: str, error: str) -> None:
    with conn:
        conn.execute("UPDATE sync_state SET last_error = ? WHERE source = ?", (error, source))
