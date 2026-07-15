# Trump Market Impact Monitor

Überwacht Aussagen von Donald Trump (Nachrichtenzitate, Truth-Social-Posts, optional
Live-Reden), lässt Claude einschätzen ob eine Aussage marktrelevant ist (positiv/
negativ/neutral, betroffene Ticker mit Long/Short-Einschätzung) und schickt bei
Relevanz einen Telegram-Alert - aber nur einmal pro Thema pro Tag, außer die Lage
eskaliert wirklich. Ein Web-Dashboard zeigt den Live-Feed inkl. Status- und
Statistik-Übersicht.

**Kein Trading-Signal / keine Finanzberatung.** Die Einschätzungen (inkl. Long/Short)
sind LLM-generiert und können falsch liegen - eigene Anlageentscheidung auf eigenes Risiko.

## Architektur

```
Quellen (News/Truth Social/Live-Audio) --poll()--> Orchestrator (Supervisor, Concurrency)
                                                        |
                                    Tier 1: Text-Duplikat-Check (difflib, 24h,
                                            auch INNERHALB einer Charge)
                                                        |
                                    Claude-Klassifikation: marktrelevant? sentiment?
                                    Ticker + Long/Short je Ticker? Tier 2: dasselbe
                                    Thema wie eine heute schon alarmierte Meldung -
                                    und falls ja, wesentliche Eskalation?
                                                        |
                                    +----------+--------+--------+-----------+
                                    v          v                 v           v
                              SQLite       Alerts gebuendelt   Pending-Alert-Retry
                            (Audit-Trail)  (Einzeln/Digest)    (holt nie versendete
                                    |       via Telegram        Alerts beim naechsten
                                    v                           Zyklus nach)
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
- **Zweistufige Duplikaterkennung**:
  - *Tier 1 (Text, `difflib`)*: dieselbe reale Aussage, die z.B. sowohl bei GDELT
    als auch per RSS auftaucht oder von vielen Portalen wortgleich syndiziert wird,
    wird per Textähnlichkeit erkannt und nur einmal klassifiziert (siehe
    `DEDUP_SIMILARITY_THRESHOLD`/`DEDUP_WINDOW_SECONDS`, Standard 24h). Prüft auch
    INNERHALB einer Charge gegeneinander, nicht nur gegen die DB - sonst würde ein
    einzelner Nachrichtenschub mit vielen syndizierten Kopien jede davon einzeln
    alarmieren.
  - *Tier 2 (Thema, via Claude)*: bereits heute alarmierte Meldungen werden Claude
    als Kontext mitgegeben (`TOPIC_CONTEXT_WINDOW_HOURS`/`TOPIC_CONTEXT_MAX_ITEMS`).
    Erkennt Claude, dass eine neue Meldung *im Kern* zu einem dieser Themen gehört
    (auch bei ganz anderem Wortlaut), wird sie nur dann erneut alarmiert, wenn sie
    eine **wesentliche Eskalation** darstellt (z.B. von Androhung zu tatsächlicher
    Umsetzung) - reine Wiederholungen/Umformulierungen bleiben stumm.
  - Beide Tiers vergleichen nur auf Zeichen-/Sprachebene innerhalb derselben
    Sprache. GDELT deckt Nachrichten global in vielen Sprachen ab und würde
    dieselbe Aussage sonst über zig fremdsprachige Übersetzungen/Umformulierungen
    immer wieder als "neues" Statement liefern - die GDELT-Abfrage ist deshalb
    fest auf `sourcelang:english` eingeschränkt.
- **Präzisions-Filter: nur die sichersten Trade-Signale (`ALERT_MIN_TICKER_CONFIDENCE`,
  Standard 0.90)**: es wird **nur noch eine Telegram-Nachricht verschickt, wenn die
  Meldung mindestens eine konkrete Aktie mit klarer Long/Short-Richtung und sehr hoher
  Pro-Ticker-Konfidenz** enthält. Reine „marktrelevante" Meldungen ohne konkrete,
  hochsichere Aktie lösen bewusst **keinen** Alert mehr aus – das Ziel ist maximale
  Präzision statt Vollständigkeit (nur die klarsten „diese Aktie geht hoch/runter"-
  Signale). Die Schwelle ist ein einziger Regler: `0.90` = sehr streng (Standard, nur
  die absolut sichersten Trades), `0.85` = streng, `0.80` = moderat, `0` = Filter aus
  (altes Verhalten: jede marktrelevante Meldung oberhalb `ALERT_CONFIDENCE_THRESHOLD`).
  Im GitHub-Actions-Betrieb ohne Code-Änderung anpassbar über eine Repository-Variable
  `ALERT_MIN_TICKER_CONFIDENCE` (Settings → Secrets and variables → Actions → Variables).
  Die Entscheidung ist an **einer** Stelle zentralisiert (`orchestrator.py`:
  `is_alert_worthy`) und wird an **allen** Alarm-Pfaden angewandt – Primärpfad,
  manueller Test *und* der Wiederhol-Pfad für nicht zugestellte Alerts
  (`_resend_pending_alerts`). Letzteres schließt eine Lücke, durch die eine Meldung, die
  die Schwelle verfehlt, einen Zyklus später sonst doch noch ungefiltert alarmiert
  worden wäre. Hinweis: der Filter senkt bewusst die Trefferzahl – er ersetzt keine
  eigene Recherche und ist keine Anlageberatung.
- **Long/Short-Einschätzung pro Ticker, jeweils mit eigener Konfidenz**: statt nur
  "betroffene Ticker" gibt Claude für jeden genannten Ticker eine `long`/`short`-
  Einschätzung UND eine eigene Konfidenz dafür ab - auch innerhalb derselben Meldung
  können unterschiedliche Ticker unterschiedlich betroffen und unterschiedlich sicher
  eingeschätzt sein (z.B. Zölle die Stahlproduzenten sicher nützen, aber Autobauern nur
  mit geringerer Sicherheit schaden). Der Telegram-Alert zeigt die vollständige
  Überschrift (nicht abgekürzt) sowie alle Ticker als eigene Zeile mit Long/Short-Wort
  und Prozent-Konfidenz, sortiert nach Konfidenz absteigend - die sicherste
  Einschätzung steht ganz oben. Die Überschrift ist mit dem Quellartikel verlinkt
  (antippen öffnet den Original-Artikel; nur http/https, Link-Vorschau deaktiviert,
  damit die Nachricht kompakt bleibt).
- **Robuste Ticker-Erkennung**: jeder von Claude gelieferte Ticker wird vor der Anzeige
  normalisiert und validiert (`classifier.py`: `_normalize_ticker`/`_clean_ticker_calls`),
  damit kein Müll beim Nutzer landet. Das entfernt Börsen-/Markt-Präfixe
  (`NASDAQ:NVDA` → `NVDA`, `NYSE: XOM` → `XOM`) und Cashtag-`$` (`$AAPL` → `AAPL`),
  vereinheitlicht auf Großbuchstaben und akzeptiert nur echte Kürzel-Formate (1-5
  Buchstaben, optional ein Klassen-Suffix wie `BRK.B`). Ganze Firmennamen (`Nvidia`),
  Sätze und Platzhalter (`TBD`, `N/A`, `NONE`, `XYZ` …) werden verworfen statt als
  vermeintliches Kürzel angezeigt; mehrfach genannte Kürzel werden zusammengeführt (bei
  Dubletten gewinnt der Eintrag mit der höchsten Ticker-Konfidenz). Zusätzlich schärft
  der System-Prompt das gewünschte Format ein (offizielles US-Börsenkürzel in
  Großbuchstaben, ohne Präfix; im Zweifel lieber nur den Sektor nennen als raten).
- **Klassifikation standardmäßig seriell** (`MAX_CONCURRENT_CLASSIFICATIONS=1`):
  zwei fast zeitgleich klassifizierte Statements zum selben Thema können sich
  gegenseitig nicht als Duplikat erkennen, weil der Themen-Kontext (Tier 2) erst
  NACH Abschluss einer Klassifikation wächst. Serielle Verarbeitung schließt dieses
  Risiko aus; höhere Werte sind möglich, erhöhen aber die Chance auf doppelte
  Alerts bei einem Nachrichtenschub.
- **Gebündelte Alerts statt Nachrichtenflut**: sind in einem Zyklus mehr als
  `ALERT_DIGEST_THRESHOLD` (Standard 3) Meldungen gleichzeitig alarmwürdig (z.B. bei
  einer echten Großlage mit vielen unterschiedlichen Artikeln), wird daraus EINE
  Sammel-Nachricht statt einer Flut von Einzelnachrichten.
- **Haiku statt Sonnet als Standard-Modell**: Klassifikation ist eine strukturierte,
  schema-gefuehrte Aufgabe (Tool-Use mit festem JSON-Schema) - dafuer reicht
  `claude-haiku-4-5` gut aus und kostet nur einen Bruchteil von Sonnet pro Call. Über
  `CLAUDE_MODEL` in `.env` z.B. auf `claude-sonnet-5` umstellbar, falls die
  Einschätzungsqualität wichtiger ist als die Kosten.
- **Auf Signalqualität geschärfter Prompt (kostenneutral)**: der Klassifikations-Prompt
  trennt klar zwischen konkreten, neuen, handlungsrelevanten Aussagen (bezifferte
  Zollrate, namentlich genannte Firma/Deal, konkrete Sanktion, Fed-Personalie) und
  vager Wirtschaftsrhetorik ohne neuen Informationsgehalt ("die Wirtschaft läuft
  großartig") - Letztere führt zu `is_market_relevant=false` oder niedriger Konfidenz.
  Hohe Konfidenz (>0.7) nur bei konkret+neu; bloße Wiederholung einer längst bekannten
  Position bekommt niedrigere Konfidenz. Das reduziert Fehlalarme, **ohne** zusätzliche
  Claude-Calls. Gegenfinanziert durch etwas kompaktere Themen-Kontext-Snippets
  (`CONTEXT_SNIPPET_MAX_CHARS`, 100 statt 150 Zeichen) - dieselbe Zahl sichtbarer
  Themen, nur knappere Beschreibungen, sodass die Kosten pro Call praktisch gleich
  bleiben.
- **Harter Kostendeckel (`MAX_CLASSIFICATIONS_PER_DAY`, Standard 100/Tag)**: jede
  neue, noch nicht bekannte Meldung kostet einen echten Claude-Call - auch wenn sie
  sich danach als Themen-Duplikat herausstellt (die Duplikaterkennung erspart den
  erneuten *Alert*, nicht den Call selbst, der ja gerade erst festgestellt hat, dass
  es ein Duplikat ist). Ohne Deckel kann ein einzelner Nachrichtenschub (z.B. eine
  große Breaking-News-Lage mit vielen Artikeln) an einem einzigen Tag überraschend
  hohe Kosten verursachen. Der Zähler ist in der SQLite-DB persistiert (`app/db.py`:
  `get_classification_calls_today`/`record_classification_call`), gilt also auch über
  einzelne GitHub-Actions-Läufe hinweg über den ganzen UTC-Tag. Ist das Limit erreicht,
  werden weitere Meldungen bis Mitternacht (UTC) einfach übersprungen (kurze Warnung
  im Log) statt einen weiteren Call auszulösen - der Claude-Selftest beim Start ist
  davon ausgenommen, damit ein ausgeschöpftes Tages-Limit nicht auch noch den
  Verbindungs-Check und damit den gesamten Monitoring-Start blockiert. Beim ERSTEN
  Zuschlagen des Limits an einem Tag kommt genau eine Telegram-Notiz - ein
  stummgeschalteter Monitor sähe sonst exakt so aus wie ein ruhiger Nachrichtentag.
- **Prioritäts-Reserve für wirklich wichtige Meldungen (`PRIORITY_CLASSIFICATIONS_PER_DAY`,
  Standard 30/Tag)**: ein reiner Hart-Deckel hätte das Problem, dass an einem lauten
  Nachrichtentag eine echt marktbewegende Meldung stumm untergeht, nur weil das normale
  Limit schon von unwichtigeren Meldungen aufgebraucht wurde. Deshalb gibt es ein
  **zweites Kontingent oberhalb** von `MAX_CLASSIFICATIONS_PER_DAY`, das ausschließlich
  als *wichtig* eingestufte Meldungen nutzen dürfen: direkte Trump-Posts von Truth Social
  (seine eigenen Worte) sowie harte Wirtschaftsthemen (Zölle, Sanktionen, Zinsen/Fed,
  Executive Orders, Shutdown, Embargo …). Die Wichtigkeit wird **billig und ohne
  zusätzlichen Claude-Call** aus Quelle + Signalwörtern bestimmt (`orchestrator.py`:
  `is_high_priority`) - sie muss ja *vor* dem Ausgeben eines Calls feststehen. Normale
  Meldungen bleiben hart bei `MAX` gesperrt, wichtige kommen bis zum absoluten
  Tages-Maximum `MAX + PRIORITY` (Standard 100 + 30 = **130**) noch durch. Technisch
  derselbe atomare Zähler wie beim Hart-Deckel, nur mit höherem Limit für Prioritäts-Calls
  (`db.py`: `reserve_classification_call_slot`). Innerhalb einer Charge werden wichtige
  Meldungen außerdem **zuerst** klassifiziert, damit das knappe Restbudget bevorzugt an
  sie geht. Auf `0` setzen macht `MAX` wieder zu einem harten Limit für *alle* Meldungen.
- **Kein grün-aber-tot**: permanente Claude-Konfigurationsfehler (401 = kaputter/
  widerrufener API-Key, 403 = fehlende Berechtigung, 404 = gelöschtes/falsches
  Modell) werden nicht mehr pro Statement geschluckt, sondern beenden den
  GitHub-Actions-Lauf mit Exitcode ≠ 0 - der Lauf wird rot und GitHub verschickt
  automatisch eine Fehler-Mail. Transiente Fehler (Timeouts, 429/529) bleiben
  weiterhin weich: das SDK retried sie, ein einzelner Ausfall bricht nichts ab.
- **Exakt gepinnte Dependencies**: der GitHub-Actions-Modus installiert die
  Python-Pakete bei jedem Lauf (48×/Tag) frisch - `requirements.txt` pinnt deshalb
  exakte, test-verifizierte Versionen, damit ein Breaking-Release eines
  Upstream-Pakets den Monitor nicht von einer Minute auf die andere töten kann.
- **Pending-Alert-Wiederholung**: Statements, die als marktrelevant eingestuft aber
  nie tatsächlich alarmiert wurden (z.B. weil ein Lauf mitten drin abgebrochen wurde
  oder Telegram kurzzeitig nicht erreichbar war), werden beim nächsten Zyklus
  automatisch erneut versucht - sonst würden sie für immer stumm bleiben.
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
- **GitHub-Actions-Concurrency-Guard**: verhindert, dass zwei überlappende Läufe
  (z.B. ein manueller Trigger während der geplante Lauf noch läuft) denselben
  DB-Cache-Stand gegeneinander überschreiben und sich dabei Alerts verlieren.
- **Robustere Quellen**: HTML-Entities in Truth-Social-Texten werden korrekt
  dekodiert, GDELT/RSS wiederholen bei transienten Verbindungsfehlern automatisch
  (nicht bei permanenten Blocks wie 403), ein einzelner kaputter RSS-Eintrag kostet
  nicht mehr den ganzen Feed, der Truth-Social-Browser-Fallback hat einen
  Gesamt-Timeout, Live-Audio-Temp-Dateien werden auch bei fehlgeschlagener
  Transkription zuverlässig aufgeräumt.
- **Weitere Härtung nach systematischem Bug-Hunt** (5 parallele Review-Durchläufe
  über den gesamten Code):
  - Batch-Queries statt Query-pro-Statement bei der Duplikatprüfung
    (`get_known_source_ids`/`get_dedup_candidates`) - relevant bei einem
    Nachrichtenschub mit vielen Statements in einem Zyklus.
  - Automatische Schema-Migration (`ALTER TABLE`) für DBs, die vor Einführung von
    `related_topic_id`/`is_major_escalation` angelegt wurden (z.B. aus einem alten
    GitHub-Actions-Cache).
  - `insert_statement` gibt bei einem `source_id`-Konflikt die tatsächliche
    existierende ID zurück statt einer irreführenden `0`.
  - Claude kann keine `related_topic_id` mehr "halluzinieren", die gar nicht im
    angebotenen Themen-Kontext war - wird erkannt und verworfen.
  - Bei `max_tokens`-Abschneidung der Claude-Antwort wird das Statement sauber als
    Fehler behandelt statt ein unvollständiges Ergebnis zu riskieren.
  - Truth-Social-Browser-Fallback ordnet abgefangene Posts nur dann zu, wenn sie
    wirklich vom konfigurierten Account (`TRUTH_SOCIAL_HANDLE`) stammen - verhindert
    Fehlzuordnung fremder Accounts.
  - Alle `_seen`-Caches der Quellen sind jetzt größenbegrenzt (FIFO-Verdrängung),
    damit ein wochenlanger Dauerbetrieb nicht unbegrenzt Speicher aufbaut.
  - Sammel-Nachrichten (Digest) werden zeilenweise statt zeichenweise gekürzt -
    verhindert, dass ein mitten in einem HTML-Tag abgeschnittener Text von Telegram
    komplett abgelehnt wird.
  - `/api/statements`, `/api/stats`, `/api/test` sind per `DASHBOARD_API_KEY`
    absicherbar (siehe Konfigurationstabelle unten).

## Dauerbetrieb (24/7)

Es gibt zwei grundsätzlich verschiedene Betriebsarten:

- **Einfachster Weg - GitHub Actions** (kein Account/Server/Kreditkarte nötig,
  nur Telegram-Alerts, kein Dashboard, Polling alle 30 Min statt 60s)
- **Voller Funktionsumfang** - Dashboard, 60s-Polling, Truth-Social-Browser-Fallback,
  Live-Audio - braucht einen (kostenlosen) Server (z.B. Oracle Cloud Free Tier)

### Einfachster Weg: GitHub Actions (empfohlen zum Ausprobieren)

Kein eigener Server, keine Kreditkarte, keine SSH/Firewall-Konfiguration - nur dieses
GitHub-Repo. Ein Workflow (`.github/workflows/monitor.yml`) führt alle 30 Minuten
automatisch einen Poll-Zyklus aus (`run_once.py`: alle Quellen abfragen, klassifizieren,
bei Relevanz Telegram-Alert schicken) und beendet sich wieder. Der Dedup-Status
zwischen Läufen wird über den GitHub-Actions-Cache mitgeschleppt.

1. Repo auf GitHub forken/nutzen (dieser Branch: `claude/trump-market-impact-analyzer-dbjvu5`)
2. **Settings → Secrets and variables → Actions → New repository secret** und dort anlegen:
   - `ANTHROPIC_API_KEY` (Pflicht)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional, aber empfohlen für Alerts)
3. Fertig - der Workflow läuft automatisch alle 30 Min. Zum sofortigen Testen:
   Tab **Actions** → *Trump Market Monitor* → **Run workflow** (manueller Trigger).
4. Ergebnis im Actions-Log einsehbar (welche Statements klassifiziert wurden), Alerts
   kommen per Telegram.

**Wichtig bei privaten Repos:** GitHub gibt kostenlosen Accounts ca. 2000 Actions-Minuten/
Monat (bei öffentlichen Repos unbegrenzt). Ein Lauf alle 30 Min bleibt im Rahmen; bei
Bedarf im Workflow auf `*/15` verkürzen, wenn das Repo öffentlich ist oder genug
Freiminuten übrig sind.

Kein Dashboard in diesem Modus - Telegram ist der Alert-Kanal, `Actions`-Tab das Log.
Live-Audio und der Truth-Social-Browser-Fallback sind hier bewusst deaktiviert (siehe
`monitor.yml`), damit jeder Lauf kurz und günstig bleibt.

### Voller Funktionsumfang: eigener (kostenloser) Server

Für Dashboard, dauerhaftes 60s-Polling und den Truth-Social-Browser-Fallback braucht es
einen durchlaufenden Prozess statt eines Cron-Jobs - dafür eignet sich ein kostenloser
Oracle-Cloud-VPS:

### Option 0: Oracle Cloud Free Tier (kostenlos, voller Funktionsumfang)

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

Der Container läuft als nicht-root User (`appuser`) und deklariert einen
`HEALTHCHECK`, der `/api/health` abfragt – `docker ps`/`docker compose ps` zeigen
den Container also auch dann als `unhealthy` an, wenn der Monitoring-Loop wegen
eines Konfigurationsfehlers (z.B. falscher `ANTHROPIC_API_KEY`) gar nicht erst
gestartet ist, obwohl der Prozess selbst noch läuft.

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
| `ALERT_CONFIDENCE_THRESHOLD` | Ab welcher Gesamt-Konfidenz (0-1) eine Meldung überhaupt für einen Alert in Frage kommt (Standard 0.5) |
| `ALERT_MIN_TICKER_CONFIDENCE` | Präzisions-Filter: Alert nur, wenn eine konkrete Aktie mit klarer Long/Short-Richtung diese Pro-Ticker-Konfidenz erreicht (Standard 0.90 = sehr streng; `0.85`/`0.80` lockern, `0` schaltet den Filter ab) – siehe Abschnitt oben |
| `MAX_CONCURRENT_CLASSIFICATIONS` | Wie viele Claude-Calls parallel laufen dürfen (Standard **1** = seriell, siehe Duplikat-Hinweis oben; höher = schneller bei Nachrichtenschüben, aber Risiko doppelter Alerts) |
| `DEDUP_SIMILARITY_THRESHOLD` | Ab welcher Textähnlichkeit (0-1) zwei Statements als Duplikat gelten (Standard 0.82) |
| `DEDUP_WINDOW_SECONDS` | Zeitfenster für die Text-Duplikatsuche, Tier 1 (Standard 24h) |
| `TOPIC_CONTEXT_WINDOW_HOURS` / `TOPIC_CONTEXT_MAX_ITEMS` | Wie viele Stunden zurück / wie viele Meldungen als Themen-Kontext an Claude mitgegeben werden, Tier 2 (Standard 24h / 20) |
| `ALERT_DIGEST_THRESHOLD` | Ab wie vielen gleichzeitigen Alerts zu einer Sammel-Nachricht gebündelt wird (Standard 3) |
| `CLAUDE_MODEL` | Modell fuer die Klassifikation (Standard `claude-haiku-4-5`, guenstig; `claude-sonnet-5` fuer potenziell bessere Qualitaet zu mehrfachen Kosten) |
| `MAX_CLASSIFICATIONS_PER_DAY` | Harter Kostendeckel: mehr Claude-Calls finden an einem Tag (UTC) nicht mehr statt (Standard 100 ≈ max. 0.15-0.25 €/Tag bei Haiku) - siehe Abschnitt oben |
| `PRIORITY_CLASSIFICATIONS_PER_DAY` | Extra-Reserve oberhalb von `MAX`, die nur wichtige Meldungen (direkte Trump-Posts, harte Wirtschaftsthemen) nutzen dürfen - absolutes Tages-Maximum = `MAX + PRIORITY` (Standard 30, also 130 gesamt; `0` deaktiviert die Reserve) - siehe Abschnitt oben |
| `TELEGRAM_STARTUP_NOTICE` | Heartbeat-Nachricht beim Start senden (Standard an) |
| `TRUTH_SOCIAL_BROWSER_FALLBACK` | Playwright-Fallback für Truth Social an/aus (Standard an) |
| `CLAUDE_MAX_RETRIES` / `CLAUDE_TIMEOUT_SECONDS` | Robustheit der Claude-API-Calls |
| `DASHBOARD_API_KEY` | Schützt `/api/statements`, `/api/stats`, `/api/test` mit einem `X-API-Key`-Header. **Unbedingt setzen**, sobald das Dashboard von außen erreichbar ist (z.B. Oracle-Cloud-Anleitung mit offenem Port 8000) – sonst kann jeder, der die IP kennt, Claude-Kosten verursachen und/oder Telegram-Alerts auslösen. `/api/health` bleibt bewusst ungeschützt (für externe Uptime-Checks). Zufälligen Wert erzeugen z.B. mit `openssl rand -hex 24`. Das Dashboard selbst fragt den Key einmalig ab und merkt ihn sich im Browser (localStorage). |

`DB_PATH` **nicht** in `.env` setzen, wenn Docker verwendet wird – das Image hat
bereits `DB_PATH=/data/trump_monitor.db` passend zum Volume-Mount als Default. Ein
eigener Wert in `.env` würde diesen überschreiben und die DB außerhalb des Volumes
ablegen; beim nächsten `docker compose up --build` wäre der komplette Verlauf weg
(siehe Kommentar in `.env.example`).

## Datenbank

SQLite-Datei (`trump_monitor.db`, Pfad über `DB_PATH` konfigurierbar) mit Tabelle
`statements`: Text, Quelle, Zeitstempel, Klassifikation, `duplicate_of_id` (falls
als Duplikat erkannt), ob ein Alert verschickt wurde. Dient als Audit-Trail und
später für Backtesting (z.B. Kursverlauf nach Statement gegen die eigene
Einschätzung abgleichen).

## API-Endpunkte

| Endpunkt | Zweck | Auth |
|---|---|---|
| `GET /` | Dashboard | – |
| `GET /api/statements?limit=&only_relevant=` | Feed als JSON | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/stats` | Aggregierte Statistik | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/health` | Status pro Quelle, Konfigurationsfehler/-warnungen, Uptime, heutiger Verbrauch des Tages-Kostendeckels (`classification_calls_today`/`_limit`) | – (bewusst offen für Uptime-Checks) |
| `POST /api/test` | Beliebigen Text durch die volle Pipeline schicken (siehe oben), max. 4000 Zeichen | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |

## Tests

Die Test-Suite liegt in `tests/` (60+ Checks: Duplikaterkennung, Tages-Kostendeckel
inkl. Race-Sicherheit, Schema-Migration, Telegram-Formatierung inkl. HTML-Injection-
und Längen-Edge-Cases, Dashboard-Auth, Quellen-Filter). Alle Claude-/Telegram-Calls
sind gemockt - die Tests kosten nichts und laufen in Sekunden:

```bash
python tests/run_tests.py
```

Jede Testdatei läuft in einem eigenen Prozess (bewusst kein pytest, siehe Kommentar
in `tests/run_tests.py`). Über `.github/workflows/tests.yml` läuft die Suite
automatisch bei jedem Push - eine Regression erscheint sofort als rotes Kreuz am
Commit.
