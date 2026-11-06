# Health-Hub

Holt Garmin- und Withings-Daten einer Person und speichert sie in einer lokalen SQLite-Datei. Läuft in Docker auf einem Raspberry Pi 5, ohne Webserver und ohne offenen Port: Alle Verbindungen gehen vom Pi aus.

**Stand: Phase 1 „Daten“.** Telegram-Dashboard und KI-Bericht sind geplant, aber noch nicht gebaut. Der Code ist getestet, lief aber noch nicht gegen die echten APIs. Welche Feldnamen deshalb noch unbestätigt sind, steht in [`docs/PLAN.md`](docs/PLAN.md) unter „Prüfung gegen die APIs“.

## Voraussetzungen

- Docker mit Compose auf dem Pi (64-Bit-OS)
- [`uv`](https://docs.astral.sh/uv/) auf dem Laptop, für den Withings-Login und die Tests
- Ein Key für die [Garmin Personal API](https://connect.garmin.com/app/settings/personalApiToken)
- Für Withings zusätzlich eine App im [Developer Dashboard](https://developer.withings.com/dashboard/)

## Instanz einrichten

Jede Person bekommt eine eigene Instanz aus demselben Image. Ersetze `<person>` durch einen Namen, z.B. `marius`.

**1. Secrets anlegen**

```bash
cp .env.example .env.<person>
chmod 600 .env.<person>
```

Trage in `.env.<person>` den Garmin-Key und sein Ablaufdatum (`GARMIN_KEY_EXPIRES`) ein, bei Bedarf auch die Withings-Zugangsdaten. Die Werte für Telegram und Claude werden in Phase 1 noch nicht gebraucht.

**2. Konfiguration anlegen**

```bash
mkdir -p data/<person>
cp config.example.yaml data/<person>/config.yaml
```

Unter `sources` steht je Kennzahl die Reihenfolge der Quellen. Die erste Quelle mit Daten gewinnt. Ohne Withings-Waage trägst du zum Beispiel `body: [garmin]` ein, dann braucht die Instanz keinen Withings-Token. Der Container läuft als Nutzer 1000, ihm muss `data/<person>/` gehören.

**3. Withings-Login (nur mit Withings-Waage)**

Trage in der Withings-App genau diesen Callback ein: `http://localhost:8765/callback`. Führe dann auf dem Laptop aus:

```bash
uv run --env-file .env.<person> setup_withings.py data/<person>
```

Die Person meldet sich im Browser an. Kopiere danach `data/<person>/withings_token.json` auf den Pi in dasselbe Verzeichnis. Lehnt Withings `localhost` ab, gibt es eine Ausweichlösung mit `--redirect-uri`, siehe Plan unter „Risiken“.

**4. Starten**

```bash
PERSON=<person> docker compose -p hub-<person> up -d --build
```

Der Sync läuft zu den Zeiten aus `sync_times`, nicht schon beim Start.

**5. Historie laden**

```bash
PERSON=<person> docker compose -p hub-<person> run --rm hub \
  /app/.venv/bin/python sync.py --backfill YYYY-MM-DD
```

Das lädt die Garmin-Historie in 28-Tage-Fenstern und die ganze Withings-Historie. Der Ruhepuls fehlt dabei absichtlich: Ihn laden die normalen Syncs nach, höchstens 30 Tage pro Lauf.

## Daten

Alles liegt in `data/<person>/` und bleibt außerhalb von Git:

| Datei | Inhalt |
| --- | --- |
| `hub.db` | SQLite mit `raw_payload`, `daily_metrics`, `activities` und `sync_state` |
| `withings_token.json` | Withings-Token, rotiert und wird vom Container selbst erneuert |
| `garmin_token.json` | Cache für den Garmin-Token |
| `config.yaml` | Einstellungen der Instanz |

Ob der letzte Sync geklappt hat:

```bash
sqlite3 data/<person>/hub.db "select * from sync_state"
```

## Entwicklung

```bash
uv sync
uv run pytest
```

Die Tests machen keine echten API-Aufrufe. Die Fixtures sind aus den offiziellen Schemas nachgebaut, siehe [`tests/fixtures/README.md`](tests/fixtures/README.md). Plan und Begründungen stehen in [`docs/PLAN.md`](docs/PLAN.md), die verbindlichen Regeln in [`CLAUDE.md`](CLAUDE.md).
