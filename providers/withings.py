"""Withings Public API: OAuth2 mit rotierendem Refresh Token und getmeas."""

import json
import os
import time
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from providers import Fetched, RawRow

API_URL = "https://wbsapi.withings.net"
AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
SCOPE = "user.metrics"
TOKEN_FILE = "withings_token.json"
EXPIRY_MARGIN_S = 60
# Eine Rohdaten-Zeile je Messgruppe: lastupdate liefert nur Geändertes, eine Zeile je Tag
# würde sonst bei der zweiten Wägung die erste überschreiben.
ENDPOINT_PREFIX = "getmeas/"
MEASURE_TYPES = {1: "weight_kg", 6: "fat_ratio_pct", 76: "muscle_mass_kg"}
# 1: Gerät erkennt die Person nicht sicher, etwa Gäste auf der Waage.
# 4: bei der Kontoanlage eingetragen, laut Withings womöglich ungenau.
UNTRUSTED_ATTRIB = {1, 4}


class WithingsError(Exception):
    pass


def request_token(client: httpx.Client, token_path: Path, grant: dict[str, str]) -> dict[str, Any]:
    """Holt einen Token und speichert ihn, bevor irgendetwas anderes passiert."""
    body = _post(
        client,
        "/v2/oauth2",
        {
            "action": "requesttoken",
            "client_id": os.environ["WITHINGS_CLIENT_ID"],
            "client_secret": os.environ["WITHINGS_CLIENT_SECRET"],
            **grant,
        },
    )
    token = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": int(time.time()) + int(body["expires_in"]),
        "userid": body.get("userid"),
        "scope": body.get("scope"),
    }
    _write_atomic(token_path, json.dumps(token))
    return token


class WithingsProvider:
    name = "withings"
    max_window_days = None  # lastupdate blättert selbst, die Historie braucht keine Fenster.
    min_interval_s = 600  # Withings: höchstens alle 10 Minuten pro Nutzer
    per_day_endpoint = None
    per_day_anchor = None

    def __init__(
        self, data_dir: Path, tz: ZoneInfo, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._token_path = data_dir / TOKEN_FILE
        self._tz = tz
        self._transport = transport

    def fetch(self, start: date, end: date, cursor: int | None = None) -> Fetched:
        """start und end gelten hier nicht: lastupdate liefert alles seit dem letzten Abruf,
        ohne Cursor (0) die ganze Historie."""
        with httpx.Client(transport=self._transport, timeout=30) as client:
            groups, updatetime = self._getmeas(client, self._access_token(client), cursor or 0)
        rows = [RawRow(f"{ENDPOINT_PREFIX}{g['grpid']}", self._day(g), g) for g in groups]
        return Fetched(rows, updatetime)

    def fetch_per_day(self, days: list[date]) -> Iterator[RawRow]:
        return iter(())

    def parse_day(self, payloads: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        groups = [p for endpoint, p in payloads.items() if endpoint.startswith(ENDPOINT_PREFIX)]
        # aufsteigend: Je Messwert gewinnt die letzte Messung des Tages.
        for group in sorted(groups, key=lambda g: g["date"]):
            if group.get("attrib") in UNTRUSTED_ATTRIB:
                continue
            for measure in group.get("measures") or []:
                if column := MEASURE_TYPES.get(measure["type"]):
                    values[column] = round(measure["value"] * 10 ** measure["unit"], 4)
        return values

    def parse_activities(self, payloads: dict[str, Any]) -> list[dict[str, Any]]:
        return []  # shortcut: nur Körperdaten, für Withings-Uhren hier einhängen.

    def _access_token(self, client: httpx.Client) -> str:
        try:
            token = json.loads(self._token_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise WithingsError(
                f"{self._token_path} fehlt, zuerst setup_withings.py ausführen"
            ) from None
        if token["expires_at"] - time.time() > EXPIRY_MARGIN_S:
            return token["access_token"]
        grant = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
        return request_token(client, self._token_path, grant)["access_token"]

    def _getmeas(
        self, client: httpx.Client, access_token: str, lastupdate: int
    ) -> tuple[list[dict[str, Any]], int]:
        groups: list[dict[str, Any]] = []
        # Zeit der ersten Seite: Was sich beim Blättern ändert, kommt beim nächsten Abruf.
        updatetime = None
        data: dict[str, Any] = {
            "action": "getmeas",
            "meastypes": ",".join(map(str, MEASURE_TYPES)),
            "category": 1,  # 2 wären Ziele des Nutzers, keine Messungen.
            "lastupdate": lastupdate,
        }
        while True:
            body = _post(client, "/measure", data, access_token)
            updatetime = updatetime or int(body["updatetime"])
            groups += body.get("measuregrps") or []
            if not body.get("more"):
                return groups, updatetime
            data["offset"] = body["offset"]

    def _day(self, group: dict[str, Any]) -> date:
        return datetime.fromtimestamp(group["date"], self._tz).date()


def _post(
    client: httpx.Client, path: str, data: dict[str, Any], access_token: str | None = None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    response = client.post(f"{API_URL}{path}", data=data, headers=headers)
    response.raise_for_status()
    payload = response.json()
    # Withings meldet Fehler mit HTTP 200 im Feld status.
    if payload.get("status") != 0:
        raise WithingsError(f"{path} {data.get('action')}: status {payload.get('status')}")
    return payload["body"]


def _write_atomic(path: Path, content: str) -> None:
    """Ein Stromausfall darf den Token nicht zerstören: Der alte gilt nach Rotation nur 8 h."""
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
