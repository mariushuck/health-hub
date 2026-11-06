"""Liest data/<person>/config.yaml und prüft, was der Sync davon braucht."""

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from db import GROUPS

PROVIDER_NAMES = {"garmin", "withings"}
ACTIVITIES = "activities"


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    sync_times: tuple[time, ...]
    sources: dict[str, list[str]]


def load(data_dir: Path) -> Config:
    raw = yaml.safe_load((data_dir / "config.yaml").read_text(encoding="utf-8"))
    sources = raw["sources"]
    # Ein Tippfehler in der Config würde sonst stumm eine leere Kennzahl erzeugen.
    if unknown := set(sources) - set(GROUPS) - {ACTIVITIES}:
        raise ValueError(f"Unbekannte Kennzahl in sources: {sorted(unknown)}")
    if unknown := {name for names in sources.values() for name in names} - PROVIDER_NAMES:
        raise ValueError(f"Unbekannte Quelle in sources: {sorted(unknown)}")
    return Config(
        timezone=ZoneInfo(raw["timezone"]),
        sync_times=tuple(time.fromisoformat(t) for t in raw["sync_times"]),
        sources=sources,
    )
