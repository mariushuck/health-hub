# Health-Hub: Garmin + Withings auf dem Raspberry Pi

Nov 6, 2026 · @Marius

## Ziel und Umfang

Ihr bekommt beide jeden Morgen einen KI-Bericht und jederzeit ein Dashboard in Telegram, jeweils aus euren eigenen Garmin- und Withings-Daten auf deinem Raspberry Pi 5.

Die wichtigste Entscheidung: Garmin läuft über die neue offizielle Personal API statt über inoffizielle Logins. Damit braucht der Pi im Dauerbetrieb keinen offenen Port. Er holt alle Daten selbst ab und spricht Telegram per Long Polling an.

**Version 1 liefert:**

- Tägliche Synchronisation von Garmin und Withings in eine lokale Datenbank
- Telegram-Befehle `/heute`, `/woche` und `/trend` mit Diagrammen als Bild
- Einen KI-Bericht jeden Morgen mit Trends, sobald die Schlafdaten da sind
- Warnungen, wenn ein Sync ausfällt oder ein Schlüssel bald abläuft

**Bewusst nicht in Version 1:** Zusammenhangsanalyse (kommt als Phase 5), Hevy-Anbindung, Schreiben auf die Uhr und jede medizinische Bewertung. Keine App, kein Webserver und keine Nutzerverwaltung: Alles läuft über Telegram-Befehle und Konfigurationsdateien.

## Architektur

Ein Container pro Person übernimmt alles: Sync, Datenbank, Bericht und Bot.

```
Garmin Personal API ─┐
Withings Public API ─┴─▶ Sync-Job ─▶ SQLite ─┬─▶ KI-Bericht-Job ◀─▶ Claude API (Haiku 5.5)
                                              │          │
                                              └─▶ Telegram-Bot ◀─▶ Telegram ◀─▶ Handy
(alles im Container einer Person, alle Verbindungen gehen vom Pi aus)
```

Die Pfeile zeigen den Datenfluss, aufgebaut wird jede Verbindung vom Pi aus. Auch der einmalige Withings-Login braucht keinen offenen Port, er läuft über ein Skript auf deinem Laptop.

## Zwei Instanzen auf einem Pi

Ja, das geht, und es passt besser zu deinen Wünschen als eine Mehrnutzer-App. Du startest dasselbe Image zweimal, und jede Instanz kennt genau eine Person. Der Code bleibt einfach, und Daten können nicht vermischt werden.

```yaml
# compose.yaml, einmal im Repo
services:
  hub:
    build: .
    image: health-hub:latest
    env_file: .env.${PERSON}
    volumes:
      - ./data/${PERSON}:/data
    restart: unless-stopped
```

```bash
PERSON=marius docker compose -p hub-marius up -d
PERSON=kumpel docker compose -p hub-kumpel up -d
```

