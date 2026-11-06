from datetime import date

import pytest
from garmin_personal_api.errors import HttpError

from conftest import fixture
from providers.garmin import GarminProvider

START, END = date(2026, 11, 4), date(2026, 11, 6)


def payloads_for(day: str) -> dict:
    return {
        "sleep_summaries": next(
            x for x in fixture("garmin/sleep_summaries.json") if x["calendarDate"] == day
        ),
        "hrv_summaries": next(
            (x for x in fixture("garmin/hrv_summaries.json")["hrvSummaries"] if x["calendarDate"] == day),
            None,
        ),
        "daily_summaries": next(
            x for x in fixture("garmin/daily_summaries.json")["data"] if x["calendarDate"] == day
        ),
        "weight_range": next(
            (
                x
                for x in fixture("garmin/weight_range.json")["dailyWeightSummaries"]
                if x["summaryDate"] == day
            ),
            None,
        ),
        "daily_heart_rate": fixture("garmin/daily_heart_rate.json"),
    }


@pytest.fixture
def provider(tmp_path, garmin_session):
    return GarminProvider(tmp_path, session=garmin_session)


def test_parse_day_maps_documented_fields(provider):
    values = provider.parse_day(payloads_for("2026-11-05"))

    assert values == {
        "sleep_seconds": 26100,
        "deep_sleep_seconds": 4500,
        "light_sleep_seconds": 15300,
        "rem_sleep_seconds": 6300,
        "awake_sleep_seconds": 1200,
        "sleep_score": 81,
        "hrv_last_night": 52,
        "hrv_weekly_avg": 54,
        "hrv_status": "BALANCED",
        "hrv_baseline_low": 48,
        "hrv_baseline_high": 61,
        "resting_hr": 46,  # aus dailyHeartRate, nicht das Tagesminimum 41
        "body_battery_high": 87,
        "body_battery_low": 18,
        "stress_avg": 27,
        "steps": 11234,
        "weight_kg": 81.2,
        "fat_ratio_pct": 17.4,
        "muscle_mass_kg": 37.1,
    }


def test_missing_values_stay_null_never_zero(provider):
    payloads = payloads_for("2026-11-04")
    payloads["daily_heart_rate"] = {"restingHeartRate": 0}

    values = provider.parse_day(payloads)

    assert all(value is None for value in values.values())


def test_unmeasurable_values_become_null_but_real_zero_steps_stay(provider):
    values = provider.parse_day(payloads_for("2026-11-06"))

    assert values["stress_avg"] is None
    assert values["steps"] == 0
    assert values["sleep_score"] is None
    assert values["weight_kg"] == 81.1  # Gramm werden erkannt
    assert values["muscle_mass_kg"] is None
    assert values["fat_ratio_pct"] is None


def test_parse_day_without_any_data(provider):
    assert all(v is None for v in provider.parse_day({}).values())


def test_fetch_splits_ranges_into_days(provider):
    rows = {(r.endpoint, r.day.isoformat()): r.payload for r in provider.fetch(START, END).rows}

    assert rows["sleep_summaries", "2026-11-05"]["sleepTimeSeconds"] == 26100
    assert rows["hrv_summaries", "2026-11-05"]["lastNightAvg"] == 52
    assert rows["hrv_summaries", "2026-11-06"] is None
    assert [a["activityId"] for a in rows["activities", "2026-11-05"]] == [900002]
    assert [a["activityId"] for a in rows["activities", "2026-11-04"]] == [900001]
    assert rows["activities", "2026-11-06"] == []
    assert len(rows) == 3 * 5


def test_range_fetch_never_calls_heart_rate(provider, garmin_session):
    provider.fetch(START, END)

    assert not [url for url, _ in garmin_session.gets if "dailyHeartRate" in url]


