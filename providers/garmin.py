"""Garmin Personal API. Feldnamen und Fenster stammen aus der Swagger-Spec von Garmin."""

import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests
from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator
from garmin_personal_api.errors import HttpError

from providers import Fetched, RawRow

log = logging.getLogger(__name__)

ACTIVITY_PAGE_SIZE = 99  # Standardwert der API
RATE_LIMIT_WAITS_S = (60, 120, 240)
PER_DAY_PAUSE_S = 2


class GarminProvider:
    name = "garmin"
    # Die Spec nennt 31 Tage für Schlaf, 1 Jahr für Tageszusammenfassungen und nichts
    # für HRV und Gewicht. 28 ist die kleinste dort genannte Grenze aller Endpunkte.
    max_window_days = 28
    min_interval_s = 0
    # Ruhepuls gibt es ohne Rückfall auf das Tagesminimum nur pro Einzeltag. Das Limit ist
    # undokumentiert, deshalb nie im Backfill, sondern in kleinen Portionen je Sync.
    per_day_endpoint = "daily_heart_rate"
    per_day_anchor = "daily_summaries"

    def __init__(self, data_dir: Path, session: requests.Session | None = None) -> None:
        self._cache_file = data_dir / "garmin_token.json"
        self._session = session

    @contextmanager
    def _connect(self, wait_on_429: bool = True) -> Iterator[tuple[Any, Callable[..., Any]]]:
        key = os.environ["GARMIN_PERSONAL_API_KEY"]
        with PersonalApiAuthenticator(
            bearer_token_cache_file=self._cache_file, session=self._session
        ) as auth:
            token = auth.obtain_token(key).access_token
            with GarminPersonalApiClient(token, session=self._session) as client:

                def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
                    refreshed = False
                    for wait_s in RATE_LIMIT_WAITS_S:
                        try:
                            return fn(*args, **kwargs)
                        except HttpError as error:
                            if error.status_code == 429 and wait_on_429:
                                time.sleep(wait_s)
                            elif error.status_code == 401 and not refreshed:
                                # Der Client wiederholt nicht, und der Cache hielte den
                                # widerrufenen Token sonst bis zu 24 h.
                                fresh = auth.obtain_token(key, force_refresh=True)
                                client.update_bearer_token(fresh.access_token)
                                refreshed = True
                            else:
                                raise
                    return fn(*args, **kwargs)

                yield client, call

    def fetch(self, start: date, end: date, cursor: int | None = None) -> Fetched:
        with self._connect() as (client, call):
            return Fetched(_fetch_rows(client, call, start, end))

    def fetch_per_day(self, days: list[date]) -> Iterator[RawRow]:
        """Liefert Tag für Tag, damit bei einem Abbruch das Geholte erhalten bleibt."""
        try:
            with self._connect(wait_on_429=False) as (client, call):
                for index, day in enumerate(days):
                    if index:
                        time.sleep(PER_DAY_PAUSE_S)
                    payload = call(client.get_daily_heart_rate, day)
                    yield RawRow(self.per_day_endpoint, day, payload)
        except HttpError as error:
            if error.status_code != 429:
                raise
            log.warning("garmin: Ratenlimit beim Ruhepuls, nächster Lauf macht weiter")

    def parse_day(self, payloads: dict[str, Any]) -> dict[str, Any]:
        sleep = payloads.get("sleep_summaries") or {}
        if not sleep.get("sleepTimeSeconds"):
            sleep = {}  # Eine Nacht mit 0 Sekunden ist eine Nacht ohne Messung.
        hrv = payloads.get("hrv_summaries") or {}
        heart = payloads.get("daily_heart_rate") or {}
        summary = payloads.get("daily_summaries") or {}
        if summary.get("includesWellnessData") is False:
            summary = {}
        weight = (payloads.get("weight_range") or {}).get("latestWeight") or {}
        return {
            "sleep_seconds": sleep.get("sleepTimeSeconds"),
            "deep_sleep_seconds": sleep.get("deepSleepSeconds"),
            "light_sleep_seconds": sleep.get("lightSleepSeconds"),
            "rem_sleep_seconds": sleep.get("remSleepSeconds"),
            "awake_sleep_seconds": sleep.get("awakeSleepSeconds"),
            # Die Spec nennt die Schlüssel von sleepScores nicht, "overall" ist unbestätigt.
            "sleep_score": _dig(sleep, "sleepScores", "overall", "value"),
            "hrv_last_night": hrv.get("lastNightAvg"),
            "hrv_weekly_avg": hrv.get("weeklyAvg"),
            "hrv_status": hrv.get("status"),
            "hrv_baseline_low": _dig(hrv, "baseline", "balancedLow"),
            "hrv_baseline_high": _dig(hrv, "baseline", "balancedUpper"),
            # Nicht heartRate.restingValue aus den Tageszusammenfassungen: Das fällt laut
            # Spec auf das Tagesminimum zurück, hier muss ein fehlender Wert NULL bleiben.
            "resting_hr": heart.get("restingHeartRate") or None,
            "body_battery_high": _dig(summary, "bodyBattery", "maxValue"),
            "body_battery_low": _dig(summary, "bodyBattery", "minValue"),
            "stress_avg": _stress(_dig(summary, "stress", "avgLevel")),
            "steps": _dig(summary, "movement", "steps", "value"),
            "weight_kg": _kg(weight.get("weight")),
            "fat_ratio_pct": weight.get("bodyFat") or None,
            "muscle_mass_kg": _kg(weight.get("muscleMass")),
        }

    def parse_activities(self, payloads: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "activity_id": a["activityId"],
                "date": a["startTimeLocal"][:10],
                "start_local": a["startTimeLocal"],
                "type": _dig(a, "activityType", "typeKey"),
                "name": a.get("activityName"),
                "duration_s": a.get("duration"),
                "distance_m": a.get("distance"),
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "aerobic_te": a.get("aerobicTrainingEffect"),
                "anaerobic_te": a.get("anaerobicTrainingEffect"),
                "training_load": a.get("activityTrainingLoad"),
            }
            for a in payloads.get("activities") or []
        ]


