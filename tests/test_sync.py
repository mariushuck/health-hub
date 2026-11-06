import json
import time
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

import db
import sync
from config import Config
from providers import Fetched, RawRow
from providers.garmin import GarminProvider
from providers.withings import WithingsProvider

TZ = ZoneInfo("Europe/Berlin")
DAY = date(2026, 11, 5)
ALL_SOURCES = {"body": ["withings", "garmin"], "steps": ["garmin"], "activities": ["garmin"]}


def cfg(sources=None) -> Config:
    return Config(TZ, (day_time(8, 45), day_time(21, 0)), sources or ALL_SOURCES)


class FakeProvider:
    """Liefert vorgegebene Rohdaten, die Übersetzung ist die echte der Quelle."""

    max_window_days = None
    min_interval_s = 0
    per_day_endpoint = None
    per_day_anchor = None

    def __init__(self, real, rows=None, cursor=None, error=None):
        self.name = real.name
        self._real = real
        self.rows = rows or []
        self.cursor = cursor
        self.error = error
        self.calls: list[tuple] = []

    def fetch(self, start, end, cursor=None):
        self.calls.append((start, end, cursor))
        if self.error:
            raise self.error
        return Fetched(self.rows, self.cursor)

    def parse_day(self, payloads):
        return self._real.parse_day(payloads)

    def parse_activities(self, payloads):
        return self._real.parse_activities(payloads)


def garmin(tmp_path, weight=None, steps=None, activities=None, **kwargs) -> FakeProvider:
    rows = [
        RawRow("weight_range", DAY, {"latestWeight": {"weight": weight}} if weight else None),
        RawRow(
            "daily_summaries",
            DAY,
            {"movement": {"steps": {"value": steps}}} if steps is not None else None,
        ),
        RawRow("activities", DAY, activities or []),
    ]
    return FakeProvider(GarminProvider(tmp_path), rows, **kwargs)


def withings_row(grpid, taken_at, **measures) -> RawRow:
    types = {"weight": 1, "fat": 6}
    group = {
        "grpid": grpid,
        "date": taken_at,
        "attrib": 0,
        "measures": [{"type": types[k], "value": v, "unit": -2} for k, v in measures.items()],
    }
    return RawRow(f"getmeas/{grpid}", DAY, group)


def withings(tmp_path, weight=None, **kwargs) -> FakeProvider:
    rows = [withings_row(1, 1793900000, weight=weight)] if weight else []
    return FakeProvider(WithingsProvider(tmp_path, TZ), rows, **kwargs)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "hub.db")
    yield connection
    connection.close()


def daily(conn):
    return conn.execute("SELECT * FROM daily_metrics WHERE date = ?", (DAY.isoformat(),)).fetchone()


def run(conn, sources, config=None, **kwargs):
    """Wie in Produktion sind immer alle Quellen da. Nicht genannte liefern diesmal nichts."""
    tmp_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    idle = {
        "garmin": FakeProvider(GarminProvider(Path(tmp_path).parent)),
        "withings": FakeProvider(WithingsProvider(Path(tmp_path).parent, TZ)),
    }
    return sync.run(conn, config or cfg(), idle | {p.name: p for p in sources}, DAY, DAY, **kwargs)


