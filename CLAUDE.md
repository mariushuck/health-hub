# Health-Hub

Holt Garmin- und Withings-Daten, speichert sie in SQLite und liefert per Telegram ein Bild-Dashboard und jeden Morgen einen KI-Bericht. Läuft auf einem Raspberry Pi 5 (8 GB, 64-Bit-OS) in Docker.

Der ausführliche Plan mit Begründungen liegt in `docs/PLAN.md`. Diese Datei hier ist verbindlich, wenn sich beide widersprechen.

## Grundprinzipien

- **Eine Instanz = eine Person.** Keine Nutzerverwaltung, keine Mandanten-IDs im Code. Mehrere Personen laufen als mehrere Compose-Projekte aus demselben Image (`PERSON=marius docker compose -p hub-marius up -d`).
- **Kein Webserver, kein offener Port.** Alle Verbindungen gehen vom Pi aus: Pull bei Garmin und Withings, Long Polling bei Telegram.
- **Geräteunabhängig.** Der Code kennt Konten (Garmin, Withings), keine Geräte. Pro Kennzahl legt die Config eine Quellen-Reihenfolge fest, z.B. `body: [withings, garmin]`. Die erste Quelle mit Daten gewinnt. Fehlende Kennzahlen lassen Kacheln und Berichtszeilen einfach wegfallen.
- **Python rechnet, die KI formuliert nur.** Jede Zahl im Bericht stammt aus einem JSON, das Python gebaut hat.
- **Fehlende Werte bleiben `NULL`**, nie 0.

## Stack

- Python 3.13 im Container, Abhängigkeiten mit `uv`
- Garmin: `garmin-connect-personal-api-python` (Personal API, Key mit Laufzeit, Access Token 24 h, nur Pull). Feldnamen stammen aus der Swagger-Spec, gegen die echte API noch unbestätigt, vor allem `sleepScores.overall` und die Einheit von `muscleMass`.
  - Sync und Backfill, Backfill in 28-Tage-Fenstern: `get_daily_sleep_summaries` (höchstens 31 Tage), `get_daily_summaries` (höchstens 1 Jahr; Schritte, Stress, Body Battery), `get_daily_hrv_summaries` und `get_weight_measurements` (keine Grenze genannt), `search_activities` (blättern).
  - Nur im normalen Sync: `get_daily_heart_rate` für den Ruhepuls, je Tag ein Aufruf, nie im Backfill. Die aktuellen Tage und danach fehlende ältere, die neuesten zuerst, zusammen höchstens 30 pro Lauf, mit 2 s Pause. Bei HTTP 429 abbrechen, der nächste Lauf macht weiter. Nicht `heartRate.restingValue` nutzen, das fällt auf das Tagesminimum zurück.
- Withings: eigenes kleines Modul mit `httpx`. OAuth2, Access Token 3 h, Refresh Token rotiert: **neuen Refresh Token sofort speichern**, bevor irgendetwas anderes passiert. Callback für `setup_withings.py`: `http://localhost:8765/callback`, genau so in der Withings-App eintragen. Messwert = `value * 10**unit`. `getmeas` nur mit `lastupdate` (der erste Abruf mit 0 holt die ganze Historie), kein `startdate`/`enddate`, so lange abfragen, bis `more = 0`. Dazu `category=1`, Gruppen mit `attrib` 1 und 4 ignorieren. Höchstens alle 10 Minuten.
- SQLite, eine Datei pro Instanz unter `/data`
- `python-telegram-bot` mit Long Polling und JobQueue (kein Cron)
- `matplotlib` für Karten (PNG 1080×1350, dunkles Design), `pandas`, `scipy`
- `anthropic` SDK, Modell `claude-haiku-5-5`, Key aus `ANTHROPIC_API_KEY`. Aufruf gekapselt in `report_llm.py`.

## Konfiguration

- `.env.<person>` im Repo-Root (Rechte 600, nie committen): nur feste Werte, die sich nie von selbst ändern: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GARMIN_PERSONAL_API_KEY`, `GARMIN_KEY_EXPIRES` (Datum `YYYY-MM-DD`, die API nennt es nicht, der Sync legt es in `sync_state` ab), `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, `ANTHROPIC_API_KEY` (für alle Instanzen derselbe)
- Alles, was sich zur Laufzeit ändert, liegt im Volume `data/<person>/` (im Container `/data`): `hub.db`, `withings_token.json` (Refresh Token, rotiert, wird atomar überschrieben), Garmin-Token-Cache. Die `.env` wird zur Laufzeit nie beschrieben.
- `data/<person>/config.yaml` je Instanz: Zeitzone `Europe/Berlin`, Berichtszeit (Standard `09:00`), Quellen-Reihenfolge, Theme, Trainingsplan, Ziel. Vorlage: `config.example.yaml` im Repo.
- Jede Instanz hat einen eigenen Telegram-Bot und antwortet nur ihrer `TELEGRAM_CHAT_ID`.

## Datenmodell

`raw_payload` (Original-JSON je Quelle, Endpunkt, Datum) → `daily_metrics` (eine Zeile pro Tag) · `activities` · `reports` · `sync_state` · später `daily_features`, `hypotheses`, `insights`.

Garmin-Bereichsantworten liegen pro Tag in `raw_payload`, Withings-Messgruppen einzeln (Endpunkt `getmeas/<grpid>`), weil `lastupdate` nur Geändertes liefert. Tageswerte werden aus den Rohdaten neu berechnet, damit sich die Quellen-Reihenfolge auch bei später eintreffenden Daten durchsetzt.

Schreiben immer per Upsert, nur in Garmin gelöschte Aktivitäten werden auch lokal gelöscht. Jeder Garmin-Sync holt die letzten 3 Tage neu, Withings holt nur Änderungen seit `lastupdate`. Schlaf und HRV zählen für den Morgen des Aufwachens.

## Zeitplan

Sync 08:45, 13:00, 21:00 (zweite Instanz 10 min versetzt). Bericht ab Berichtszeit, alle 15 min prüfen, ob Schlafdaten da sind, spätestens 2,5 h danach senden. Wochenbericht sonntags 18:00.

## Arbeitsweise

- Phasenweise bauen, nur die Phase, die gerade dran ist (siehe `docs/PLAN.md`, Abschnitt „Reihenfolge“).
- Tests mit `pytest`. API-Antworten als Fixtures speichern, keine echten Calls in Tests.
- Commits nach Conventional Commits: `<type>(<scope>): <beschreibung>`, Beschreibung unter 50 Zeichen, imperativ, klein geschrieben. Erlaubte Types: feat, fix, docs, style, refactor, chore.