def _fetch_rows(
    client: GarminPersonalApiClient, call: Callable[..., Any], start: date, end: date
) -> list[RawRow]:
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    sleep = call(client.get_daily_sleep_summaries, start, end) or []
    hrv = (call(client.get_daily_hrv_summaries, start, end) or {}).get("hrvSummaries") or []
    summaries = (call(client.get_daily_summaries, start, end) or {}).get("data") or []
    weights = call(client.get_weight_measurements, start, end) or {}
    activities = _fetch_activities(client, call, start, end)

    by_day = {
        "sleep_summaries": {x["calendarDate"]: x for x in sleep},
        "hrv_summaries": {x["calendarDate"]: x for x in hrv},
        "daily_summaries": {x["calendarDate"]: x for x in summaries},
        "weight_range": {x["summaryDate"]: x for x in weights.get("dailyWeightSummaries") or []},
    }
    rows = [
        RawRow(endpoint, day, items.get(day.isoformat()))
        for endpoint, items in by_day.items()
        for day in days
    ]
    for day in days:
        day_activities = [a for a in activities if a["startTimeLocal"][:10] == day.isoformat()]
        rows.append(RawRow("activities", day, day_activities))
    return rows


def _fetch_activities(
    client: GarminPersonalApiClient, call: Callable[..., Any], start: date, end: date
) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    while True:
        page = call(
            client.search_activities,
            start=len(activities),
            limit=ACTIVITY_PAGE_SIZE,
            start_date=start,
            end_date=end,
        )
        activities += page or []
        if not page or len(page) < ACTIVITY_PAGE_SIZE:
            return activities


def _dig(data: dict[str, Any], *path: str) -> Any:
    for key in path:
        data = (data or {}).get(key)
    return data


def _stress(level: int | None) -> int | None:
    # Andere Garmin-Dienste kodieren "nicht messbar" als -1 oder -2.
    return level if level is not None and level >= 0 else None


def _kg(value: float | None) -> float | None:
    # Die Spec nennt für weight kg, für muscleMass als int32 gar keine Einheit (vermutlich
    # Gramm). Über 1000 ist nie ein Körperwert in kg, ein 0 nie eine echte Messung.
    if not value:
        return None
    return value / 1000 if value > 1000 else value