def dump(conn) -> list:
    return [
        [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in ("daily_metrics", "activities")
    ] + [
        [tuple(row) for row in conn.execute("SELECT source, endpoint, date, payload FROM raw_payload ORDER BY 1, 2, 3")]
    ]


def test_second_run_changes_nothing(conn, tmp_path):
    activity = {"activityId": 7, "startTimeLocal": "2026-11-05 18:00:00", "duration": 60.0}
    sources = [garmin(tmp_path, weight=81.0, steps=5000, activities=[activity]), withings(tmp_path, weight=8100)]

    assert run(conn, sources)
    first = dump(conn)
    assert run(conn, sources)

    assert dump(conn) == first
    assert len(first[0]) == 1
    assert len(first[1]) == 1


def test_first_source_with_data_wins(conn, tmp_path):
    run(conn, [garmin(tmp_path, weight=81.0), withings(tmp_path, weight=8000)])

    assert daily(conn)["weight_kg"] == 80.0


def test_preferred_source_arriving_later_replaces_fallback(conn, tmp_path):
    run(conn, [garmin(tmp_path, weight=81.0), withings(tmp_path)])
    assert daily(conn)["weight_kg"] == 81.0

    run(conn, [withings(tmp_path, weight=8000), garmin(tmp_path, weight=81.0)])

    assert daily(conn)["weight_kg"] == 80.0


def test_fallback_syncing_after_preferred_source_does_not_overwrite(conn, tmp_path):
    run(conn, [withings(tmp_path, weight=8000)])
    assert daily(conn)["weight_kg"] == 80.0

    # Withings liefert diese Runde nichts Neues, seine Rohdaten zählen trotzdem weiter.
    run(conn, [garmin(tmp_path, weight=81.5)])

    assert daily(conn)["weight_kg"] == 80.0


def test_groups_never_mix_sources_on_one_day(conn, tmp_path):
    g = garmin(tmp_path)
    g.rows = [RawRow("weight_range", DAY, {"latestWeight": {"weight": 81.0, "bodyFat": 18.0}})]
    run(conn, [g, withings(tmp_path, weight=8000)])

    assert daily(conn)["weight_kg"] == 80.0
    assert daily(conn)["fat_ratio_pct"] is None


def test_metric_without_configured_source_stays_null(conn, tmp_path):
    run(conn, [garmin(tmp_path, steps=5000)], config=cfg({"body": ["garmin"]}))

    assert daily(conn)["steps"] is None


def test_retracted_value_returns_to_null(conn, tmp_path):
    run(conn, [garmin(tmp_path, steps=5000)])
    assert daily(conn)["steps"] == 5000

    run(conn, [garmin(tmp_path)])

    assert daily(conn)["steps"] is None


def test_real_zero_is_stored_as_zero(conn, tmp_path):
    run(conn, [garmin(tmp_path, steps=0)])

    assert daily(conn)["steps"] == 0


def test_raw_data_survives_failed_parsing(conn, tmp_path, monkeypatch):
    g = garmin(tmp_path, steps=5000)
    monkeypatch.setattr(g._real, "parse_day", lambda _: 1 / 0)

    assert run(conn, [g]) is False

    assert conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0] == 3
    assert daily(conn) is None
    assert "division" in db.get_state(conn, "garmin")["last_error"]


def test_failing_source_does_not_stop_the_other(conn, tmp_path):
    failing = withings(tmp_path, error=RuntimeError("Token abgelaufen"))

    assert run(conn, [failing, garmin(tmp_path, weight=81.0)]) is False

    assert daily(conn)["weight_kg"] == 81.0
    assert db.get_state(conn, "withings")["last_error"] == "Token abgelaufen"
    assert db.get_state(conn, "withings")["last_success"] is None
    assert db.get_state(conn, "garmin")["last_error"] is None


def test_success_clears_error_and_stores_cursor(conn, tmp_path):
    run(conn, [withings(tmp_path, error=RuntimeError("kaputt"))])

    run(conn, [withings(tmp_path, weight=8000, cursor=1794000000)])

    state = db.get_state(conn, "withings")
    assert state["last_error"] is None
    assert state["last_success"] is not None
    assert state["cursor"] == 1794000000


