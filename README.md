# Trump Market Impact Monitor

Überwacht Aussagen von Donald Trump (Nachrichtenzitate, Truth-Social-Posts, optional
Live-Reden), lässt Claude einschätzen ob eine Aussage marktrelevant ist (positiv/
negativ/neutral, betroffene Ticker/Sektoren) und schickt bei Relevanz einen
Telegram-Alert. Ein Web-Dashboard zeigt den Live-Feed inkl. Status- und Statistik-Übersicht.

**Kein Trading-Signal / keine Finanzberatung.** Die Einschätzungen sind LLM-generiert
und können falsch liegen.

## Architektur

```
Quellen (News/Truth Social/Live-Audio) --poll()--> Orchestrator (Supervisor, Concurrency)
                                                        |
                                              Duplikat-Check (difflib)
                                                        |
                                              Claude-Klassifikation
                                              (marktrelevant? sentiment?
                                               ticker? sektoren?)
                                                        |
                                              +---------+---------+
                                              v                   v
                                        SQLite (Audit-Trail)  Telegram-Alert (HTML, Retry)
                                              |
                                              v
                                Web-Dashboard (FastAPI): Feed, Health, Stats, Test-Endpoint
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # fuer den Truth-Social-Browser-Fallback

cp .env.example .env
# .env ausfüllen: mindestens ANTHROPIC_API_KEY
```

### Telegram-Alerts einrichten (optional, aber empfohlen)

