"""Gemeinsames Interface der Datenquellen.

Der Sync kennt nur dieses Interface. fetch holt und liefert Rohdaten je Tag,
parse_day und parse_activities übersetzen sie ohne Netz und ohne Zugangsdaten.
So lassen sich Tageswerte jederzeit aus raw_payload neu berechnen, auch wenn
eine Quelle gerade ausfällt.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RawRow:
    endpoint: str
    day: date
    payload: Any  # None heißt: Die Quelle hatte für diesen Tag nichts.


@dataclass(frozen=True)
class Fetched:
    rows: list[RawRow]
    cursor: int | None = None


class Provider(Protocol):
    name: str
    max_window_days: int | None  # None: Der ganze Zeitraum passt in einen Abruf.
    min_interval_s: int
    # Endpunkt, den es nur pro Einzeltag gibt. Er läuft nie im Backfill. Fehlende Tage sind
    # die, für die per_day_anchor Daten hat, per_day_endpoint aber noch keine Zeile.
    per_day_endpoint: str | None
    per_day_anchor: str | None

    def fetch(self, start: date, end: date, cursor: int | None) -> Fetched: ...

    def fetch_per_day(self, days: list[date]) -> Iterator[RawRow]: ...

    def parse_day(self, payloads: dict[str, Any]) -> dict[str, Any]: ...

    def parse_activities(self, payloads: dict[str, Any]) -> list[dict[str, Any]]: ...


def create(name: str, data_dir: Path, tz: ZoneInfo) -> Provider:
    if name == "garmin":
        from providers.garmin import GarminProvider

        return GarminProvider(data_dir)
    if name == "withings":
        from providers.withings import WithingsProvider

        return WithingsProvider(data_dir, tz)
    raise ValueError(f"Unbekannte Quelle: {name}")