def test_cursor_goes_to_next_sync_not_to_backfill(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    w = FakeProvider(WithingsProvider(tmp_path, TZ), cursor=1794000000)
    w.min_interval_s = 0
    run(conn, [w])
    run(conn, [w])
    run(conn, [w], backfill=True)

    assert [cursor for _, _, cursor in w.calls] == [None, 1794000000, None]


def test_failed_sync_keeps_cursor_for_next_attempt(conn, tmp_path):
    w = FakeProvider(WithingsProvider(tmp_path, TZ), cursor=100)
    w.min_interval_s = 0
    run(conn, [w])
    w.error, w.cursor = RuntimeError("kaputt"), 200
    run(conn, [w])

    assert db.get_state(conn, "withings")["cursor"] == 100


def test_withings_rate_limit_skips_second_poll(conn, tmp_path):
    w = withings(tmp_path, weight=8000)
    w.min_interval_s = 600

    assert run(conn, [w]) is True
    assert run(conn, [w]) is True

    assert len(w.calls) == 1


def test_backfill_reports_source_skipped_by_rate_limit(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    w = withings(tmp_path, weight=8000)
    w.min_interval_s = 600
    run(conn, [w])

    assert run(conn, [w], backfill=True) is False


def test_deleted_activity_disappears(conn, tmp_path):
    keep = {"activityId": 1, "startTimeLocal": "2026-11-05 08:00:00"}
    gone = {"activityId": 2, "startTimeLocal": "2026-11-05 18:00:00"}
    run(conn, [garmin(tmp_path, activities=[keep, gone])])
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2

    run(conn, [garmin(tmp_path, activities=[keep])])

    assert [r["activity_id"] for r in conn.execute("SELECT activity_id FROM activities")] == [1]

    run(conn, [garmin(tmp_path, activities=[])])

    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_activity_values_are_updated(conn, tmp_path):
    activity = {"activityId": 1, "startTimeLocal": "2026-11-05 08:00:00", "duration": 60.0}
    run(conn, [garmin(tmp_path, activities=[activity])])

    run(conn, [garmin(tmp_path, activities=[{**activity, "duration": 90.0}])])

    assert conn.execute("SELECT duration_s FROM activities").fetchone()[0] == 90.0


def test_windows_cover_range_without_gaps_or_overlap():
    result = list(sync.windows(date(2026, 1, 1), date(2026, 3, 1), 28))

    assert result[0] == (date(2026, 1, 1), date(2026, 1, 28))
    assert result[-1][1] == date(2026, 3, 1)
    for (_, previous_end), (next_start, _) in zip(result, result[1:]):
        assert (next_start - previous_end).days == 1
    assert all((end - start).days < 28 for start, end in result)


def test_windows_without_limit_is_one_call():
    assert list(sync.windows(date(2025, 1, 1), DAY, None)) == [(date(2025, 1, 1), DAY)]


def test_second_weigh_in_keeps_first_measure_group(conn, tmp_path):
    morning = withings_row(1, 1793870000, weight=8000, fat=1800)
    evening = withings_row(2, 1793900000, weight=8100)
    provider = FakeProvider(WithingsProvider(tmp_path, TZ), [morning])
    run(conn, [provider])

    provider.rows = [evening]  # lastupdate liefert nur die neue Gruppe
    run(conn, [provider])

    assert daily(conn)["weight_kg"] == 81.0
    assert daily(conn)["fat_ratio_pct"] == 18.0
    assert conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0] == 2


def test_edited_earlier_measure_does_not_replace_later_one(conn, tmp_path):
    provider = FakeProvider(
        WithingsProvider(tmp_path, TZ),
        [withings_row(1, 1793870000, weight=8000), withings_row(2, 1793900000, weight=8100)],
    )
    run(conn, [provider])

    provider.rows = [withings_row(1, 1793870000, weight=7900)]
    run(conn, [provider])

    assert daily(conn)["weight_kg"] == 81.0


def test_withings_incremental_sync_with_real_provider(conn, tmp_path):
    (tmp_path / "withings_token.json").write_text(
        json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600})
    )
    seen = []
    responses = [[withings_row(1, 1793870000, weight=8000).payload], [withings_row(2, 1793900000, weight=8100).payload]]

    def handler(request):
        seen.append(parse_qs(request.content.decode())["lastupdate"][0])
        body = {"updatetime": str(1000 * len(seen)), "more": 0, "measuregrps": responses.pop(0)}
        return httpx.Response(200, json={"status": 0, "body": body})

    provider = WithingsProvider(tmp_path, TZ, transport=httpx.MockTransport(handler))
    provider.min_interval_s = 0
    config = cfg({"body": ["withings"]})

    assert sync.run(conn, config, {"withings": provider}, DAY, DAY)
    assert sync.run(conn, config, {"withings": provider}, DAY, DAY)

    assert seen == ["0", "1000"]
    assert daily(conn)["weight_kg"] == 81.0
    assert db.get_state(conn, "withings")["cursor"] == 2000
    assert conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0] == 2


def every_day_summaries(params) -> dict:
    start, end = date.fromisoformat(params["startDate"]), date.fromisoformat(params["endDate"])
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    return {"data": [{"calendarDate": d.isoformat(), "includesWellnessData": True} for d in days]}


def heart_rate_days(session) -> list[str]:
    return [p["date"] for url, p in session.gets if "dailyHeartRate" in url]


@pytest.fixture
def garmin_run(conn, tmp_path, garmin_session, monkeypatch):
    """Echter Garmin-Client und echte Datenbank, die Zeit bleibt stehen."""
    waits = []
    monkeypatch.setattr(sync.time, "sleep", waits.append)
    garmin_session.waits = waits
    provider = GarminProvider(tmp_path, session=garmin_session)
    config = cfg({"resting_hr": ["garmin"], "steps": ["garmin"]})

    def go(start, end, backfill=False):
        return sync.run(conn, config, {"garmin": provider}, start, end, backfill=backfill)

    return go


