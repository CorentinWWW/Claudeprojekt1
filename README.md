# Trump Market Impact Monitor

Überwacht Aussagen von Donald Trump (Nachrichtenzitate, Truth-Social-Posts, optional
Live-Reden), lässt Claude einschätzen ob eine Aussage marktrelevant ist (positiv/
negativ/neutral, betroffene Ticker/Sektoren) und schickt bei Relevanz einen
Telegram-Alert. Ein Web-Dashboard zeigt den Live-Feed.

**Kein Trading-Signal / keine Finanzberatung.** Die Einschätzungen sind LLM-generiert
und können falsch liegen.

## Architektur

```
Quellen (News/Truth Social/Live-Audio) --poll()--> Orchestrator
                                                        |
                                                        v
                                              Claude-Klassifikation
                                              (marktrelevant? sentiment?
                                               ticker? sektoren?)
                                                        |
                                              +---------+---------+
                                              v                   v
                                        SQLite (Verlauf)    Telegram-Alert
                                              |
                                              v
                                        Web-Dashboard (FastAPI)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env ausfüllen: mindestens ANTHROPIC_API_KEY
```

### Telegram-Alerts einrichten (optional, aber empfohlen)

1. Bot erstellen: mit [@BotFather](https://t.me/BotFather) auf Telegram chatten, `/newbot`
2. `TELEGRAM_BOT_TOKEN` aus der Antwort in `.env` eintragen
3. Eigene Chat-ID herausfinden, z.B. via [@userinfobot](https://t.me/userinfobot)
4. `TELEGRAM_CHAT_ID` in `.env` eintragen

### Starten

```bash
python main.py
```

Dashboard läuft dann auf `http://localhost:8000`.

## Datenquellen

| Quelle | Datei | Status | Latenz |
|---|---|---|---|
| Nachrichten (GDELT) | `app/sources/news_gdelt.py` | Stabil, kostenlos, kein Key nötig | ~15 Min |
| Nachrichten (RSS) | `app/sources/news_rss.py` | Stabil, kostenlos | Minuten |
| Truth Social | `app/sources/truth_social.py` | **Best-Effort** – inoffizielle API, kann brechen | Sekunden-Minuten |
| Live-Audio (Reden) | `app/sources/live_audio.py` | **Experimentell**, optionale Deps | ~30s Chunks |

Quellen einzeln an/aus schalten über `.env` (`ENABLE_NEWS`, `ENABLE_TRUTH_SOCIAL`,
`ENABLE_LIVE_AUDIO`).

### Wichtige Einschränkungen

- **Truth Social** hat keine offizielle Public-API. `truth_social.py` nutzt die
  undokumentierte API der Web-App. Das kann jederzeit aufhören zu funktionieren
  (Rate-Limit, Auth-Pflicht, Endpoint-Änderung). Der Code fängt das ab und liefert
  dann einfach 0 Ergebnisse statt abzustürzen. Falls nötig, kann ein eigenes
  Bearer-Token über `TRUTH_SOCIAL_BEARER_TOKEN` gesetzt werden.
- **Live-Audio** braucht zusätzlich `pip install yt-dlp faster-whisper` sowie
  `ffmpeg` im System. Es gibt **keine automatische Erkennung**, wann Trump live
  spricht — Stream-URLs müssen manuell in `LIVE_AUDIO_STREAM_URLS` gepflegt werden
  (z.B. Link zu einem angekündigten Event oder einem 24/7-Nachrichtensender).
  Transkription läuft in Chunks (Standard 30s), keine echte Wort-für-Wort-Live-Transkription.
- **GDELT liefert Artikel-Titel**, nicht das wörtliche Zitat — gut als Signal,
  aber kein Ersatz für O-Töne.
- Kostenlose Aktienkurse sind i.d.R. 15 Min. verzögert; für echte Realtime-Kurse
  braucht es einen kostenpflichtigen Anbieter (nicht Teil dieses Projekts, aber
  leicht als weiterer Alert-Datenpunkt ergänzbar).

## Dauerbetrieb (24/7)

Dieses Projekt läuft als lang laufender Python-Prozess (Polling-Loop + Webserver in
einem). Für echten Dauerbetrieb muss es auf einer permanenten Umgebung deployed
werden, z.B.:

- Ein eigener VPS (`systemd`-Service oder `tmux`/`screen` + `python main.py`)
- Railway / Fly.io / Render (Python-Buildpack, Start-Command `python main.py`)

## Konfiguration (.env)

Siehe `.env.example` für alle Variablen: API-Keys, Poll-Intervall,
Quellen an/aus, Alert-Ziel, Live-Audio-Parameter.

## Datenbank

SQLite-Datei (`trump_monitor.db`, Pfad über `DB_PATH` konfigurierbar) mit Tabelle
`statements`: Text, Quelle, Zeitstempel, Klassifikation, ob Alert verschickt wurde.
Dient als Audit-Trail und später für Backtesting (z.B. Kursverlauf nach Statement
gegen die eigene Einschätzung abgleichen).