def test_fetch_per_day_pauses_between_calls_only(provider, monkeypatch):
    waits = []
    monkeypatch.setattr("providers.garmin.time.sleep", waits.append)

    rows = list(provider.fetch_per_day([END, START]))

    assert [r.day for r in rows] == [END, START]
    assert rows[0].endpoint == "daily_heart_rate"
    assert rows[0].payload["restingHeartRate"] == 46
    assert waits == [2]


def test_fetch_per_day_stops_at_once_on_rate_limit(provider, garmin_session, monkeypatch):
    waits = []
    monkeypatch.setattr("providers.garmin.time.sleep", waits.append)
    garmin_session.fail_when = lambda url, params: 429 if params["date"] == "2026-11-05" else None

    rows = list(provider.fetch_per_day([END, date(2026, 11, 5), START]))

    assert [r.day for r in rows] == [END]
    assert len(garmin_session.gets) == 2
    assert waits == [2]  # kein Warten auf das Limit


def test_fetch_per_day_replaces_revoked_token(provider, garmin_session):
    garmin_session.failures = [401]

    assert len(list(provider.fetch_per_day([END]))) == 1
    assert garmin_session.token_requests == 2


def test_fetch_per_day_raises_other_errors_after_yielding_loaded_days(
    provider, garmin_session, monkeypatch
):
    monkeypatch.setattr("providers.garmin.time.sleep", lambda _: None)
    garmin_session.fail_when = lambda url, params: 500 if params["date"] == "2026-11-04" else None
    rows = provider.fetch_per_day([END, START])

    assert next(rows).day == END
    with pytest.raises(HttpError):
        next(rows)


def test_fetch_handles_empty_no_content_response(provider, garmin_session):
    garmin_session.overrides["hrv_summaries"] = None

    rows = provider.fetch(START, END).rows

    assert all(r.payload is None for r in rows if r.endpoint == "hrv_summaries")


def test_fetch_pages_through_all_activities(provider, garmin_session, monkeypatch):
    monkeypatch.setattr("providers.garmin.ACTIVITY_PAGE_SIZE", 2)
    pages = {
        0: [_activity(1), _activity(2)],
        2: [_activity(3), _activity(4)],
        4: [_activity(5)],
    }
    garmin_session.overrides["activities"] = lambda params: pages[params["start"]]

    rows = provider.fetch(START, END).rows

    found = [a["activityId"] for r in rows if r.endpoint == "activities" for a in r.payload]
    assert sorted(found) == [1, 2, 3, 4, 5]


def test_parse_activities(provider):
    payload = fixture("garmin/activities.json")

    run, strength = provider.parse_activities({"activities": payload})

    assert run["activity_id"] == 900002
    assert run["date"] == "2026-11-05"
    assert run["type"] == "running"
    assert run["distance_m"] == 8012.4
    assert run["aerobic_te"] == 3.4
    assert run["training_load"] == 112.3
    assert strength["distance_m"] is None
    assert strength["max_hr"] is None


def test_waits_and_retries_on_rate_limit(provider, garmin_session, monkeypatch):
    waits = []
    monkeypatch.setattr("providers.garmin.time.sleep", waits.append)
    garmin_session.failures = [429, 429]

    provider.fetch(START, END)

    assert waits == [60, 120]


def test_gives_up_after_repeated_rate_limit(provider, garmin_session, monkeypatch):
    monkeypatch.setattr("providers.garmin.time.sleep", lambda _: None)
    garmin_session.failures = [429] * 4

    with pytest.raises(HttpError):
        provider.fetch(START, END)


def test_revoked_token_is_replaced_once(provider, garmin_session):
    garmin_session.failures = [401]

    provider.fetch(START, END)

    assert garmin_session.token_requests == 2


def test_other_http_errors_are_raised(provider, garmin_session):
    garmin_session.failures = [500]

    with pytest.raises(HttpError):
        provider.fetch(START, END)


def _activity(activity_id: int) -> dict:
    return {"activityId": activity_id, "startTimeLocal": "2026-11-05 10:00:00"}