def test_backfill_loads_no_resting_hr(conn, garmin_run, garmin_session):
    garmin_session.overrides["daily_summaries"] = every_day_summaries

    assert garmin_run(date(2026, 9, 1), date(2026, 11, 6), backfill=True)

    assert heart_rate_days(garmin_session) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM raw_payload WHERE endpoint = 'daily_heart_rate'"
    ).fetchone()[0] == 0


def test_sync_loads_current_days_then_newest_missing_days_up_to_limit(garmin_run, garmin_session):
    garmin_session.overrides["daily_summaries"] = every_day_summaries
    garmin_run(date(2026, 8, 1), date(2026, 11, 6), backfill=True)
    garmin_session.waits.clear()

    assert garmin_run(date(2026, 11, 4), date(2026, 11, 6))

    days = heart_rate_days(garmin_session)
    assert len(days) == 30
    assert days[:5] == ["2026-11-06", "2026-11-05", "2026-11-04", "2026-11-03", "2026-11-02"]
    assert days[-1] == "2026-10-08"  # 3 aktuelle Tage und 27 fehlende ab dem 03.11.
    assert garmin_session.waits == [2] * 29


def test_next_run_continues_where_last_one_stopped(garmin_run, garmin_session):
    garmin_session.overrides["daily_summaries"] = every_day_summaries
    garmin_run(date(2026, 8, 1), date(2026, 11, 6), backfill=True)
    garmin_run(date(2026, 11, 4), date(2026, 11, 6))
    garmin_session.gets.clear()

    garmin_run(date(2026, 11, 4), date(2026, 11, 6))

    days = heart_rate_days(garmin_session)
    assert days[:4] == ["2026-11-06", "2026-11-05", "2026-11-04", "2026-10-07"]
    assert len(days) == 30


def test_catch_up_ends_when_nothing_is_missing(garmin_run, garmin_session):
    garmin_session.overrides["daily_summaries"] = every_day_summaries
    garmin_run(date(2026, 10, 28), date(2026, 11, 6), backfill=True)
    garmin_run(date(2026, 11, 4), date(2026, 11, 6))
    garmin_session.gets.clear()

    garmin_run(date(2026, 11, 4), date(2026, 11, 6))

    assert heart_rate_days(garmin_session) == ["2026-11-06", "2026-11-05", "2026-11-04"]


def test_days_without_summary_are_not_requested(garmin_run, garmin_session):
    # Die Fixture hat nur Daten für den 4. bis 6.11., davor gab es noch kein Gerät.
    garmin_run(date(2026, 9, 1), date(2026, 11, 6), backfill=True)
    garmin_session.gets.clear()

    garmin_run(date(2026, 11, 7), date(2026, 11, 9))

    assert heart_rate_days(garmin_session) == [
        "2026-11-09", "2026-11-08", "2026-11-07", "2026-11-06", "2026-11-05", "2026-11-04",
    ]


def test_rate_limit_stops_without_error_and_next_run_resumes(conn, garmin_run, garmin_session):
    garmin_session.overrides["daily_summaries"] = every_day_summaries
    garmin_run(date(2026, 8, 1), date(2026, 11, 6), backfill=True)
    garmin_session.gets.clear()
    garmin_session.waits.clear()
    garmin_session.fail_when = lambda url, p: 429 if p and p.get("date") == "2026-11-03" else None

    assert garmin_run(date(2026, 11, 4), date(2026, 11, 6)) is True

    assert heart_rate_days(garmin_session)[-1] == "2026-11-03"
    assert len(heart_rate_days(garmin_session)) == 4
    assert 60 not in garmin_session.waits  # bricht ab, statt auf das Limit zu warten
    state = db.get_state(conn, "garmin")
    assert state["last_error"] is None
    assert state["last_success"] is not None
    row = conn.execute("SELECT resting_hr FROM daily_metrics WHERE date = '2026-11-04'").fetchone()
    assert row["resting_hr"] == 46

    garmin_session.fail_when = None
    garmin_session.gets.clear()
    garmin_run(date(2026, 11, 4), date(2026, 11, 6))

    assert heart_rate_days(garmin_session)[3] == "2026-11-03"


def test_other_error_keeps_loaded_days_and_is_reported(conn, garmin_run, garmin_session):
    garmin_session.fail_when = lambda url, p: 500 if p and p.get("date") == "2026-11-05" else None

    assert garmin_run(date(2026, 11, 5), date(2026, 11, 6)) is False

    assert conn.execute(
        "SELECT date FROM raw_payload WHERE endpoint = 'daily_heart_rate'"
    ).fetchall()[0]["date"] == "2026-11-06"
    assert "500" in db.get_state(conn, "garmin")["last_error"]