| Bestandteil | Regel |
| --- | --- |
| Telegram-Bot | Eigener Bot je Instanz, weil sich zwei Prozesse mit demselben Token die Updates gegenseitig [wegnehmen](https://core.telegram.org/bots/api#getupdates) |
| Garmin | Eigener Key je Person |
| Withings | Eine gemeinsame Withings-App, aber ein eigener Refresh Token je Person |
| Claude API | Ein Key für beide reicht, getrennte Keys trennen die Kosten |
| Daten und Konfiguration | Eigenes Verzeichnis je Instanz |
| Code | Ein Image, ein Update für beide |

Dein Pi 5 mit 8 GB hat dafür reichlich Platz. Beide Container warten fast immer und arbeiten nur beim Sync und beim Rendern ein paar Sekunden. Ich schätze den Bedarf auf wenige hundert MB pro Instanz; mit `docker stats` misst du ihn in Phase 1 nach.

**Geräteunabhängig:**

- **Konten statt Geräte:** Die Software kennt ein Garmin-Konto und ein Withings-Konto. Welche Uhr oder Waage dorthin synchronisiert, ist egal.
- **Gemeinsames Schema:** Jede Quelle liefert, was sie hat, in dieselben Kennzahlen.
- **Reihenfolge je Kennzahl:** In der Konfiguration steht zum Beispiel `body: [withings, garmin]`. Die erste Quelle mit Daten gewinnt, so entstehen keine Doppelwerte.
- **Fehlende Kennzahlen:** Kachel, Berichtszeile und Hypothesen entfallen automatisch.
- **Erweiterbar:** Der Withings-Provider liefert zuerst Körperdaten. Schlaf und Aktivität von Withings-Uhren lassen sich später genauso einhängen.

| Kennzahl | Du | Dein Kumpel |
| --- | --- | --- |
| Schlaf, HRV, Ruhepuls, Stress, Body Battery, Aktivitäten | Garmin (Venu 4) | Garmin (Cirqa) |
| Gewicht, Fettanteil, Muskelmasse | Garmin (Index S2) | Withings (Body Smart) |

Die [Cirqa](https://www.electrocity.ie/product/garmin-cirqa-screenless-smart-band-activity-tracker-l-xl-mauve/) liefert laut Händler dieselben Kernwerte wie die Venu 4, nur ohne Display. Eine [offizielle Brücke von Withings zu Garmin](https://support.withings.com/hc/en-us/community/posts/360036420233-Garmin-Connect-partner-app-sync) gibt es nicht, deshalb braucht dein Kumpel beide Anbindungen.

**Neue Instanz anlegen, alles manuell:**

1. Bei @BotFather einen Bot anlegen und den Token in `.env.kumpel` eintragen.
2. Die Telegram-ID der Person eintragen. Nur sie bekommt Antworten.
3. Die Person erzeugt in Garmin Connect ihren Key und gibt ihn dir für `GARMIN_PERSONAL_API_KEY`, dazu das Ablaufdatum für `GARMIN_KEY_EXPIRES`.
4. Für Withings führst du einmal `setup_withings.py` auf deinem Laptop aus, die Person meldet sich im Browser an. Das Skript fängt den Code auf `http://localhost:8765/callback` ab und schreibt den Token als `withings_token.json` ins Datenverzeichnis der Person. Der Container erneuert ihn dort selbst, die `.env` bleibt unverändert.
5. In `data/<person>/config.yaml` stehen Berichtszeit, Zeitzone, Design, Trainingsplan und Ziel.
6. Instanz starten und einmal `sync.py --backfill <Datum>` ausführen, der erste Bericht kommt am nächsten Morgen.

Withings' eigenes [OAuth-Beispiel](https://github.com/withings-sas/api-oauth2-python) trägt mit `Environment = dev` eine `localhost`-Adresse als Callback ein. Die Adresse muss genau so in der Withings-App stehen wie im Skript. Klappt das nicht, gibt es eine Ausweichlösung, siehe Risiken.

## Datenquellen

Garmin liefert für euch beide Schlaf, Erholung und Training. Körperdaten kommen je nach Waage aus Garmin oder Withings.

| Bereich | Quelle | Aufruf | Wofür |
| --- | --- | --- | --- |
| Schlaf | Garmin | `get_daily_sleep_summaries` | Dauer, Phasen, Score |
| HRV | Garmin | `get_daily_hrv_summaries` | Nachtwert, Status, Baseline |
| Body Battery, Stress, Schritte | Garmin | `get_daily_summaries` | Erholung über den Tag, Tageswerte |
| Ruhepuls | Garmin | `get_daily_heart_rate`, nur im normalen Sync | Tageswert |
| Aktivitäten | Garmin | `search_activities` | Krafteinheiten und Läufe |
| Gewicht, Fettanteil, Muskelmasse | Garmin oder Withings | `getmeas` mit `lastupdate`, Typen 1, 6, 76, oder `get_weight_measurements` | Körpertrend |

Die beiden APIs unterscheiden sich vor allem beim Token-Handling ([Garmin FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/), [Garmin-Client](https://github.com/garmin/connect-personal-api-python), [Withings-Referenz](https://developer.withings.com/llms.md)):

| Eckdaten | Garmin Personal API | Withings Public API |
| --- | --- | --- |
| Zugang | Key selbst in Garmin Connect erzeugen | Eigene App im Developer Dashboard, kein Vertrag |
| Laufzeit | Key 30, 90 oder 180 Tage oder 1 Jahr, Access Token 24 h | Access Token 3 h, Refresh Token 1 Jahr |
| Erneuerung | Kein Refresh Token, neuer Token aus dem Key | Rotation: alter Refresh Token verfällt nach 8 h |
| Abruf | Nur Pull | Pull mit `lastupdate`, Webhooks optional |
| Limit | In der FAQ keine Zahl genannt | Höchstens alle 10 Minuten pro Nutzer |

**Grenze bei Garmin:** Training Readiness und Training Status stehen nicht in der Swagger-Spec der Personal API. Belastung und VO2max gibt es dort nur je Aktivität (`activityTrainingLoad`, `vO2MaxValue`). Ob sie gefüllt sind, zeigt erst die echte API. Der Bericht kommt in Version 1 ohne Tageswerte dazu aus und nutzt Schlaf, HRV, Ruhepuls und Aktivitäten.

Die Sportart ist egal: Python berechnet die Belastung jeder Einheit aus Dauer und Herzfrequenz. Kraft, Handball und „Sonstiges“ zählen dadurch gleich. Einheiten ohne Aufzeichnung fehlen aber ganz. Ein Krafttag sieht dann wie ein Ruhetag aus und zeigt sich höchstens indirekt über Stress, Body Battery und Ruhepuls am Folgetag.

Die inoffizielle Bibliothek `garminconnect` könnte diese Werte ergänzen, bleibt aber außen vor. Sie läuft laut [Garmin-MCP](https://glama.ai/mcp/servers/filipelimao/Garmin-MCP) außerhalb von Garmins Bedingungen, und ihr Unterbau `garth` gilt dort als nicht mehr gepflegt.

### Prüfung gegen die APIs

Beim Bau von Phase 1 wurden die Annahmen dieses Plans gegen die Swagger-Spec von Garmin, den Garmin-Client und die OpenAPI-Spec von Withings geprüft. Die Abschnitte dieses Plans sind bereits korrigiert, die Tabelle hält fest, was sich warum geändert hat. Gegen die echten APIs lief das noch nicht.

| Frühere Annahme | Tatsächlich | Umsetzung |
| --- | --- | --- |
| Belastung und VO2max gibt es nicht | Je Aktivität stehen `activityTrainingLoad` und `vO2MaxValue` im Schema, nur Readiness und Training Status fehlen | `training_load` wird gespeichert, VO2max bleibt in den Rohdaten |
| Body Battery über `get_daily_body_battery` | Der Endpunkt hat kein Tageshoch, nur `charged`, `drained` und Rohreihen | `bodyBattery.maxValue` aus `get_daily_summaries` |
| Stress über `get_daily_stress`, Schritte über `get_daily_summaries` | `get_daily_summaries` liefert auch Stress, bis zu 1 Jahr pro Abruf | Ein Aufruf deckt Schritte, Stress und Body Battery ab |
| Ruhepuls über `get_daily_summaries` oder `get_daily_heart_rate` | `restingValue` fällt auf das Tagesminimum zurück und verstößt gegen die NULL-Regel | Nur `get_daily_heart_rate`, nie im Backfill: aktuelle und fehlende Tage, höchstens 30 pro Lauf, 2 s Pause, bei HTTP 429 weiter im nächsten Lauf |
| Einzeltage für Schlaf und HRV, dazu `get_activity` | Es gibt Bereichs-Endpunkte, und die Aktivitätssuche enthält schon alle Felder | `get_daily_sleep_summaries`, `get_daily_hrv_summaries`, nur `search_activities` |
| Fenstergröße offen | Schlaf 31 Tage, Summaries 1 Jahr, HRV und Gewicht ohne Angabe; `get_daily_stress` und `get_daily_steps` wären auf 28 begrenzt | Backfill in 28-Tage-Fenstern |
| Withings: `lastupdate` und zugleich 3 Tage neu holen | Laut Spec gilt `lastupdate` statt `startdate` und `enddate` | Nur `lastupdate`, kein Fenster |
| Withings-Historie in kleinen Fenstern | `lastupdate` blättert selbst, viele Abrufe würden die 10-Minuten-Regel verletzen | Der erste Abruf mit `lastupdate=0` holt die ganze Historie |
| `getmeas` nur mit Messtypen | Ohne `category=1` können auch Nutzerziele kommen, `attrib=1` kann von anderen Personen stammen | `category=1`, Gruppen mit `attrib` 1 und 4 werden ignoriert |
| Ablaufdatum des Garmin-Keys kommt aus der API | Die Spec hat keinen Endpunkt dafür | `GARMIN_KEY_EXPIRES` in der `.env`, der Sync legt es in `sync_state` ab |
| `localhost` laut Drittprojekt erlaubt | Withings' eigenes Beispiel nutzt `http://localhost:5000/get_token` mit `Environment = dev` | Callback `http://localhost:8765/callback`, genau so in der Withings-App eintragen |
| `raw_payload` speichert die Antwort unverändert | Der Schlüssel Quelle, Endpunkt, Datum passt nicht zu Bereichsantworten und nicht zu `lastupdate` | Garmin pro Tag, Withings pro Messgruppe (`getmeas/<grpid>`), die Elemente bleiben unverändert |

**Fenster laut Spec:**

| Endpunkt | Höchstens |
| --- | --- |
| `get_daily_sleep_summaries`, `get_daily_body_battery` | 31 Tage |
| `get_daily_stress`, `get_daily_steps` | 28 Tage |
| `get_daily_summaries` | 1 Jahr |
| `get_daily_hrv_summaries`, `get_weight_measurements` | Keine Angabe |
| `search_activities` | Blättert mit `start` und `limit` |

**Noch unbestätigt:** Die Spec nennt weder die Schlüssel von `sleepScores` (angenommen: `overall`) noch die Einheit von `muscleMass` (`int32`, vermutlich Gramm). Ob `calendarDate` den Morgen des Aufwachens meint, zeigen erst echte Daten. Auch, dass `lastupdate=0` die ganze Historie liefert, ist bisher nur eine Annahme.

## Tech-Entscheidungen

Ein einziger Python-Container reicht: Er synchronisiert, rechnet und spricht mit Telegram. Er läuft mit Docker Compose wie dein mediastack.

| Baustein | Wahl | Grund | Alternative |
| --- | --- | --- | --- |
| Laufzeit | Docker Compose auf 64-Bit-OS | Kennst du schon; das Image bringt Python 3.13 unabhängig vom OS mit | systemd mit `uv` |
| Garmin-Client | [`garmin-connect-personal-api-python`](https://github.com/garmin/connect-personal-api-python) | Von Garmin selbst, verwaltet den Token-Cache | [`gcpapi-sync`](https://pypi.org/project/gcpapi-sync/), typisiert, aber privates Projekt |
| Withings-Client | Kleines eigenes Modul mit `httpx` | Nur OAuth und zwei Aufrufe nötig | Fertiges SDK als zusätzliche Abhängigkeit |
| Datenbank | SQLite | Ein Nutzer, wenige Schreibvorgänge, Backup ist eine Datei | Postgres, hier zu schwer |
| Bot | `python-telegram-bot` mit Long Polling | Keine eingehende Verbindung; die JobQueue ersetzt Cron | `aiogram` |
| Diagramme | `matplotlib`, PNG per `sendPhoto` | Läuft ohne Browser auf dem Pi | Plotly, mehr Abhängigkeiten |
| KI | Haiku 5.5 über die Claude API | Rund 0,03 USD im Monat, kein Training auf deinen Daten | Sonnet 5.5, rund 0,60 USD im Monat |
| Fernzugriff | Tailscale | Nutzt du schon, keine Portfreigabe nötig | Cloudflare Tunnel |
| Statistik | pandas und scipy | Decken Theil-Sen, Spearman und FDR ab, läuft auf dem Pi | statsmodels |

Docker löst auch die Python-Frage: Garmins Client braucht mindestens Python 3.12, Raspberry Pi OS Bookworm bringt laut [dieser Anleitung](https://aruljohn.com/info/python-raspberrypi) aber nur 3.11 mit.

## Datenmodell

Jede API-Antwort landet zuerst unverändert als JSON und erst danach in den Tageswerten. So kannst du neue Felder später auswerten, ohne alles erneut abzurufen.

| Tabelle | Schlüssel | Inhalt |
| --- | --- | --- |
| `raw_payload` | Quelle, Endpunkt, Datum | Original-JSON und Abrufzeit, Garmin je Tag, Withings je Messgruppe (`getmeas/<grpid>`) |
| `daily_metrics` | Datum | Eine Zeile pro Tag: Schlaf, HRV, Ruhepuls, Body Battery, Stress, Schritte, Gewicht, Fettanteil, Muskelmasse |
| `activities` | Quelle, Aktivitäts-ID | Datum, Typ, Dauer, Distanz, Puls, Training Effect, Belastung |
| `reports` | Datum | Eingabe-JSON, Text, Modell, Token, Versandzeit |
| `sync_state` | Quelle | Letzter Versuch, letzter Erfolg, letzter Fehler, Withings-`lastupdate`, Ablaufdatum des Garmin-Keys |
| daily\_features | Datum | Tageswerte ohne Eigentrend, Normalbereiche, Belastung, Schlafzeiten |
| hypotheses | ID | Auslöser, Zielgröße, Versatz, Herkunft (Katalog oder KI) |
| insights | Hypothese, Datum | Effekt, Intervall, n, q-Wert, Stabilität, zuletzt gesendet |

Jede Instanz hat genau eine Datenbankdatei in ihrem eigenen Datenverzeichnis.

**Regeln für die Pipeline:**

- Alle Schreibvorgänge sind Upserts, der Sync darf also beliebig oft laufen. Nur in Garmin gelöschte Aktivitäten werden auch lokal gelöscht.
- Jeder Garmin-Lauf holt die letzten 3 Tage neu, weil die Uhr Daten nachreichen kann. Withings holt nur Änderungen seit dem letzten `lastupdate`.
- Tageswerte werden aus den Rohdaten neu berechnet, so setzt sich die Quellen-Reihenfolge auch durch, wenn die bevorzugte Quelle später liefert.
- Schlaf und HRV zählen für den Morgen des Aufwachens, [wie bei Garmin](https://pypi.org/project/ownmetrics-mcp/).
- Fehlende Werte bleiben `NULL` und werden nie zu 0.
- Withings-Werte werden als `value × 10^unit` umgerechnet, und Abfragen laufen, bis `more = 0` ist.
- Die Garmin-Historie lädt `sync.py --backfill <Datum>` einmalig in 28-Tage-Fenstern, siehe „Prüfung gegen die APIs“. Withings holt sie schon beim ersten Abruf.
- Der Ruhepuls ist nicht Teil des Backfills. Jeder normale Sync lädt die aktuellen 3 Tage und 27 fehlende, die neuesten zuerst. Bei 3 Syncs am Tag sind das 81 Tage, 21 Monate Historie brauchen also rund 8 Tage.

## Telegram-Dashboard

Version 1 ist ein Bild-Dashboard: Du schickst einen Befehl, und der Bot antwortet mit einem Diagramm. Das braucht weder Webserver noch offenen Port.

| Befehl | Antwort |
| --- | --- |
| `/heute` | Kacheln für HRV, Ruhepuls, Schlaf, Body Battery und Gewicht, je gegen deinen Normalbereich |
| `/woche` | 4 Wochen Training, Schlaf, HRV und Ruhepuls untereinander |
| `/trend` | Gewicht, Muskelmasse, HRV und Ruhepuls, mit Buttons für 30, 90 und 365 Tage |
| `/bericht` | Heutigen KI-Bericht erneut senden |
| `/status` | Letzter Sync je Quelle und Ablaufdatum des Garmin-Keys aus `sync_state` |
| /warum | Diagramm und Kennzahlen zur letzten Aussage der KI |
| /help | Alle Befehle mit je einem Satz, passend zu deinen Daten |
| /profil | Aktuelle Einstellungen anzeigen, ändern läuft über die Konfiguration |
| /pause | Berichte und Warnungen aussetzen oder wieder starten |

Jede Instanz hat ihren eigenen Bot und antwortet nur der Telegram-ID aus ihrer Konfiguration. Telegram [bittet alle Bots](https://core.telegram.org/bots/features) um `/start` und `/help`.

`/help` listet alle Befehle mit je einem Satz und zeigt nur, wofür Daten vorhanden sind. Das Menü hinter `/` setzt der Bot beim Start mit `setMyCommands`.

## Darstellung

Jede Kennzahl erscheint als dein eigener Trend gegen dein eigenes Normalniveau, nie als einzelner Tageswert gegen eine allgemeine Norm. Tageswerte schwanken zu stark, und „HRV 52“ sagt ohne Vergleich nichts.

| Kennzahl | Darstellung | Warum |
| --- | --- | --- |
| Gewicht | Tagespunkte grau, geglättete Trendlinie (α = 0,1), Hauptzahl in kg pro Woche | Gewicht folgt einem [Wochenrhythmus](https://karger.com/ofa/article/7/1/36/240088/Weight-Rhythms-Weight-Increases-during-Weekends) und schwankt mit Darminhalt und Wasser; die [Glättung](https://fourmilab.ch/hackdiet/www/subsubsection1_2_4_0_4_4.html) filtert das heraus |
| Fettanteil, Muskelmasse | Nur Trendlinie über 90 Tage, nicht auf `/heute` | Bioimpedanz bildet laut [einer Studie](https://public-pages-files-2025.frontiersin.org/journals/sports-and-active-living/articles/10.3389/fspor.2025.1644082/text) Verläufe eher ab als Absolutwerte; gemessen wurden Smartwatch und InBody, nicht Index S2 oder Body Smart |
| HRV | Nachtpunkte, 7-Tage-Schnitt, Band aus 60 Tagen | Die [AIS-Leitlinie](https://www.ais.gov.au/__data/assets/pdf_file/0013/1130260/HRV-BPG_FINAL.pdf) empfiehlt logarithmierte Werte, 7-Tage-Schnitt und 60-Tage-Mittel ± 0,5 CV |
| Ruhepuls | Gleiche Logik wie HRV | Ein Verfahren für alles; die Übertragung ist meine Wahl |
| Schlaf | 7-Tage-Schnitt, dazu Streuung der Einschlafzeit | Eine kurze Nacht ist noch kein Trend |
| Body Battery | Tageshoch als Kontext | Garmin-Wert, dessen Berechnung du nicht siehst |
| Training | Minuten pro Tag, Laufkilometer pro Woche | Beides zählt für den Hyrox-Aufbau |

**Regeln für alle Karten:**

- **Format:** Hochformat mit 1080 × 1350 Pixeln, Text ab 30 Pixeln, das entspricht auf dem iPhone rund 11 Punkt.
- **Eine Skala pro Diagramm:** Training, Schlaf und HRV stehen untereinander auf derselben Zeitachse. Zwei Skalen in einem Bild erzeugen Zusammenhänge, die es nicht gibt.
- **Farbe nur mit Bedeutung:** Alles bleibt neutral, bis ein Wert den Normalbereich verlässt. Dann erscheint ein Symbol mit Text, nie Farbe allein.
- **Kein Gesamtscore:** Ein eigener Wert würde die Annahmen verstecken. Die AIS-Leitlinie rät auch bei Geräte-Diagnosen wie „übermäßig ermüdet“ zur Vorsicht.
- **Lücken bleiben Lücken:** Ein 7-Tage-Schnitt braucht mindestens 3 Messungen, sonst bleibt die Stelle leer.
- **Dunkles Design:** Der Bot kennt dein Telegram-Theme nicht, deshalb legst du es per `THEME` in der Konfiguration fest.

`/heute` zeigt jeden Morgen, was vom Normalbereich abweicht. `/trend` zeigt beim Gewicht zuerst das Tempo und erst danach den Wert.

`/woche` legt alles auf dieselbe Zeitachse. `/warum` belegt jede Aussage der KI mit den Einzelwerten und Kennzahlen dahinter.

## Trends und Zusammenhänge

Python rechnet und testet, die KI wählt aus, erklärt und schlägt neue Hypothesen vor. So findet sie Muster, ohne Zahlen zu erfinden, und du bekommst nur Aussagen, die einen Test bestanden haben.

**Trends, täglich:**

- **Abweichung:** Der 7-Tage-Schnitt wird mit dem Normalbereich verglichen. Gemeldet wird erst ab 3 Tagen außerhalb.
- **Steigung:** [Theil-Sen](https://bookdown.org/david_carslaw/openair/sections/trend-analysis/theil-sen.html) über 28 und 90 Tage ist robust gegen Ausreißer. Das Intervall kommt aus einem Block-Bootstrap, weil benachbarte Tage nicht unabhängig sind.
- **Schwelle:** Ein Trend zählt nur, wenn das Intervall die Null ausschließt und die Änderung im Alltag relevant ist.

**Zusammenhänge, wöchentlich:**

1. **Katalog statt Datenfischen:** Getestet werden festgelegte Hypothesen, nicht alles gegen alles.
2. **Eigentrend entfernen:** Gerechnet wird mit der Abweichung vom 28-Tage-Median, weil zwei steigende Reihen sonst immer korrelieren.
3. **Effekt messen:** Median-Differenz für Ja-Nein-Fragen, Spearman für Mengen, jeweils mit 0 bis 2 Tagen Versatz.
4. **Fair testen:** Die Testreihe wird zeitlich im Kreis verschoben, das erhält die Autokorrelation. Normale Tests [überschätzen](https://pdf.arxiv.org/pdf/1904.01622) sonst die Sicherheit.
5. **Mehrfachtests korrigieren:** Benjamini-Hochberg mit q < 0,10 über alle Hypothesen, in `scipy` als `false_discovery_control`.
6. **Stabilität prüfen:** Mindestens 15 Tage je Gruppe und gleiche Richtung in der älteren und der neueren Hälfte.

| Frage | Auslöser | Zielgröße | Zeitpunkt |
| --- | --- | --- | --- |
| Späte Einheiten | Ende nach 20 Uhr | Schlafdauer, HRV, Ruhepuls | Nacht danach |
| Trainingsbelastung | Minuten mal Training Effect | HRV, Ruhepuls | 1 bis 2 Tage danach |
| Kurze Nächte | Schlaf unter 7 h | Ruhepuls, Stress, Body Battery | Folgetag |
| Schlafrhythmus | Streuung der Einschlafzeit | HRV im 7-Tage-Schnitt | Gleiche Woche |
| Stress | Tagesstress | Schlaf-Score | Gleiche Nacht |
| Laufumfang | Kilometer pro Woche | Ruhepuls, HRV | Folgewoche |
| Wochenende | Samstag und Sonntag | Abweichung vom Gewichtstrend | Montag und Dienstag |

**Zwei Fallen, am Beispiel deiner Daten:**

- **Wochentag gleich Trainingsart:** Beine liegen immer auf Donnerstag, ein Bein-Effekt lässt sich also nicht vom Wochentag trennen. Deshalb nutzt der Katalog die Belastung statt der Trainingsart, und Python überspringt solche Tests für jede Instanz automatisch.
- **Kurze Trainingshistorie:** Garmin-Daten reichen bis Februar 2025 zurück, regelmäßiges Training nur bis Ende August 2026. Fragen zu Schlaf und Stress haben rund 21 Monate Daten, Trainingsfragen rund 10 Wochen.

**Was die KI damit macht:**

- **Täglich:** Höchstens ein Hinweis im Bericht, und nur, wenn er neu ist oder sich geändert hat.
- **Sonntags:** Ein Wochenbericht mit bis zu 3 Trends, 2 Zusammenhängen und einem Experiment.
- **Warnungen:** Sofort, wenn HRV oder Ruhepuls 3 Tage außerhalb liegen, höchstens alle 3 Tage.
- **Neue Hypothesen:** Sonntags schlägt Haiku bis zu 3 Hypothesen als JSON vor, nur aus einer erlaubten Feldliste. Python testet sie danach wie alle anderen.
- **Sprache:** Die KI schreibt „geht einher mit“ statt „verursacht“, denn Beobachtungsdaten [zeigen keine Ursache](https://arxiv.org/pdf/2208.00739v4). Dafür schlägt sie ein Experiment vor, zum Beispiel zwei Wochen früher trainieren.

## KI-Bericht

Python rechnet alle Zahlen, das Modell formuliert nur. So kann der Bericht keine Werte erfinden, und du kannst jede Aussage in der Datenbank nachprüfen.

**Ablauf jeden Morgen:**

1. Ab der konfigurierten Berichtszeit, standardmäßig 09:00, prüft der Job alle 15 Minuten, ob die Schlafdaten da sind. Sie kommen erst an, wenn das Gerät mit dem Handy synchronisiert hat.
2. Python baut ein JSON aus Nachtwerten, 7- und 28-Tage-Schnitt, Abweichungen, letzten Einheiten, Gewichtstrend, geprüften Hinweisen und Stand zum Ziel.
3. Dazu kommt die Konfiguration der Instanz. Bei dir sind das Di Push, Mi Pull, Do Legs, ein Tag am Wochenende und der Hyrox.
4. Haiku 5.5 schreibt daraus über die Claude API den Bericht nach fester Vorlage.
5. Eine einfache Prüfung gleicht die Messwerte im Text mit dem JSON ab. Bei Abweichung oder API-Fehler geht nur die Zahlenübersicht raus.
6. Der Bot sendet den Bericht, und `reports` speichert Eingabe und Text.

Spätestens 2,5 Stunden nach der Berichtszeit geht der Bericht raus, notfalls ohne Schlafdaten und mit einem Hinweis darauf.

**Vorlage, höchstens 150 Wörter:**

- **Lage:** ein Satz zur Erholung gegenüber der eigenen Baseline
- **Heute:** was das für die geplante Einheit bedeutet
- **Auffällig:** höchstens ein neuer Trend oder Zusammenhang
- **Ziel:** Stand zum Ziel aus der Konfiguration, bei dir Tage bis zum Hyrox und Laufumfang

Der Prompt verbietet Diagnosen, nennt Gewicht nur als Wochentrend und verlangt, fehlende Werte offen zu benennen.

**Aufruf:** Der Job nutzt das offizielle Python-SDK `anthropic`, das den Key aus `ANTHROPIC_API_KEY` liest.

```python
client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-haiku-5-5",
    max_tokens=600,
    system=PROMPT,
    messages=[{"role": "user", "content": payload_json}],
)
```

- **Konto:** Den Key erzeugst du in der [Claude Console](https://platform.claude.com/). Die API wird dort getrennt vom Pro-Plan über Guthaben abgerechnet.
- **Eigenes Modul:** `report_llm.py` kapselt den Aufruf, damit du das Modell mit einer Zeile ändern kannst.

**Kosten:** Der tägliche Bericht kostet mit Haiku 5.5 bei angenommenen 3.000 Input- und 800 Output-Token rund 0,02 USD im Monat. Mit Wochenbericht und Hypothesen, je etwa 8.000 und 4.000 Input-Token, sind es rund 0,03 USD pro Instanz. Mit Sonnet 5.5 läge alles bei etwa 0,60 USD, falls dir die Texte zu schlicht sind ([Preise](https://platform.claude.com/docs/en/about-claude/pricing)).

**Datenschutz:** Zwei Stellen verlassen den Pi.

- **Claude API:** Sie bekommt nur Kennzahlen ohne Namen. Anthropic trainiert laut den [Commercial Terms](https://www.anthropic.com/legal/commercial-terms) nicht auf API-Inhalten.
- **Telegram:** Bot-Chats sind nicht Ende-zu-Ende-verschlüsselt, die Werte von euch beiden liegen also auch auf Telegram-Servern.

**Daten deines Kumpels:** Sie liegen auf deinem Pi. Sag ihm, was gespeichert wird und wohin Daten gehen, und lösche seine Instanz, wenn er aussteigt. Rein persönliche Vorhaben fallen laut [Erwägungsgrund 18](https://gdpr-info.eu/recitals/no-18/) nicht unter die DSGVO; ob das für einen Freund noch gilt, ist nicht eindeutig. Das ist keine Rechtsberatung.

## Betrieb auf dem Pi

Das System soll ohne dich laufen: Es startet nach einem Stromausfall neu und meldet Probleme selbst per Telegram.

| Thema | Umsetzung |
| --- | --- |
| Dienst | Ein Compose-Projekt je Person aus demselben Image, jeweils mit `restart: unless-stopped`, Zeitzone `Europe/Berlin` |
| Zeitplan | Sync um 08:45, 13:00 und 21:00, die zweite Instanz 10 Minuten versetzt, Bericht zur konfigurierten Zeit, Wochenbericht sonntags um 18:00 |
| Speicher | `data/<person>/` mit SQLite, `withings_token.json`, Garmin-Token-Cache und `config.yaml`, am besten auf SSD statt SD-Karte |
| Secrets | `.env.<person>` mit Rechten 600 und nicht im Repo: nur feste Werte wie Garmin-Key und dessen Ablaufdatum, Withings-Client-ID und -Secret, Bot-Token und Chat-ID, dazu in beiden derselbe Claude-API-Key |
| Tokens | Neuen Withings-Refresh-Token sofort speichern, bevor irgendetwas anderes passiert |
| Backups | Täglich `sqlite3 .backup` und `rsync` über Tailscale auf deinen Homelab-Server, Restore einmal testen |
| Deploy | `git pull` und `docker compose up -d --build`, ein Skript startet danach beide Instanzen neu |
| Warnungen | Bei Sync-Fehlern, wenn der letzte Erfolg über 26 h zurückliegt, und ab 14 Tage vor Ablauf des Garmin-Keys laut `GARMIN_KEY_EXPIRES`, jeweils an die betroffene Person |
| Pi-Ausfall | Der Pi kann seinen eigenen Ausfall nicht melden: Bleibt dein eigener Bericht aus, ist das dein Signal |
| Löschen | Instanz stoppen und ihr Datenverzeichnis löschen, Backups laufen nach 30 Tagen aus |

## Reihenfolge

Ohne Termine: Jede Phase endet, wenn ihr Kriterium erfüllt ist.

| Phase | Aufgaben | Abnahmekriterium |
| --- | --- | --- |
| 1 Daten | Pi aufsetzen, Repo, Provider-Schicht, Garmin- und Withings-Provider, SQLite, Backfill | 7 Tage in Folge vollständige Tageswerte in deiner Instanz, Withings-Werte in der deines Kumpels |
| 2 Telegram-Dashboard | Bot, Allowlist, `/heute`, `/woche`, `/trend`, `/status`, /help, Diagramme | Jeder Befehl antwortet auf dem iPhone in unter 10 Sekunden |
| 3 KI-Bericht | API-Key, JSON, Prompt, Trends, Prüfung, Fallback, Versand | 5 Morgen in Folge korrekter Bericht, alle Zahlen stimmen mit der Datenbank überein |
| 4 Härtung | Zweite Instanz, Backups, Restore-Test, Warnungen, Neustart-Test | Pi vom Strom genommen, danach läuft alles ohne Eingriff weiter |
| 5 Zusammenhänge | Katalog, Tests, Filter, Wochenbericht, Hypothesen der KI, /warum | Ein künstlich eingebauter Effekt wird gefunden, eine Zufallsreihe nicht |

## Risiken

Das größte Risiko ist die Personal API für deinen Kumpel, denn Garmin hat sie für die eigenen Daten einer Person gebaut. Das nimmst du für den privaten Rahmen in Kauf.

| Risiko | Folge | Plan |
| --- | --- | --- |
| Personal API ist neu, seit Oktober 2026 | Felder oder Limits können sich ändern | Rohdaten speichern, Client kapseln, bei HTTP 429 warten |
| Withings lehnt den Callback ab | OAuth scheitert beim ersten Login | Adresse nach dem Login innerhalb von 30 Sekunden ins Setup-Skript kopieren |
| Withings-Refresh-Token geht verloren | Sync fällt aus, neuer Login nötig | Sofort speichern, Login-Skript im Repo |
| Withings-Messung wird nachträglich gelöscht | Ob `lastupdate` das meldet, ist unklar, der Wert bleibt dann in der Datenbank | Beim ersten echten Fall prüfen, sonst die Zeile `getmeas/<grpid>` von Hand löschen und den Tag neu berechnen |
| Garmin-Key wird erneuert, das Datum nicht | Warnung kommt zur falschen Zeit | `GARMIN_KEY_EXPIRES` mit dem Key zusammen ändern, Garmin mailt zusätzlich etwa eine Woche vorher |
| Uhr synchronisiert morgens nicht | Bericht kommt spät oder ohne Schlaf | Wiederholung alle 15 Minuten, Fallback spätestens um 11:30 |
| Felder fehlen je nach Gerät | Lücken im Dashboard | Endpunkte je Gerät testen, fehlende Kacheln ausblenden |
| SD-Karte oder Pi fallen aus | Verlust der lokalen Daten | Tägliches Backup, Rohdaten teils neu abrufbar |
| API-Guthaben ist aufgebraucht | Kein KI-Text an diesem Tag | Zahlenübersicht als Fallback, /status zeigt den Grund |
| Garmin schränkt fremde Keys ein | Sync für deinen Kumpel fällt aus | Provider-Schicht, später Wechsel auf das Starter-Programm |

Zu Garmin: Die [FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/) beschreibt die Personal API für die eigenen Daten einzelner Nutzer und nicht für kommerzielle Anwendungen mit mehreren Endnutzern. Laut [ownmetrics](https://pypi.org/project/ownmetrics-mcp/) darf der Key nicht weitergegeben werden. Ein Freund, der seinen Key auf deinem Pi einträgt, bewegt sich also mindestens am Rand davon.

Zu Withings: Der [kostenlose Plan](https://static.withings.com/content/api/plans/plans.html) gilt für 10 aktive Nutzer. Konten, die nach dem 12. Oktober 2026 entstanden sind, brauchen für die Freigabe an solche Apps Withings+. Das kostete laut [Withings-FAQ](https://support.withings.com/hc/en-us/articles/8986672043153-Withings-FAQ) zuletzt 99,95 EUR im Jahr, der Preisstand ist allerdings von 2023. Das Konto deines Kumpels stammt aus der Zeit vor 2026, für ihn bleibt der Zugang also kostenlos.

Zum Withings-Login: Laut [benos-auth](https://github.com/bgalbs/benos-auth) verlangt Withings eine HTTPS-Adresse ohne `localhost`, Withings' eigenes [OAuth-Beispiel](https://github.com/withings-sas/api-oauth2-python) nutzt dagegen in der Umgebung `dev` eine `localhost`-Adresse. Das Setup-Skript nutzt standardmäßig `http://localhost:8765/callback`. Klappt das nicht, registrierst du eine leere Seite, etwa auf GitHub Pages, startest das Skript mit `--redirect-uri` und kopierst die Adresse nach dem Login hinein. Der Code darin gilt nur 30 Sekunden.

## Quellen

- [Garmin: Program Overview](https://developer.garmin.com/gc-developer-program/overview)
- [Garmin: Personal API FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)
- [Garmin: connect-personal-api-python](https://github.com/garmin/connect-personal-api-python)
- [ownmetrics-mcp](https://pypi.org/project/ownmetrics-mcp/): Grenzen der Personal API
- [Garmin-MCP](https://glama.ai/mcp/servers/filipelimao/Garmin-MCP): offizielle und inoffizielle Tools
- [gcpapi-sync](https://pypi.org/project/gcpapi-sync/)
- [Garmin Cirqa: Funktionen laut Händler](https://www.electrocity.ie/product/garmin-cirqa-screenless-smart-band-activity-tracker-l-xl-mauve/)
- [Withings: API-Referenz](https://developer.withings.com/llms.md)
- [Withings: API-Pläne](https://static.withings.com/content/api/plans/plans.html)
- [Withings: Withings+ FAQ](https://support.withings.com/hc/en-us/articles/8986672043153-Withings-FAQ)
- [Withings Community: Garmin-Anbindung](https://support.withings.com/hc/en-us/community/posts/360036420233-Garmin-Connect-partner-app-sync)
- [benos-auth](https://github.com/bgalbs/benos-auth): Anforderungen an den Callback
- [Withings: OAuth-Beispiel](https://github.com/withings-sas/api-oauth2-python): `localhost` als Callback
- [Withings: OpenAPI-Spec](https://developer.withings.com/openapi.yaml)
- [personal-health-data-mcp](https://glama.ai/mcp/servers/emilniclashofseth/personal-health-data-mcp/tree): `localhost` als Callback
- [Telegram: Bot API, getUpdates](https://core.telegram.org/bots/api#getupdates)
- [Telegram: Bot Features](https://core.telegram.org/bots/features)
- [Anthropic: Preise](https://platform.claude.com/docs/en/about-claude/pricing)
- [Anthropic: Commercial Terms](https://www.anthropic.com/legal/commercial-terms)
- [DSGVO: Erwägungsgrund 18](https://gdpr-info.eu/recitals/no-18/)
- [Python auf dem Raspberry Pi](https://aruljohn.com/info/python-raspberrypi)
- [Orsama et al. 2014: Weight Rhythms](https://karger.com/ofa/article/7/1/36/240088/Weight-Rhythms-Weight-Increases-during-Weekends)
- [The Hacker's Diet: geglätteter Gewichtstrend](https://fourmilab.ch/hackdiet/www/subsubsection1_2_4_0_4_4.html)
- [Frontiers 2025: Bioimpedanz gegen DXA](https://public-pages-files-2025.frontiersin.org/journals/sports-and-active-living/articles/10.3389/fspor.2025.1644082/text)
- [AIS: HRV Best Practice Guideline](https://www.ais.gov.au/__data/assets/pdf_file/0013/1130260/HRV-BPG_FINAL.pdf)
- [Tang und Landes: t-Tests mit serieller Korrelation](https://pdf.arxiv.org/pdf/1904.01622)
- [Daza und Schneider: kausale Effekte aus Wearable-Daten](https://arxiv.org/pdf/2208.00739v4)
- [openair: Theil-Sen-Trends](https://bookdown.org/david_carslaw/openair/sections/trend-analysis/theil-sen.html)

Die [Swagger-Doku](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/) von Garmin wirkt beim direkten Abruf leer, die vollständige Spec steckt aber im JS-Bundle der Seite. Feldnamen und Zeitfenster sind daraus geprüft, siehe „Prüfung gegen die APIs“.
