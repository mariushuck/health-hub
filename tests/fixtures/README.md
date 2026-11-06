# Fixtures

Nachgebaut aus den offiziellen Schemas (Garmin-Swagger, Withings-OpenAPI), **nicht** von echten
Konten aufgezeichnet. Sobald echte Antworten vorliegen, hier ersetzen und persönliche Werte
und IDs vorher entfernen. Die Garmin-Antworten lassen sich so holen:

    gc-personal sleep-summaries --start-date 2026-11-04 --end-date 2026-11-06

Abweichungen vom Schema, die Absicht sind:

- `sleepScores.overall`: Die Spec nennt die Schlüssel dieser Map nicht.
- `muscleMass` in Gramm: Die Spec nennt nur `int32` ohne Einheit.