def test_late_resting_hr_reaches_daily_metrics(conn, garmin_run, garmin_session):
    garmin_session.overrides["daily_summaries"] = every_day_summaries
    garmin_run(date(2026, 10, 1), date(2026, 10, 3), backfill=True)
    assert conn.execute(
        "SELECT resting_hr FROM daily_metrics WHERE date = '2026-10-02'"
    ).fetchone()["resting_hr"] is None

    garmin_run(date(2026, 11, 4), date(2026, 11, 6))

    assert conn.execute(
        "SELECT resting_hr FROM daily_metrics WHERE date = '2026-10-02'"
    ).fetchone()["resting_hr"] == 46


@pytest.mark.parametrize(
    "value, stored",
    [("2027-11-06", "2027-11-06"), ("20271106", "2027-11-06"), ("", None), ("bald", None)],
)
def test_key_expiry_is_recorded(conn, monkeypatch, value, stored):
    monkeypatch.setenv("GARMIN_KEY_EXPIRES", value)

    sync.record_key_expiry(conn)

    assert db.get_state(conn, "garmin")["key_expires"] == stored


def test_key_expiry_can_be_removed_and_keeps_other_state(conn, monkeypatch):
    monkeypatch.setenv("GARMIN_KEY_EXPIRES", "2027-11-06")
    sync.record_key_expiry(conn)
    db.mark_attempt(conn, "garmin")
    db.mark_success(conn, "garmin", 5)
    monkeypatch.delenv("GARMIN_KEY_EXPIRES")

    sync.record_key_expiry(conn)

    state = db.get_state(conn, "garmin")
    assert state["key_expires"] is None
    assert state["cursor"] == 5
    assert state["last_success"] is not None


def test_key_expiry_is_stored_even_if_garmin_sync_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_KEY_EXPIRES", "2027-11-06")
    monkeypatch.setattr(
        sync,
        "create_sources",
        lambda *_: {"garmin": garmin(tmp_path, error=RuntimeError("Key abgelaufen"))},
    )

    assert sync.sync_once(tmp_path, cfg({"steps": ["garmin"]})) is False

    conn = db.connect(tmp_path / "hub.db")
    state = db.get_state(conn, "garmin")
    conn.close()
    assert state["key_expires"] == "2027-11-06"
    assert state["last_error"] == "Key abgelaufen"


def test_withings_only_instance_records_no_garmin_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "create_sources", lambda *_: {"withings": withings(tmp_path)})

    sync.sync_once(tmp_path, cfg({"body": ["withings"]}))

    conn = db.connect(tmp_path / "hub.db")
    state = db.get_state(conn, "garmin")
    conn.close()
    assert state is None


def test_backfill_with_real_garmin_client_respects_window_limit(conn, tmp_path, garmin_session, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    provider = GarminProvider(tmp_path, session=garmin_session)
    config = cfg({"steps": ["garmin"], "activities": ["garmin"]})

    ok = sync.run(conn, config, {"garmin": provider}, date(2026, 9, 1), date(2026, 11, 6), backfill=True)

    assert ok is True
    ranges = [
        (date.fromisoformat(p["startDate"]), date.fromisoformat(p["endDate"]))
        for url, p in garmin_session.gets
        if "dailySleepSummariesByDate" in url
    ]
    assert len(ranges) == 3
    assert all((end - start).days < 28 for start, end in ranges)
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 67
    row = conn.execute("SELECT steps FROM daily_metrics WHERE date = '2026-11-05'").fetchone()
    assert row["steps"] == 11234
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2


def test_next_sync_time_is_today_or_tomorrow():
    config = cfg()

    assert sync.seconds_until_next(datetime(2026, 11, 5, 8, 0, tzinfo=TZ), config) == 45 * 60
    assert sync.seconds_until_next(datetime(2026, 11, 5, 8, 45, tzinfo=TZ), config) == 12.25 * 3600
    assert sync.seconds_until_next(datetime(2026, 11, 5, 22, 0, tzinfo=TZ), config) == 10.75 * 3600


def test_next_sync_time_across_clock_change():
    # In der Nacht auf den 25.10.2026 hat der Tag 25 Stunden.
    now = datetime(2026, 10, 24, 22, 0, tzinfo=TZ)

    assert sync.seconds_until_next(now, cfg()) == 11.75 * 3600