1. Bot erstellen: mit [@BotFather](https://t.me/BotFather) auf Telegram chatten, `/newbot`
2. `TELEGRAM_BOT_TOKEN` aus der Antwort in `.env` eintragen
3. Eigene Chat-ID herausfinden, z.B. via [@userinfobot](https://t.me/userinfobot)
4. `TELEGRAM_CHAT_ID` in `.env` eintragen

Beim Start schickt das System automatisch eine kurze Heartbeat-Nachricht ("Monitor
gestartet, aktive Quellen: ...") - das ist die einfachste Art zu prüfen, ob Telegram
korrekt verbunden ist.

### Starten

```bash
python main.py
```

Dashboard läuft dann auf `http://localhost:8000`.

### Sofort verifizieren, ob alles korrekt konfiguriert ist

Direkt nach dem Start:

1. **Claude-Selftest**: Beim Boot wird automatisch ein Test-Statement klassifiziert.
   Schlägt das fehl (z.B. falscher API-Key), wird das im Log als `CRITICAL` gemeldet,
   der Monitoring-Loop startet **nicht**, das Dashboard bleibt aber erreichbar und
   zeigt den Fehler unter `/api/health` bzw. als roten Banner im Dashboard an.
2. **Test-Panel im Dashboard**: Auf der Startseite gibt es ein Feld "Pipeline testen" -
   eigenen Beispieltext eingeben, auf "Durch Claude jagen" klicken, Ergebnis (Sentiment,
   Ticker, Begründung) erscheint sofort, optional inkl. echtem Telegram-Alert. Damit
   lässt sich die komplette Kette (Claude + Telegram) verifizieren, ohne auf eine echte
   Trump-Aussage zu warten. Programmatisch äquivalent:
   ```bash
   curl -X POST http://localhost:8000/api/test \
     -H "Content-Type: application/json" \
     -d '{"text": "Ich werde 25% Zölle auf alle importierten Autos erheben.", "send_telegram": true}'
   ```
3. **`/api/health`**: zeigt pro Quelle den Zeitpunkt des letzten Polls/Erfolgs/Fehlers,
   Loop-Neustarts und etwaige Konfigurationsfehler/-warnungen.

## Datenquellen

| Quelle | Datei | Status | Latenz |
|---|---|---|---|
| Nachrichten (GDELT) | `app/sources/news_gdelt.py` | Stabil, kostenlos, kein Key nötig | ~15 Min |
| Nachrichten (RSS) | `app/sources/news_rss.py` | Stabil, kostenlos | Minuten |
| Truth Social | `app/sources/truth_social.py` | Best-Effort: direkter API-Call, mit Browser-Fallback | Sekunden-Minuten |
| Live-Audio (Reden) | `app/sources/live_audio.py` | Experimentell, optionale Deps | ~30s Chunks |

Quellen einzeln an/aus schalten über `.env` (`ENABLE_NEWS`, `ENABLE_TRUTH_SOCIAL`,
`ENABLE_LIVE_AUDIO`).

### Truth Social im Detail

Zwei Ebenen, automatisch nacheinander versucht:

1. **Direkter API-Call** gegen die undokumentierte JSON-API der Web-App. Günstig
   und schnell, aber leicht durch Bot-Schutz blockierbar.
2. **Browser-Fallback** (Playwright/Chromium): lädt die öffentliche Profilseite in
   einem echten headless Browser und liest die Netzwerk-Antworten mit, die die
   Seite selbst von ihrer eigenen API bekommt (gleiche JSON-Struktur wie Pfad 1,
   nur mit echtem Browser-Fingerprint/Cookies/Headern - robuster gegen Bot-Blocking,
   aber CPU/RAM-intensiv). Wird deshalb nur alle `TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL`
   Sekunden (Standard 300s) versucht, nicht bei jedem Poll-Zyklus.

Falls beide Wege fehlschlagen, liefert die Quelle 0 Ergebnisse statt abzustürzen -
das wird in `/api/health` sichtbar (`last_error` bei `truth_social`). Optional kann
ein eigenes Bearer-Token (`TRUTH_SOCIAL_BEARER_TOKEN`, z.B. aus einer eigenen
eingeloggten Session) gesetzt werden, falls verfügbar.

**Hinweis:** Diese Sandbox-Umgebung konnte den Live-Zugriff auf truthsocial.com beim
Testen nicht verifizieren (Netzwerk-Policy blockiert ausgehende Verbindungen zu
diesem Host). Beide Codepfade wurden mechanisch getestet (Chromium startet,
navigiert, Fehlerbehandlung greift), aber die tatsächliche Erfolgsrate auf einem
Server mit echtem Internetzugang lässt sich erst nach dem Deployment verifizieren.

### Live-Audio im Detail

Braucht zusätzlich `pip install yt-dlp faster-whisper` sowie `ffmpeg` im System.
Es gibt **keine automatische Erkennung**, wann Trump live spricht — Stream-URLs
müssen manuell in `LIVE_AUDIO_STREAM_URLS` gepflegt werden (z.B. Link zu einem
angekündigten Event oder einem 24/7-Nachrichtensender). Transkription läuft in
Chunks (Standard 30s), keine Wort-für-Wort-Live-Transkription.

### Sonstige Einschränkungen

- **GDELT liefert Artikel-Titel**, nicht das wörtliche Zitat — gut als Signal,
  aber kein Ersatz für O-Töne.
- Kostenlose Aktienkurse sind i.d.R. 15 Min. verzögert; für echte Realtime-Kurse
  braucht es einen kostenpflichtigen Anbieter (nicht Teil dieses Projekts, aber
  leicht als weiterer Alert-Datenpunkt ergänzbar).

## Robustheit / Reife dieser Version

- **Startup-Validierung**: fehlender `ANTHROPIC_API_KEY`, alle Quellen deaktiviert
  etc. werden beim Boot erkannt (`app/config.py: validate()`), zusammen mit einem
  echten Claude-Selftest-Call. Fehler stoppen den Monitoring-Loop kontrolliert
  (nicht den ganzen Prozess) und sind über `/api/health` sichtbar.
- **Cross-Source-Duplikaterkennung**: dieselbe reale Aussage, die z.B. sowohl bei
  GDELT als auch per RSS auftaucht, wird per Textähnlichkeit (`difflib`) erkannt
  und nur einmal klassifiziert/alarmiert (siehe `DEDUP_SIMILARITY_THRESHOLD`).
- **Nebenläufige Klassifikation**: mehrere neue Statements pro Poll-Zyklus werden
  parallel klassifiziert (begrenzt durch `MAX_CONCURRENT_CLASSIFICATIONS`), statt
  nacheinander.
- **Telegram-Fix**: die ursprüngliche Markdown-Formatierung konnte durch
  Sonderzeichen im Statement-Text (`_`, `*`, `` ` ``) brechen und Alerts
  stillschweigend verschlucken. Jetzt HTML-Modus mit korrektem Escaping, plus
  Retry bei Rate-Limits (HTTP 429).
- **Supervisor**: der Poll-Loop läuft in einer äußeren Schleife, die ihn bei einem
  unerwarteten Absturz automatisch mit exponentiellem Backoff neu startet
  (`run_health.loop_restarts` in `/api/health`).
- **Health- und Stats-Endpoints**: `/api/health` (Quellen-Status, Fehler, Uptime,
  Loop-Neustarts) und `/api/stats` (Gesamtzahlen, Sentiment-Verteilung, Duplikate,
  gesendete Alerts), beide auch im Dashboard sichtbar.
- **SQLite im WAL-Modus**: gleichzeitige Schreibzugriffe (Orchestrator) und
  Lesezugriffe (Dashboard-API) blockieren sich nicht gegenseitig.

## Dauerbetrieb (24/7)

Dieses Projekt läuft als lang laufender Python-Prozess (Polling-Loop + Webserver in
einem). Für echten Dauerbetrieb muss es auf einer permanenten Umgebung deployed
werden - vier fertige Optionen liegen bei:

### Option 0: Oracle Cloud Free Tier (kostenlos, empfohlen)

Oracle Cloud bietet einen "Always Free"-VPS, der dauerhaft (nicht nur als Trial)
kostenlos bleibt. So richtest du ihn ein:

1. **Account erstellen**: [oracle.com/cloud/free](https://www.oracle.com/cloud/free/)
   → "Start for free". Braucht E-Mail, Telefonnummer und eine Kreditkarte zur
   Identitätsprüfung - es wird nichts abgebucht, solange man im Free-Tier-Limit bleibt.
2. **VM erstellen**: Cloud Console → *Compute* → *Instances* → *Create Instance*
   - Image: **Ubuntu** (Standard-Vorschlag meist schon passend)
   - Shape: **VM.Standard.A1.Flex** (ARM, bis 4 OCPU/24GB RAM gratis - komfortabler
     für Docker+Chromium) probieren; falls "Out of host capacity" kommt (bei Free-Tier
     manchmal je nach Region ausgebucht), alternativ **VM.Standard.E2.1.Micro** (x86,
     1 OCPU/1GB RAM, garantiert verfügbar, aber knapper bemessen)
   - SSH-Key: eigenen Public Key hochladen (oder von Oracle generieren lassen und
     herunterladen)
   - *Create* klicken, IP-Adresse der Instanz notieren
3. **Port 8000 auf Netzwerk-Ebene freigeben**: Instanz-Detailseite → Subnetz-Link →
   *Security Lists* → Default Security List → *Add Ingress Rules*:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: TCP
   - Destination Port Range: `8000`
4. **Einloggen und Bootstrap-Skript laufen lassen**:
   ```bash
   ssh ubuntu@<Server-IP>
   curl -fsSL https://raw.githubusercontent.com/CorentinWWW/Claudeprojekt1/claude/trump-market-impact-analyzer-dbjvu5/deploy/oracle_bootstrap.sh | bash
   ```
   Das Skript (`deploy/oracle_bootstrap.sh`) installiert Docker, öffnet Port 8000 in
   der VM-eigenen Firewall (iptables/ufw - zusätzlich zur Security List aus Schritt 3),
   klont dieses Repo und legt `.env` aus der Vorlage an.
5. **Konfigurieren und starten**:
   ```bash
   nano ~/trump-market-monitor/.env   # ANTHROPIC_API_KEY (+ optional Telegram) eintragen
   cd ~/trump-market-monitor && sudo docker compose up -d --build
   ```
6. Dashboard unter `http://<Server-IP>:8000` aufrufen, im Test-Panel einen Beispieltext
   durchjagen um zu prüfen, dass alles korrekt konfiguriert ist.

Für Updates später: `cd ~/trump-market-monitor && git pull && sudo docker compose up -d --build`.

### Option A: Docker (auf einem beliebigen Server)

```bash
cp .env.example .env   # ausfuellen
docker compose up -d --build
```

Das Image installiert Chromium samt System-Abhängigkeiten automatisch
(`playwright install --with-deps chromium` im `Dockerfile`). Die SQLite-Datenbank
liegt in einem benannten Volume (`trump-monitor-data`), übersteht also Neustarts/Updates.

> Hinweis: Der Docker-Build selbst konnte in dieser Sandbox nicht getestet werden
> (kein laufender Docker-Daemon verfügbar), da hier bewusst keine Container-in-Container-
> Mechanismen genutzt werden. Das `Dockerfile` folgt aber dem offiziell dokumentierten
> Standardmuster für Playwright-in-Docker-Setups.

### Option B: Eigener VPS ohne Docker (systemd)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install --with-deps chromium
```

Dann `deploy/trump-monitor.service` nach `/etc/systemd/system/` kopieren, Pfade/User
anpassen, und:

```bash
sudo systemctl enable --now trump-monitor
```

### Option C: Railway / Fly.io / Render

Python-Buildpack, Start-Command `python main.py`, Build-Command muss zusätzlich
`playwright install --with-deps chromium` ausführen (z.B. als Post-Build-Hook oder
im `Dockerfile`, falls die Plattform Container-Deploys unterstützt).

## Konfiguration (.env)

Siehe `.env.example` für alle Variablen. Wichtige zusätzliche Stellschrauben:

| Variable | Bedeutung |
|---|---|
| `ALERT_CONFIDENCE_THRESHOLD` | Ab welcher Claude-Konfidenz (0-1) ein Telegram-Alert geschickt wird (Standard 0.5) |
| `MAX_CONCURRENT_CLASSIFICATIONS` | Wie viele Claude-Calls parallel laufen dürfen (Standard 3) |
| `DEDUP_SIMILARITY_THRESHOLD` | Ab welcher Textähnlichkeit (0-1) zwei Statements als Duplikat gelten (Standard 0.82) |
| `DEDUP_WINDOW_SECONDS` | Zeitfenster für die Duplikatsuche (Standard 6h) |
| `TELEGRAM_STARTUP_NOTICE` | Heartbeat-Nachricht beim Start senden (Standard an) |
| `TRUTH_SOCIAL_BROWSER_FALLBACK` | Playwright-Fallback für Truth Social an/aus (Standard an) |
| `CLAUDE_MAX_RETRIES` / `CLAUDE_TIMEOUT_SECONDS` | Robustheit der Claude-API-Calls |

## Datenbank

SQLite-Datei (`trump_monitor.db`, Pfad über `DB_PATH` konfigurierbar) mit Tabelle
`statements`: Text, Quelle, Zeitstempel, Klassifikation, `duplicate_of_id` (falls
als Duplikat erkannt), ob ein Alert verschickt wurde. Dient als Audit-Trail und
später für Backtesting (z.B. Kursverlauf nach Statement gegen die eigene
Einschätzung abgleichen).

## API-Endpunkte

| Endpunkt | Zweck |
|---|---|
| `GET /` | Dashboard |
| `GET /api/statements?limit=&only_relevant=` | Feed als JSON |
| `GET /api/stats` | Aggregierte Statistik |
| `GET /api/health` | Status pro Quelle, Konfigurationsfehler/-warnungen, Uptime |
| `POST /api/test` | Beliebigen Text durch die volle Pipeline schicken (siehe oben) |
