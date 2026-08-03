# Market Impact Predictor

Überwacht allgemein marktrelevante Nachrichten und Aussagen - Wirtschaftsdaten,
Zentralbank-/Fed-Entscheidungen, Politiker- und CEO-Statements, Quartalszahlen,
Fusionen/Übernahmen, geopolitische Ereignisse und mehr, aus Nachrichtenfeeds
(GDELT/RSS), optional ergänzt um Original-Quellen-Posts (z.B. ein bestimmter
Truth-Social-Account). Lässt Claude für jede Meldung einschätzen,
ob sie marktrelevant ist (positiv/negativ/neutral, betroffene Ticker mit
Long/Short-Einschätzung je Aktie) und schickt bei Relevanz einen Telegram-Alert -
aber nur einmal pro Thema pro Tag, außer die Lage eskaliert wirklich. Ein
Web-Dashboard zeigt den Live-Feed inkl. Status-, Statistik- und
Trefferquoten-Übersicht.

**Kein Trading-Signal / keine Finanzberatung.** Die Einschätzungen (inkl. Long/Short)
sind LLM-generiert und können falsch liegen - eigene Anlageentscheidung auf eigenes Risiko.

> **Branch-Hinweis:** Dieses Repository hat (noch) keinen `main`-Branch. Produktiv -
> also der Branch, auf dem der GitHub-Actions-Cron (`.github/workflows/monitor.yml`)
> tatsächlich läuft - ist immer der **Default-Branch** des Repos (aktuell
> `claude/trump-market-impact-analyzer-dbjvu5`), sichtbar oben links im GitHub-
> Branch-Dropdown bzw. unter *Settings → Branches*. GitHub-Actions-`schedule`-Trigger
> feuern ausschließlich auf dem Default-Branch, egal wie viele andere Branches
> existieren oder wie sie heißen - Änderungen auf anderen Branches (z.B.
> Review-/Feature-Branches) haben also erst nach einem Merge in den Default-Branch
> einen Effekt auf den laufenden Bot.

## Architektur

```
Quellen (News/Truth Social) --poll()--> Orchestrator (Supervisor, Concurrency)
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
   marktrelevante Meldung zu warten. Programmatisch äquivalent:
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
| FED-Live-Audio (optional, Standard aus) | `app/sources/fed_audio.py` | Nur innerhalb konfigurierter Zeitfenster aktiv | Sekunden, während des Fensters |

Quellen einzeln an/aus schalten über `.env` (`ENABLE_NEWS`, `ENABLE_TRUTH_SOCIAL`,
`ENABLE_FED_AUDIO`).

**Allgemeine Abdeckung, nicht auf eine Person eingeschränkt:** GDELT und RSS filtern
bewusst NICHT auf ein bestimmtes Thema/eine bestimmte Person, sondern decken alle
Nachrichten ab, die zu marktrelevanten Stichworten passen (Zölle, Zinsen/Fed,
Sanktionen, Quartalszahlen, Fusionen/Übernahmen, Ratingänderungen, Wirtschaftsdaten
u.v.m. - siehe `MARKET_KEYWORDS` in `app/sources/news_gdelt.py`). Truth Social bleibt
eine optionale, auf einen konfigurierbaren Account eingeschränkte Zusatzquelle
(`TRUTH_SOCIAL_HANDLE`, Standard der Account von Donald Trump) für dessen unmittelbare
eigene Worte - die eigentliche Marktabdeckung hängt nicht an dieser einen Quelle.

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

### FED-Live-Audio im Detail

Hört während konfigurierter Zeitfenster (typischerweise FOMC-Pressekonferenzen) einen
Livestream mit, transkribiert per [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
und speist den Text als ganz normales Statement in die Klassifikations-Pipeline ein.

**Sicherheitshinweis:** Es gab bereits eine ähnliche Live-Audio-Quelle für beliebige
Streams, die komplett entfernt wurde, weil der transkribierte Text direkt in ein
GitHub-Actions-Skript interpoliert wurde (Script-Injection, CWE-94). Diese Quelle läuft
stattdessen wie jede andere Quelle in-process im Dauerbetrieb (kein Workflow-Trigger),
der transkribierte Text landet nur als Statement-Text in der DB und durchläuft die
normale Claude-Klassifikation - keine Shell-/Workflow-Interpolation an irgendeiner
Stelle.

**Bewusst nur die FED, nicht beliebige Livestreams:** FOMC-Sitzungstermine stehen
Monate im Voraus fest, der Bot hört also nur in einem klar begrenzten Zeitfenster zu
statt dauerhaft - relevant auf einer ressourcenknappen VM (siehe Oracle-Cloud-Anleitung
oben, 1GB RAM).

Einrichtung:

1. `ENABLE_FED_AUDIO=true`
2. `FED_AUDIO_STREAM_URL` auf einen Live-Stream setzen (z.B. der offizielle
   YouTube-Kanal der Federal Reserve)
3. `FED_MEETING_WINDOWS` mit den aktuellen Terminen befüllen, Format
   `ISO-Startzeit(UTC)/Dauer-in-Minuten`, Komma-getrennt, z.B.
   `2026-09-16T18:30/90,2026-11-04T19:00/90`. Aktuelle Termine:
   [federalreserve.gov/monetarypolicy/fomccalendars.htm](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) -
   bewusst nicht vorbefüllt, da es dafür keine verlässliche automatisierte Quelle gibt.

Braucht zusätzlich das System-Paket `ffmpeg` (im mitgelieferten Dockerfile bereits
enthalten) sowie `faster-whisper`/`yt-dlp` (in `requirements.txt`). Bei Docker/Oracle-
Cloud-Deployment ist alles bereits vorbereitet, nur die drei `.env`-Werte oben fehlen.

Ein Fehlschlag beim Auflösen der Stream-URL oder beim Mitschnitt wird wie bei jeder
anderen Quelle in `/api/health` sichtbar (`last_error` bei `fed_audio`); Stille
innerhalb des Fensters (z.B. Pause in der Pressekonferenz) gilt dagegen nicht als
Fehler.

### Sonstige Einschränkungen

- **GDELT liefert Artikel-Titel**, nicht das wörtliche Zitat — gut als Signal,
  aber kein Ersatz für O-Töne.
- Kostenlose Aktienkurse sind i.d.R. 15 Min. verzögert; für echte Realtime-Kurse
  braucht es einen kostenpflichtigen Anbieter (nicht Teil dieses Projekts, aber
  leicht als weiterer Alert-Datenpunkt ergänzbar).

## Technische Analyse (TradingView-Stil)

Zusätzlich zum Nachrichten-/Textsignal holt sich der Predictor eine **technische
Zweitmeinung** aus den Kursdaten — analog zum „Technicals"-Panel auf TradingView.
Aktivieren über `ENABLE_TECHNICALS=true` (im Workflow bereits an).

Für jeden handelbaren Ticker eines Alerts wird die **Tageshistorie** (Stooq, best-effort,
kein Key) geladen und ein großer Teil der TradingView-Standardindikatoren in reinem
Python berechnet (`app/indicators.py`, keine zusätzlichen Dependencies).

**Zwei-Quellen-Fallback (`app/prices.py`):** Liefert Stooq keinen Kurs/keine Historie
(live beobachtet: vorbörslich fehlt bei kleineren/jüngeren Werten wie RKLB oft ein
frisches Close, Stooq antwortet dann mit „N/D"), wird automatisch **Yahoo Finance**
(ebenfalls kostenlos, kein Key) als zweite Quelle versucht, bevor der Ticker als
„kein Kurs abrufbar" gilt. Dabei wird bevorzugt ein echter Vor-/Nachbörslich-Kurs
verwendet, falls vorhanden - genau die Lücke, die Stooq vorbörslich oft nicht deckt.

- **Gleitende Durchschnitte:** SMA & EMA (10/20/30/50/100/200), VWMA(20), Hull-MA(9),
  Ichimoku-Basislinie(9/26/52)
- **Oszillatoren:** RSI(14), Stochastik(14,3,3), Stochastik-RSI, CCI(20), ADX(14)+DI,
  Awesome Oscillator, Momentum(10), MACD(12,26,9), Williams %R(14), Bull/Bear Power(13),
  Ultimate Oscillator(7,14,28)
- **Kontext:** ATR(14), Bollinger-Bänder(20,2), ROC, OBV

Daraus wird — nach der TradingView-Methodik (je Indikator Buy/Sell/Neutral, Mehrheit je
Gruppe, Mittel aus MA- und Oszillator-Gruppe) — eine **Gesamtbewertung** gebildet:
**Strong Buy / Buy / Neutral / Sell / Strong Sell**.

**Wie das den Predictor verbessert:**
- Die Bewertung steht im Alert (mit MA-/Oszillator-Zählung und RSI/MACD/ADX) und zeigt,
  ob die Technik das Signal **✅ bestätigt** oder ihm **⚠️ widerspricht**.
- Bei Bestätigung/Widerspruch wird der **Überzeugungs-Score angehoben/gesenkt**
  (`TECHNICALS_CONVICTION_WEIGHT`, Standard ±10) — das wirkt auf alle nachgelagerten
  Gates **und** auf die Paper-Trading-Positionsgröße.
- Optional (`TECHNICALS_REQUIRE_AGREEMENT=true`) wird ein Alert **unterdrückt**, wenn die
  Technik klar widerspricht (Long trotz „Strong Sell" bzw. Short trotz „Strong Buy").
  Standard aus — die Technik reichert dann nur an.

Die Indikator-Mathematik ist mit bekannten Reihen unit-getestet (`tests/test_indicators.py`).
Bewusst best-effort: ist die Historie nicht erreichbar, entfällt die Technik still, der
Alert läuft normal weiter.

## VIX-Marktregime-Gate

Zusätzlich zum einzelnen Ticker-Signal berücksichtigt der Predictor den **Gesamtmarkt**:
der **VIX** (CBOE Volatility Index, marktweiter „Angst-Indikator") wird über Stooq (`^vix`,
kostenlos, kein Key) geholt. Aktivieren über `ENABLE_VIX_GATE=true` (im Workflow bereits an).

Bei hoher Marktangst (`VIX_HIGH_THRESHOLD`, Standard 30) bewegt sich oft der **gesamte
Markt** chaotisch statt entlang des konkreten Katalysators — Korrelationen zwischen
Sektoren steigen, ein einzelnes direktionales Signal wird unzuverlässiger. Das Gate:
- senkt den **Überzeugungs-Score** um `VIX_CONVICTION_PENALTY` (Standard 10) — das wirkt
  auf alle nachgelagerten Gates und die Paper-Trading-Positionsgröße,
- kann optional (`VIX_SUPPRESS_ABOVE`, Standard 0 = aus) einen Alert bei extrem hohem VIX
  ganz **unterdrücken**,
- zeigt den aktuellen VIX-Stand im Alert an (📊).

Best-effort: ist der Kursdienst nicht erreichbar, entfällt das Gate für diesen Zyklus
einfach (kein Fehler, kein Suppress).

## Paper-Trading (virtuelles Depot)

Ein **rein virtuelles Depot** (Standard-Startkapital **500 €**, `PAPER_STARTING_CAPITAL`),
das die Signalgüte mit echten Kursen mitverfolgt — **kein echtes Geld, kein Broker, keine
Orderausführung, keine Anlageberatung.** Aktivieren über `PAPER_TRADING=true` (im
GitHub-Actions-Workflow bereits an, abschaltbar über die Repo-Variable `PAPER_TRADING`).

**Was passiert:**
- Bei **jedem tatsächlich verschickten Alert** (Long/Short) wird für die handelbaren
  Ticker eine **virtuelle Position eröffnet** — für ALLE, nicht nur eine Auswahl
  (`PAPER_MAX_POSITIONS`, Standard 100, ist nur noch ein hohes Sicherheitsnetz; der
  eigentliche Deckel ist das freie Kapital). Der Einstiegskurs ist immer der
  **tatsächliche aktuelle Kurs** zum Alarm-Zeitpunkt (Stooq-Live-Quote, best-effort) —
  läuft ein Alert z.B. vorbörslich bei 100 €, merkt sich der Bot 100 €, nicht einen
  nachträglich "korrigierten" Wert. Die Eröffnungs-Meldung zeigt dazu die Börsen-Session
  an (🌅 vorbörslich / 🟢 Börse offen / 🌆 nachbörslich / 🌙 zu), damit transparent ist,
  in welcher Phase der Kurs abgefragt wurde. Die **Positionsgröße bestimmt der Bot
  selbst** aus dem Überzeugungs-Score (Anteil des Depotwerts: stärkeres Signal → mehr
  Kapital, gedeckelt, siehe `app/paper_trading.py: position_fraction`).
- **Stop-Loss / Take-Profit** werden aus der Tagesspanne abgeleitet (`suggest_risk_levels`)
  und schließen die Position **automatisch**, sobald der Kurs sie erreicht — mit sofortiger
  Telegram-Meldung.
- **Trailing-Stop** (`PAPER_TRAILING_STOP`, Standard an): Sobald eine Position
  `PAPER_TRAIL_ACTIVATE_R` (Standard 1.0) mal ihr Anfangsrisiko im Plus steht, zieht der
  Stop mit dem Hochpunkt mit (`PAPER_TRAIL_DISTANCE_R` dahinter) und das feste Ziel wird
  ignoriert — **Gewinner dürfen laufen**. Hintergrund: mit fixem Ziel ist der Gewinn je
  Trade bei ~1,5R gedeckelt, der Verlust bei 1R. Break-even bräuchte damit rechnerisch
  **40 % Trefferquote** (1/(1+1,5)) — exakt der Wert, den ein reiner Zufallskurs liefert.
  Der gesamte Erwartungswert hinge also allein daran, dass das Nachrichtensignal echte
  Vorhersagekraft hat, und Spread/Slippage fräßen den Rest. Der Trail hebt den mittleren
  Gewinn, ohne den Verlust je Trade zu vergrößern.
- **Maximale Haltedauer** (`PAPER_MAX_HOLDING_HOURS`, Standard 24 h): Eine Position, die
  weder Stop noch Ziel erreicht, wird danach zum aktuellen Kurs glattgestellt
  (`Haltedauer abgelaufen`). Ohne das bliebe sie **unbegrenzt** offen und würde ihren
  Einsatz dauerhaft binden — da der freie Barbestand (nicht `PAPER_MAX_POSITIONS`) der
  eigentlich bindende Faktor ist, bekäme nach den ersten Alerts **kein weiterer Alert**
  mehr eine Position.
- **Handelskosten** (`PAPER_COST_BPS`, im Workflow 10 bps je Seite): Spread/Gebühren
  werden vom realisierten Ergebnis abgezogen. Das macht die Zahlen nicht besser, sondern
  **ehrlich** — eine Strategie, die nur ohne Kosten funktioniert, sollte man nicht mit
  echtem Geld nachbauen.
- Ein **Gegensignal** (z.B. offene Long-Position, neuer Short-Alert auf denselben Ticker)
  **dreht die Position**: die alte wird glattgestellt, die neue eröffnet.
- Optional ein **laufender Depot-Status** per Telegram — „auf wie viel steht alles":
  Gesamtwert, freier Bestand und je Position der aktuelle Kurs samt (noch nicht
  realisiertem) Gewinn/Verlust. Gedrosselt auf `PAPER_STATUS_INTERVAL_MINUTES`
  (**Standard 0 = aus** — Eröffnungen und Schließungen melden sich ohnehin **immer
  sofort**, unabhängig davon; ein zusätzlicher periodischer Status ist rein optional
  und muss explizit gesetzt werden, sonst spammt er).
- Der Depot-Zustand (offene/geschlossene Positionen, realisierter Gewinn) liegt in der
  SQLite-DB und **überlebt einzelne GitHub-Actions-Läufe** über den DB-Cache.
- **Kapitalerhalt-Modus** (`PAPER_CAPITAL_PRESERVATION`, Standard an): laufen
  `PAPER_LOSS_STREAK_THRESHOLD` (Standard 3) geschlossene Verlust-Trades **in Folge**,
  wird die Positionsgröße automatisch mit `PAPER_LOSS_STREAK_SIZE_FACTOR` (Standard 0.5 =
  halbiert) verkleinert — Risk-off nach einer Pechsträhne, statt unverändert
  weiterzumachen. Der Depot-Status weist aktives Risk-off mit 🛡 aus.
- **Harte Risikogrenzen** (Kapitalschutz, unabhängig vom Kapitalerhalt-Modus oben):
  `PAPER_MAX_DRAWDOWN_PCT` (Standard 20 %) sperrt das Eröffnen **neuer** Positionen,
  sobald der Depotwert um diesen Prozentsatz vom bisherigen **Höchststand** (nicht vom
  Startkapital) gefallen ist — selbstheilend, sobald sich das Depot wieder über die
  Schwelle erholt. `PAPER_MAX_TOTAL_EXPOSURE_PCT` (Standard 60 %) sperrt neue Positionen,
  sobald so viel vom Depotwert bereits in offenen Positionen gebunden ist. Beide sperren
  **nur neue** Positionen — laufende werden nie zwangsliquidiert, sie behalten ihre
  Stop-/Ziel-Marken. Eine gesperrte Eröffnung wird wie jede andere „keine Position"-Lücke
  sichtbar per Telegram gemeldet (kein stilles Nichtstun). 0 = jeweils aus.

Braucht erreichbare Kursdaten (Stooq, best-effort — wie das Preis-Tracking); ist der
Kursdienst mal nicht erreichbar, entfällt das Eröffnen/Bewerten still. Telegram muss
konfiguriert sein, sonst laufen die Positionen nur stumm in der DB mit.

## Replay (Auswertung der eigenen Klassifikationen) — kostenlos

**In den meisten Fällen der bessere Weg als der Backtest unten.** `app/replay.py` wertet
die **bereits vorhandenen** Claude-Klassifikationen aus der Produktions-Datenbank gegen
echte Kursverläufe aus — ohne einen einzigen neuen Claude-Call und ohne GDELT:

```bash
python -m app.replay --db-path /data/trump_monitor.db
```

Warum das dem Backtest überlegen ist, wo es geht:

- **Kosten: null.** Die teure Arbeit (Klassifikation) hat der Live-Bot für jedes
  gespeicherte Statement längst erledigt. Ausgewertet wurden bisher aber nur die wenigen,
  die durch *alle* Alarm-Gates kamen — die große Mehrheit lag ungenutzt herum.
- **Echtes Out-of-Sample.** Die Einschätzungen wurden **live** getroffen, bevor sich der
  Kurs bewegt hat. Ein klassischer Backtest klassifiziert alte Nachrichten mit dem
  heutigen Modell und trägt damit ein Lookahead-Risiko (das Modell könnte den Ausgang
  aus seinem Training kennen). Dieses Risiko gibt es hier prinzipiell nicht.
- **Beantwortet Fragen, die der Backtest nicht kann:** Korreliert Claudes Konfidenz
  überhaupt mit der Trefferquote (`nach_konfidenz` — Ticker-Calls über die *ganze*
  Spanne, nicht nur die, die das 90 %-Gate passiert haben)? Und bringen die Alarm-Gates
  etwas (`alarmiert_vs_unterdrueckt`)?

Die Produktions-DB wird **strikt lesend** geöffnet (SQLite `mode=ro`) — ein
Schreibversuch würde von SQLite selbst abgelehnt, der laufende Bot kann durch einen
Auswertungslauf nicht gestört werden.

**Grenzen:** reicht nur so weit zurück wie der Bot läuft, nur die tatsächlich genutzten
Quellen, und wie beim Backtest nur Tages-Schlusskurse (Horizont in Handelstagen, nicht
in Minuten wie im Live-Betrieb).

## Backtest (historische Auswertung)

`app/backtest.py` spielt einen selbst gewählten historischen Zeitraum durch **dieselbe**
Gating-Logik wie die Live-Pipeline (`is_alert_worthy()`/`actionable_tickers()` aus
`app/orchestrator.py`) und wertet aus, wie sich die aufgerufenen Ticker in den
Handelstagen danach tatsächlich bewegt haben — aufgeschlüsselt nach
Marktkapitalisierungs-Klasse (micro/small/mid/large, `app/prices.py`), um zu prüfen, ob
Small-Caps tatsächlich einen geringeren Verzögerungs-Nachteil haben, statt es nur zu
behaupten.

**Quellenwahl (`--source`):**

| | `alphavantage` (empfohlen) | `finnhub` | `gdelt` |
|---|---|---|---|
| Historie | Jahre | Jahre | unklar |
| Abdeckung | marktweit | **nur vorgegebene Ticker** (`--tickers`, Standard `DEFAULT_FINNHUB_TICKERS`) | marktweit |
| API-Key | ja, **kostenlos** ohne Kreditkarte | ja, **kostenlos** ohne Kreditkarte | nein |
| Rate-Limit | wenige Abfragen/Tag, aber **1000 Artikel je Abfrage** → 3 Monate mit ~13 Abfragen | **~60 Abfragen/Minute**, aber 1 Abfrage je Ticker | 1 Anfrage/5 s, sperrt bei Verstoß die **IP für Stunden** (siehe unten) |

Für Zeiträume über wenige Tage ist `--source alphavantage` praktisch alternativlos —
GDELTs IP-Sperre macht längere Läufe unbrauchbar. Key in die `.env` als
`ALPHAVANTAGE_API_KEY` (siehe `.env.example`).

`--source finnhub` ist der Ausweg, wenn Alpha Vantages Tageskontingent (~25
Abfragen/Tag) an EINEM Tag schon durch vorherige Test-/Diagnoseläufe aufgebraucht ist —
Finnhubs Gratis-Tarif erlaubt stattdessen ~60 Abfragen/Minute, genug für mehrere
Neuansätze am selben Tag. Der ehrliche Unterschied: Finnhubs `company-news` ist **nur
pro Ticker** abrufbar, kein marktweiter Endpunkt auf dem Gratis-Tarif. Statt dass Claude
den betroffenen Ticker selbst aus einem allgemeinen Artikel ableitet, wird hier vorab
eine Ticker-Liste abgefragt (Standard: 24 Ticker über micro/small/mid/large-Cap
gestreut, siehe `app.backtest.DEFAULT_FINNHUB_TICKERS`, überschreibbar per
`--tickers AAPL,MSFT,...`). Das trägt ein echtes, von Menschen kuratiertes
Auswahl-Risiko — der Report weist die tatsächlich genutzte Liste deshalb offen als
`tickers` aus, statt es zu verschleiern. Key in die `.env` als `FINNHUB_API_KEY`
(siehe `.env.example`, kostenlos unter finnhub.io/register).

**Kostenlimit (`--budget-usd`)** — wichtiger als `--max-calls`, weil in Geld statt in
Stückzahl gedacht: Passen nicht alle gefundenen Artikel ins Budget, wird eine
**zufällige Stichprobe über den gesamten Zeitraum** gezogen (nicht die ersten N — das
würde nur den Anfang abdecken und die Auswertung auf eine einzelne Marktphase
verzerren). Das Limit greift **doppelt**: bei der Planung *und* als harter Abbruch
während des Laufs, falls die Kostenschätzung danebenliegt. Fester Zufalls-Seed, damit
derselbe Aufruf dieselbe Stichprobe liefert.

```bash
# 3 Monate abdecken, aber hoechstens 3 $ ausgeben (~800 Klassifikationen):
python -m app.backtest --source alphavantage \
  --start 2026-05-05 --end 2026-08-03 --budget-usd 3.0 --max-calls 0 --execute
```

```bash
# Gleiches Budget, aber ueber Finnhub (z.B. weil das Alpha-Vantage-Tageskontingent
# gerade aufgebraucht ist) - Standard-Tickerliste, optional per --tickers ersetzbar:
python -m app.backtest --source finnhub \
  --start 2026-05-05 --end 2026-08-03 --budget-usd 3.0 --max-calls 0 --execute
```

```bash
# 1. Immer zuerst ohne --execute: reiner Kostenvoranschlag, KEIN Claude-Call
python -m app.backtest --start 2026-06-01 --end 2026-06-07

# 2. Erst nach Prüfung des Voranschlags tatsächlich klassifizieren (max. 30 echte
#    Calls in diesem Beispiel, unabhängig von der Zeitraumgröße). --horizon-days nimmt
#    eine kommagetrennte Liste (Standard '1,3,5,10') - ALLE Horizonte werden aus
#    DERSELBEN Klassifikation ausgewertet, kostet also nicht mehr als einer.
python -m app.backtest --start 2026-06-01 --end 2026-06-07 --execute --max-calls 30
```

**Wichtig, ehrlich:**
- **Kostenkontrolle**: Ohne `--execute` wird **niemals** ein echter Claude-Call gemacht
  — nur ein aus den tatsächlichen Prompt-Bestandteilen hergeleiteter Kostenvoranschlag.
  Mit `--execute` begrenzt `--max-calls` die Anzahl echter Calls **hart**, unabhängig vom
  normalen Tages-Limit der Live-Pipeline (das gilt hier nicht — der Backtest läuft immer
  gegen eine **isolierte** Datenbank, nie gegen die Produktions-DB, siehe `--db-path`).
- **Historische Abdeckung**: Ob/wie weit GDELTs kostenlose API rückwirkend Daten
  liefert, ist nicht garantiert — ein leerer `raw_fetched`-Wert für einen weit
  zurückliegenden Zeitraum ist das ehrliche Signal dafür, kein Bug.
- **Rate-Limit — der wichtigste Fallstrick**: GDELT nennt in seiner 429-Antwort
  *"Please limit requests to one every 5 seconds"*. Zwischen den täglichen Chunks wird
  deshalb mit Sicherheitsmarge pausiert (8s), `fetch_range()` retried zusätzlich
  geduldig (5 Versuche, langer Backoff).

  **Aber Vorsicht:** Live beobachtet — hat man das Limit einmal deutlich gerissen
  (z.B. ein 30-Tage-Backtest mit zu kurzer Pause), sperrt GDELT die **IP** für längere
  Zeit und antwortet danach selbst auf *einzelne*, weit auseinanderliegende Anfragen
  (>15 Min Abstand, simpelste Query) weiterhin mit 429. Das trifft dann auch den
  **Live-Poll** des Bots, nicht nur den Backtest. Dagegen gibt es einen
  Schutzschalter (`GDELT_RATE_LIMIT_COOLDOWNS_SECONDS`): nach einem 429 legt die Quelle
  sich selbst für 15 → 30 → 60 → 120 Minuten still, statt weiter dagegen zu hämmern
  (das verlängert die Sperre nur). Der Zustand ist in `/api/health` unter
  `sources.news_gdelt.last_error` sichtbar; ein einziger Erfolg setzt alles zurück.

  Der Backtest-Report weist fehlgeschlagene Tage über `days_failed`/`days_total`
  **getrennt** von `raw_fetched` aus, damit "GDELT hat wirklich nichts" nicht mit "der
  Abruf ist fehlgeschlagen" verwechselt wird. Bei vielen fehlgeschlagenen Tagen:
  **abwarten** (nur das hilft) und danach mit einem deutlich kleineren Zeitraum
  wieder anfangen.
- **Auswertungs-Horizont**: Die Live-Pipeline bewertet ein Ergebnis über ein kurzes
  Intraday-Fenster (`PRICE_OUTCOME_HORIZON_MINUTES`, Standard 60 Minuten). Für die
  Vergangenheit liefern die hier genutzten kostenlosen Quellen aber nur
  Tages-Schlusskurse — der Backtest misst die Bewegung deshalb zwangsläufig über
  **Handelstage** (`--horizon-days`), nicht Minuten. Das ist keine gleichwertige
  Nachbildung des Live-Verhaltens, sondern die beste mit kostenlosen Daten mögliche
  Näherung.
- **Mehrere Horizonte gleichzeitig**: `--horizon-days 1,3,5,10` (Standard) wertet
  denselben klassifizierten Artikel für alle vier Haltezeiträume aus (nur Kursdaten,
  die sind gratis) und liefert im Report zusätzlich `by_horizon` — beantwortet "welcher
  Haltezeitraum funktioniert besser" ohne einen zweiten bezahlten Lauf.
  `hit_rate`/`by_tier` beziehen sich immer auf den ersten (kleinsten) Horizont der Liste.
- Nur GDELT als Quelle (RSS/Truth Social/Fed-Audio haben keine historische Abfrage-API),
  ohne Grenzfall-Eskalation (planbare statt variable Kosten pro Call).

**Auch im Dashboard** (`GET /`): Panel "Backtest (historische Auswertung)" mit
Von/Bis-Datum, Horizonte (kommagetrennt) und Max.-Calls-Feldern. "💡 Kostenvoranschlag" macht — wie die
CLI ohne `--execute` — nie einen echten Call. "⚠️ Ausführen" fragt vorher per
Bestätigungsdialog nach, läuft serverseitig in einem **eigenen Subprozess** (nicht im
Dashboard-Prozess selbst, der ja parallel den Live-Loop fährt — sonst würde die
DB-Isolation des Backtests kurzzeitig auch den Live-Betrieb umleiten) und zeigt danach
Trefferquote, Ø-Bewegung und die Aufschlüsselung je Marktkapitalisierungs-Klasse direkt
im Panel. Serverseitig zusätzlich hart gedeckelt (max. 60 Tage Zeitraum, max. 100 echte
Calls, 10 Minuten Zeitlimit), unabhängig von den Eingabefeldern.

## Früher dran sein (Latenz)

Die Alerts kamen zuletzt teils erst, **als die Bewegung schon lief**. Zwei Gegenmaßnahmen:

- **Häufigeres Polling:** der GitHub-Actions-Cron läuft jetzt alle **5 Minuten** (vorher 15),
  das Job-Timeout entsprechend unter dem Intervall (4 Min). **Wichtig:** GitHub garantiert
  bei Schedule-Trigger-Intervallen unter ~15 Min keinen pünktlichen Start — bei hoher Last
  auf den öffentlichen Runnern kann sich ein Lauf um Stunden verzögern (beobachtbar in den
  Actions-Läufen). Für echte, verlässliche Sekunden-Latenz bleibt nur der **Dauerbetrieb**
  (`main.py`/Docker/systemd/Oracle Cloud Free Tier, siehe unten) — dort läuft der Loop
  ununterbrochen, unabhängig vom GitHub-Cron.
- **Parallele Klassifikation** (`MAX_CONCURRENT_CLASSIFICATIONS`, in dieser Vorlage/dem
  Workflow **2**, Code-Standard 1): die serielle Claude-Klassifikation ist typischerweise
  die dominante Zeitquelle *innerhalb* eines Zyklus (mehrere Sekunden je Meldung). Bei 2
  gleichzeitig laufenden Klassifikationen verkürzt sich diese Phase spürbar — auf Kosten
  eines geringfügig höheren Risikos, dass zwei fast zeitgleiche Meldungen zum selben
  Thema sich nicht gegenseitig als Duplikat erkennen (der Themen-Kontext wächst erst nach
  Abschluss einer Klassifikation) und beide einen Alert auslösen. Per Repo-Variable
  einstellbar, ohne Code zu ändern.
- **`MAX_NEWS_AGE_MINUTES`** enger fassen (z.B. 15 statt Workflow-Standard 180): filtert
  Meldungen heraus, die schon länger zurückliegen, bevor überhaupt ein Claude-Call
  ausgelöst wird — spart Zeit/Kosten für ohnehin meist schon eingepreiste News. Ebenfalls
  eine Repo-Variable, kein Code nötig.
- **„Zu spät"-Warnung im Alert:** ist der Kurs am Alarm-Tag bereits stärker als
  `LATE_MOVE_WARN_PCT` (Standard 3 %) **in Signalrichtung** gelaufen, weist der Alert
  ausdrücklich darauf hin, dass die Bewegung evtl. großteils gelaufen ist — so wird ein
  spätes Signal wenigstens als solches sichtbar (Gegenstück zur bestehenden
  Divergenz-Warnung für Bewegungen *gegen* die These).
- **Übernacht-/Vorbörsen-Gap-Antizipation** (`ENABLE_GAP_PREDICTION`, Standard an):
  Der Klassiker ist, dass ein Katalysator **kurz vor/nach Börsenschluss** aufkommt, über
  Nacht ausgehypt wird und der Kurs dann **vorbörslich schon extrem gappt** — zu spät zum
  Einsteigen. Kommt ein klar gerichteter Alert in einem Fenster, in dem der Markt ihn
  nicht mehr voll einpreisen kann — **nachbörslich, über Nacht, übers Wochenende** oder
  **kurz vor Schluss** (< `GAP_NEAR_CLOSE_MINUTES`, Standard 45 min) — sagt der Alert
  ausdrücklich: „🚀 Mögliche Übernacht-Rallye … Einstieg jetzt, **bevor** der Kurs zum
  nächsten Open hochgappt". Ist die Session bereits **vorbörslich**, wird stattdessen
  gewarnt, dass der Gap evtl. schon läuft. Rein zeit-/richtungsbasiert, kein Extra-Call.
- **Gap-Chase-Bewertung** (`ENABLE_GAP_CHASE_EVALUATION`, Standard an, braucht
  Preis-Tracking): das Gegenstück zur Antizipation oben — der Gap ist bereits passiert
  (Markt war zu, jetzt zur Börsenöffnung entsprechend extrem hoch/niedrig). Kurz **nach**
  der Eröffnung (`GAP_CHASE_WINDOW_MINUTES`, Standard 30 min) vergleicht der Bot den
  tatsächlichen Gap (heutiger Eröffnungskurs vs. gestriger Schluss, aus der
  Kurshistorie) mit Claudes geschätzter Gesamtbewegung: hat der Gap bereits
  `GAP_CHASE_TOO_LATE_RATIO` (Standard 80 %) davon aufgebraucht (ohne Schätzung:
  `GAP_CHASE_TOO_LATE_ABS_PCT`, Standard 6 %), rät der Alert vom (Nach-)Kauf ab
  („⏭ … riskant, Gap-Fade-Gefahr“); ist noch Luft, heißt es „🎯 … kann sich noch lohnen“
  samt einem groben Ausstiegs-Kursziel (aus der verbleibenden geschätzten Bewegung, sonst
  aus derselben Tagesspannen-Logik wie die normalen Stop-/Ziel-Vorschläge). Nur relevant,
  wenn der Gap überhaupt `GAP_CHASE_MIN_GAP_PCT` (Standard 3 %) erreicht. Der Alert zeigt
  dazu **immer einen Text-Fortschrittsbalken** (`[███████░░░] 70% bis 'zu spät'`), der
  auf einen Blick zeigt, wie nah der Gap an der "lohnt sich nicht mehr"-Schwelle ist —
  bewusst als reiner Unicode-Balken statt eines generierten Bild-Charts, damit kein
  zusätzlicher Dependency (matplotlib o.ä.) die Installationszeit der 48x/Tag frisch
  aufgesetzten GitHub-Actions-Läufe verlangsamt.
- **Verdichtete Kauf-/Verkaufsempfehlung** (`ENABLE_TRADE_RECOMMENDATION`, Standard an):
  fasst Überzeugungs-Score, technische Zweitmeinung, Gap-Chase-Timing und Gerücht-/
  Divergenz-Warnung des stärksten handelbaren Tickers zu **einer** klaren Zeile zusammen
  (z.B. „📢 Empfehlung: 🟢 Jetzt kaufen (hohe Überzeugung)" oder „📢 Empfehlung: 🟠
  Abwarten / nur kleine Position (Technik widerspricht klar)") — spart das manuelle
  Zusammenreimen aus mehreren Alert-Zeilen. Rein additiv aus bereits vorhandenen
  Signalen, kein zusätzlicher Claude-Call. **Marktphasen-bewusst**: ist der Markt
  gerade zu (vor-/nachbörslich, Wochenende), heißt es „Zum nächsten Handelsstart
  kaufen" statt „Jetzt kaufen" — eine echte Order kann außerhalb der Handelszeiten
  ohnehin nicht ausgeführt werden. Das Paper-Depot eröffnet die Position trotzdem
  sofort (zum best-effort aktuellen Kurs); nur der Text ändert sich. **Keine
  Anlageberatung.**

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
- **Trading-Ausbau (10 Verbesserungen)**: aufeinander abgestimmte Erweiterungen mit
  dem Ziel, Signale früher, sicherer und handlungsnäher zu machen:
  1. **Geringere Latenz** – Cron von 30 auf **15 Min** verkürzt, zusätzliche schnelle
     RSS-Feeds; für echte Sekunden-Latenz der Always-on-Modus (Docker/systemd,
     `run_forever`).
  2. **Kurs-Feedback / Backtesting** (`ENABLE_PRICE_TRACKING`) – nach jedem Alert wird
     der Kurs erfasst und nach einem Horizont erneut gemessen: **echte Trefferquote**
     statt Bauchgefühl (`app/prices.py`, Tabelle `alert_outcomes`, best-effort Stooq).
  3. **Konfidenz-Kalibrierung** – Dashboard-Panel „Trefferquote (Backtesting)" +
     `/api/calibration`: Trefferquote gesamt, je Konfidenz-Bucket und je Richtung.
  4. **Zweitmeinung für Grenzfälle** (`ENABLE_BORDERLINE_ESCALATION`) – Meldungen knapp
     an der Schwelle werden mit einem stärkeren Modell zweitgeprüft (nur diese
     Grenzfälle kosten extra, cap-bewusst).
  5. **Story-Threads** – bei einer Eskalation zeigt der Alert eine kompakte Zeitleiste
     der Themenentwicklung (`get_topic_thread`).
  6. **Volatilitäts-Flag** – bei Zöllen/Sanktionen/Fed etc. markiert der Alert „hohe
     Volatilität, Richtung evtl. unsicher (ggf. Straddle)".
  7. **Börsen-Session im Alert** – ist der US-Markt gerade offen/vor-/nachbörslich/zu?
     (`app/market_hours.py`).
  8. **Kontext: heutige Bewegung** – je handelbarem Ticker die heutige %-Bewegung im
     Alert (wenn `ENABLE_PRICE_TRACKING`).
  9. **Chart-Buttons** – Inline-Buttons unter dem Alert öffnen den Chart des jeweiligen
     Tickers (TradingView).
  10. **Persönlicher Filter** – Watchlist/Blocklist nach Tickern und Sektoren
      (`WATCHLIST_TICKERS`/`WATCHLIST_SECTORS`/`BLOCKLIST_TICKERS`).
  Alle netzabhängigen Teile (#2/#3/#8) sind **best-effort** – ist der Kursdienst nicht
  erreichbar, entfällt das Feature still (Circuit-Breaker), der Poll-Zyklus läuft normal
  weiter. Der ausgelieferte GitHub-Actions-Workflow und `.env.example` haben
  `ENABLE_PRICE_TRACKING` **an**, damit die Erfolgsmessung von Anfang an mitläuft (der
  reine Code-Standard ist weiterhin aus, damit die Tests offline/schnell bleiben); zum
  Abschalten eine Repo-Variable `ENABLE_PRICE_TRACKING=false` setzen. Kurs-/Trefferquoten
  sind Analyse-Hilfen, **keine Anlageberatung**.
- **Präzisions- & Feedback-Ausbau (15 weitere Verbesserungen)**: baut auf dem obigen
  Trading-Ausbau auf und schärft Signalqualität, Risiko-Handling und Nachvollziehbarkeit
  – alles ohne zusätzlichen Claude-Call (bis auf die zwei neuen Schätzfelder, die im
  bestehenden Call mitlaufen). Die neuen **Zustell-Gates sind bewusst standardmäßig aus**
  und ändern das Verhalten nur auf Wunsch:
  1. **Überzeugungs-Score (0–100)** (`ENABLE_CONVICTION_SCORE`) – verdichtet Gesamt-/
     Ticker-Konfidenz, Frische, Quellen-Bestätigung sowie Hedge-/Volatilitäts-Abschlag
     zu einer Zahl im Alert (`app/scoring.py`, rein rechnerisch).
  2. **Gerücht-/Konjunktiv-Erkennung** – Formulierungen wie „reportedly / könnte / in
     talks" senken den Score und markieren den Alert mit „unbestätigt/Gerücht".
  3. **Erwartete Bewegung (%) + Horizont** (`ALERT_MIN_EXPECTED_MOVE_PCT`) – Claude
     schätzt grob Größenordnung und Zeitraum; optional als Mindest-Gate.
  4. **Positionsgrößen-Einordnung** – aus dem Score eine grobe, ausdrücklich
     unverbindliche Kategorie (Sondierung/Standard/hohe Überzeugung).
  5. **Stop-Loss/Take-Profit-Vorschlag** (`ENABLE_RISK_LEVELS`) – aus der heutigen
     Tagesspanne als Volatilitätsmaß (Chance-Risiko ~1.5), nur mit Preis-Tracking.
  6. **Ticker-Cooldown** (`TICKER_ALERT_COOLDOWN_MINUTES`) – derselbe Ticker+Richtung
     nicht mehrfach innerhalb des Fensters → keine Doppel-Einstiege in dieselbe Story.
  7. **Ruhezeiten** (`QUIET_HOURS`) – nachts nur sehr überzeugte Alerts sofort; der Rest
     wird über den bestehenden Resend-Pfad nach Fensterende automatisch nachgeschickt.
  8. **Handelbares Universum / Liquiditäts-Gate** (`TICKER_UNIVERSE`) – nur Ticker aus
     einer Positivliste lösen Alerts aus; obskure/illiquide Kürzel fallen heraus.
  9. **Historische Pro-Ticker-Trefferquote** (`ENABLE_HISTORICAL_HITRATE`) – „bisher
     7/10 richtig" je Ticker im Alert, aus den Backtesting-Ergebnissen.
  10. **Wochen-Performance-Digest** (`ENABLE_WEEKLY_DIGEST`) – einmal pro Woche eine
      Telegram-Zusammenfassung (Trefferquote, beste/schlechteste Ticker).
  11. **`/api/performance`** – aggregierte Trefferquote/Ø-Rendite + beste/schlechteste
      Ticker als JSON.
  12. **Mehr Metriken in `/api/health`** – u.a. `alerts_sent_today` sowie pro Quelle die
      Zähler `prefiltered`/`stale` (verworfene Meldungen ohne Claude-Call).
  13. **Kurs-Cache + Circuit-Breaker** (`PRICE_CACHE_TTL_SECONDS`) – gleiche Quote nicht
      doppelt holen; nach mehreren Fehlern kurz gar nicht anfragen (Kursdienst schonen).
  14. **Stale-News-Filter** (`MAX_NEWS_AGE_MINUTES`) – zu alte Meldungen gar nicht erst
      klassifizieren (spart Calls; alte News sind meist eingepreist).
  15. **Multi-Quellen-Korroboration** – wird dieselbe Meldung von mehreren unabhängigen
      Quellen gebracht, zeigt der Alert „bestätigt durch N Quellen" und der Score steigt.
  Sämtliche Marken/Scores sind Analyse-Hilfen, **keine Anlageberatung**.
- **Money-, Kontext- & Analytics-Ausbau (12 weitere Verbesserungen)**: dritte Runde,
  wieder ohne zusätzlichen Claude-Call; verhaltensändernde Gates sind standardmäßig aus:
  1. **Globales Überzeugungs-Gate** (`ALERT_MIN_CONVICTION`) – ein einziger, geld-
     orientierter Regler: Alert nur ab Score ≥ Schwelle (0–100).
  2. **Richtungswechsel-Flag** – kippt die Einschätzung eines Tickers gegenüber dem
     letzten Alert (z.B. erst long, jetzt short), warnt der Alert „⟳ Richtungswechsel".
  3. **Kurs-Divergenz-Warnung** (`DIVERGENCE_WARN_PCT`) – läuft der Kurs heute schon
     gegen die These, „⚠️ Kurs läuft bereits gegen die These" (mit Preis-Tracking).
  4. **Sektor-Cluster-Hinweis** (`SECTOR_CLUSTER_MIN`) – mehrere Werte einer Branche
     zuletzt alarmiert = stärkeres Makro-Signal → „🧩 Sektor-Cluster".
  5. **Kelly-lite Positionsanteil** (`ENABLE_KELLY_SUGGESTION`) – grober, unverbindlicher
     Bankroll-Anteil (Half-Kelly, gedeckelt) aus der historischen Trefferquote.
  6. **Anti-Fatigue-Ratelimit** (`MAX_ALERTS_PER_HOUR`) – höchstens N Alerts/Stunde; die
     überzeugendsten zuerst, der Rest wird über den Resend-Pfad nachgeschickt.
  7. **Trefferquote je Quelle** – welche Quelle historisch zuverlässiger war
     (`/api/performance`, Feld `by_source`).
  8. **Schwellen-Empfehlung** – `/api/calibration` schlägt aus den Backtesting-Buckets
     eine belegte `ALERT_MIN_TICKER_CONFIDENCE`-Untergrenze vor.
  9. **CSV-Export** (`/api/outcomes.csv`) – alle Ergebnis-Datensätze für die Offline-
     Analyse (Spreadsheet).
  10. **Config-Validierung** – `validate()` warnt bei unsinnigen neuen Einstellungen
      (ungültige `QUIET_HOURS`, `ALERT_MIN_CONVICTION` > 100, `WEEKLY_DIGEST_WEEKDAY`
      außerhalb 0–6 …), statt still ins Leere zu laufen.
  11. **Zyklus-Timing** – `/api/health` zeigt `last_cycle_seconds` + gleitenden
      Durchschnitt (wird ein Poll-Zyklus langsam?).
  12. **Aktive-Gates-Übersicht** – Startup-Log und `/api/health` (`active_gates`) zeigen
      auf einen Blick, welche optionalen Gates gerade Alerts beeinflussen.
  Auch hier: alles Analyse-Hilfen, **keine Anlageberatung**.
- **Historische-Performance-Feedback** (`ENABLE_HISTORICAL_PERFORMANCE_GATE`, Standard
  **aus**): `ENABLE_HISTORICAL_HITRATE` zeigt die historische Pro-Ticker-Trefferquote
  bisher nur im Alert an - sie floss NICHT in die Entscheidung selbst ein, ein Ticker
  mit belegt schlechter Bilanz wurde also genauso behandelt wie einer mit durchweg
  guter. Dieses Gate schließt die Lücke: der stärkste handelbare Ticker eines Alerts
  wird anhand SEINER EIGENEN historischen Trefferquote (aus den ausgewerteten
  `alert_outcomes`, braucht `ENABLE_PRICE_TRACKING`) im Überzeugungs-Score hoch-/
  heruntergestuft (`HISTORICAL_PERFORMANCE_WEIGHT`, linear um die 50%-Coinflip-Marke) -
  der Bot lernt so aus seinen eigenen vergangenen Alerts für genau diesen Ticker, statt
  jede neue Meldung unabhängig davon gleich zu bewerten. Erst ab
  `HISTORICAL_PERFORMANCE_MIN_SAMPLES` ausgewerteten Alerts für diesen Ticker wirksam,
  damit nicht 1-2 Zufallstreffer den Score verzerren. Optional
  (`HISTORICAL_PERFORMANCE_SUPPRESS_BELOW`) wird ein Alert für einen Ticker mit belegt
  schlechter Trefferquote sogar komplett unterdrückt statt nur den Score zu senken.
- **Echte Nachrichtenzeit + Alter im Alert**: statt eines bloßen „gerade erfasst"-
  Zeitstempels liest jede Quelle jetzt die **tatsächliche Veröffentlichungszeit** aus
  (GDELT `seendate`, RSS `published_parsed`, Truth Social `created_at`; Fallback auf
  „jetzt", wenn die Quelle keins liefert). Der Alert zeigt daraus ein kompaktes Alter
  („🕒 vor 3 Min" / „vor 2 Std") – für eine Handelsentscheidung entscheidend, denn eine
  Stunden alte Meldung ist oft schon eingepreist, während eine gerade erschienene noch
  Bewegung bringen kann.
- **Markierung des handelbaren Tickers**: im Alert bekommen die Ticker, die die
  Präzisions-Schwelle (`ALERT_MIN_TICKER_CONFIDENCE`) tatsächlich erreichen, einen
  Stern `⭐`. So ist auf einen Blick klar, welcher Ticker der eigentliche, hochsichere
  Auslöser ist und welche nur Kontext mit geringerer Sicherheit sind (die weiterhin
  angezeigt werden, nach Konfidenz sortiert).
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
- **Billiger, Claude-freier Vorfilter (`ENABLE_PREFILTER`, Standard an)**: erste Stufe
  des Trichters, *bevor* ein Claude-Call ausgegeben wird. Seit der Verallgemeinerung
  (kein Personen-/Themenfilter mehr an den Quellen) kommt deutlich mehr Rohmaterial
  herein - jede Meldung würde sonst einen echten Call und einen Slot des
  Tages-Kostendeckels kosten, der Deckel wäre oft schon vormittags erschöpft. Der
  Vorfilter (`app/prefilter.py`) verwirft **offensichtliche Nicht-Ereignisse** per
  einfacher Muster-Denylist: Listicles (`"5 stocks to watch"`, `"7 charts …"`),
  Ratgeber/How-to/Erklärstücke, Kauf-Empfehlungs-Clickbait der Finanzportale (Motley
  Fool, Zacks, `"stocks to buy"`), reine Personal-Finance-/Werbe-Themen (401k, Roth
  IRA, Kreditkarten, Prime Day …). So bleibt die Abdeckung breit, aber die Kosten
  sinken, ohne dafür den Deckel anzuheben. Bewusst **konservativ (hohe Präzision statt
  hoher Trefferzahl)**: im Zweifel wird durchgelassen - ein fälschlich durchgelassener
  Grenzfall kostet nur einen Call und wird danach sauber von Claude als nicht relevant
  verworfen, ein fälschlich *verworfenes* echtes Ereignis wäre dagegen für immer
  verloren. Deshalb ausschließlich eine Denylist eindeutiger Nicht-Ereignis-Muster,
  keine „muss ein Signalwort enthalten"-Positivpflicht, die schlicht formulierte echte
  Meldungen verwerfen könnte. Der Zähler `prefiltered` pro Quelle steht in
  `/api/health`. Auf `false` setzen gibt jede Rohmeldung direkt an Claude (mehr Kosten).
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
  als *wichtig* eingestufte Meldungen nutzen dürfen: direkte Original-Quellen-Posts
  (z.B. von Truth Social, falls aktiviert - deren unmittelbare eigene Worte) sowie
  harte Wirtschaftsthemen (Zölle, Sanktionen, Zinsen/Fed,
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
  Gesamt-Timeout.
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
  nur Telegram-Alerts, kein Dashboard, Polling alle 15 Min statt 60s)
- **Voller Funktionsumfang** - Dashboard, 60s-Polling, Truth-Social-Browser-Fallback -
  braucht einen (kostenlosen) Server (z.B. Oracle Cloud Free Tier)

### Einfachster Weg: GitHub Actions (empfohlen zum Ausprobieren)

Kein eigener Server, keine Kreditkarte, keine SSH/Firewall-Konfiguration - nur dieses
GitHub-Repo. Ein Workflow (`.github/workflows/monitor.yml`) führt alle 15 Minuten
automatisch einen Poll-Zyklus aus (`run_once.py`: alle Quellen abfragen, klassifizieren,
bei Relevanz Telegram-Alert schicken) und beendet sich wieder. Der Dedup-Status
zwischen Läufen wird über den GitHub-Actions-Cache mitgeschleppt.

1. Repo auf GitHub forken/nutzen (dieser Branch: `claude/trump-market-impact-analyzer-dbjvu5`)
2. **Settings → Secrets and variables → Actions → New repository secret** und dort anlegen:
   - `ANTHROPIC_API_KEY` (Pflicht)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional, aber empfohlen für Alerts)
3. Fertig - der Workflow läuft automatisch alle 30 Min. Zum sofortigen Testen:
   Tab **Actions** → *Market Impact Predictor* → **Run workflow** (manueller Trigger).
4. Ergebnis im Actions-Log einsehbar (welche Statements klassifiziert wurden), Alerts
   kommen per Telegram.

**Wichtig bei privaten Repos:** GitHub gibt kostenlosen Accounts ca. 2000 Actions-Minuten/
Monat (bei öffentlichen Repos unbegrenzt). Ein Lauf alle 30 Min bleibt im Rahmen; bei
Bedarf im Workflow auf `*/15` verkürzen, wenn das Repo öffentlich ist oder genug
Freiminuten übrig sind.

Kein Dashboard in diesem Modus - Telegram ist der Alert-Kanal, `Actions`-Tab das Log.
Der Truth-Social-Browser-Fallback ist hier bewusst deaktiviert (siehe `monitor.yml`),
damit jeder Lauf kurz und günstig bleibt.

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
   klont dieses Repo, legt `.env` aus der Vorlage an und richtet automatische Updates
   per Cron ein (siehe unten).
5. **Konfigurieren und starten**:
   ```bash
   nano ~/trump-market-monitor/.env   # ANTHROPIC_API_KEY (+ optional Telegram) eintragen
   cd ~/trump-market-monitor && sudo docker compose up -d --build
   ```
6. Dashboard unter `http://<Server-IP>:8000` aufrufen, im Test-Panel einen Beispieltext
   durchjagen um zu prüfen, dass alles korrekt konfiguriert ist.

**Updates laufen danach automatisch:** `deploy/auto_update.sh` läuft alle 15 Minuten
per Cron, zieht sich neue Commits vom Produktions-Branch selbst (pull-basiert - keine
Zugangsdaten auf GitHub nötig, kein zusätzlicher offener Port), baut bei Bedarf neu
und rollt automatisch auf den zuletzt funktionierenden Commit zurück, falls der
Container danach nicht `healthy` wird. Log: `~/trump-market-monitor/auto_update.log`.
Manuelles Update bleibt weiterhin möglich: `cd ~/trump-market-monitor && git pull &&
sudo docker compose up -d --build`.

Läuft die VM schon von vor dieser Änderung, holt sie den Cron nachträglich mit einem
erneuten Lauf des Bootstrap-Skripts (siehe Schritt 4 oben) - Docker/Repo/`.env`
werden dabei übersprungen, es wird nur der Cron ergänzt.

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
| `WATCHLIST_TICKERS` / `WATCHLIST_SECTORS` | Nur diese Ticker/Sektoren melden (Komma-Listen); leer = alle (#10) |
| `BLOCKLIST_TICKERS` | Diese Ticker nie melden (Komma-Liste) (#10) |
| `TICKER_UNIVERSE` | Handelbares Universum / Liquiditäts-Gate: nur Ticker aus dieser Positivliste lösen Alerts aus; leer = kein Filter |
| `MAX_NEWS_AGE_MINUTES` | Stale-News-Filter: ältere Meldungen gar nicht erst klassifizieren (spart Calls); `0` = aus |
| `TICKER_ALERT_COOLDOWN_MINUTES` | Ticker-Cooldown: gleicher Ticker+Richtung nicht erneut alarmieren innerhalb des Fensters; `0` = aus |
| `QUIET_HOURS` / `QUIET_HOURS_TZ` / `QUIET_HOURS_MIN_CONVICTION` | Ruhezeiten (`START-ENDE`, lokale Zeit): nachts nur Alerts ≥ Überzeugungs-Schwelle sofort, der Rest wird nach Fensterende nachgeschickt; leer = aus |
| `ALERT_MIN_CONVICTION` | Globales Gate: Alert nur ab Überzeugungs-Score ≥ diesem Wert (0–100); `0` = aus |
| `MAX_ALERTS_PER_HOUR` | Anti-Fatigue-Ratelimit: max. N Alerts je rollierender Stunde, überzählige werden zurückgestellt; `0` = aus |
| `SECTOR_CLUSTER_MIN` / `SECTOR_CLUSTER_WINDOW_HOURS` | Sektor-Cluster-Hinweis ab N Meldungen derselben Branche im Fenster (`MIN ≤ 1` = aus) |
| `DIVERGENCE_WARN_PCT` | Warnen, wenn der Kurs heute ≥ X% gegen die These läuft (nur mit Preis-Tracking); `0` = aus |
| `ENABLE_KELLY_SUGGESTION` | Kelly-lite Positionsanteil im Alert (braucht ausgewertete Ergebnisse) |
| `ENABLE_MARKET_SESSION_INFO` | US-Börsen-Session (offen/vor-/nachbörslich/zu) im Alert (Standard an) (#7) |
| `ENABLE_VOLATILITY_FLAG` | High-Volatility-Hinweis im Alert bei Zöllen/Sanktionen/Fed etc. (Standard an) (#6) |
| `ENABLE_CHART_BUTTONS` / `CHART_URL_TEMPLATE` | Inline-Chart-Buttons pro handelbarem Ticker (TradingView), `{ticker}` wird ersetzt (Standard an) (#9) |
| `ENABLE_CONVICTION_SCORE` | Überzeugungs-Score (0–100) + Positionsgrößen-Einordnung im Alert (Standard an, kein Extra-Call) |
| `ALERT_MIN_EXPECTED_MOVE_PCT` | Nur alarmieren, wenn Claudes grobe erwartete Bewegung ≥ diesem % ist (`0` = aus) |
| `ENABLE_HISTORICAL_HITRATE` | Historische Pro-Ticker-Trefferquote im Alert (braucht Preis-Tracking zum Befüllen) |
| `ENABLE_RISK_LEVELS` | Vorgeschlagene Stop-/Take-Profit-Marken aus der Tagesspanne (nur mit Preis-Tracking) |
| `ENABLE_HISTORICAL_PERFORMANCE_GATE` / `HISTORICAL_PERFORMANCE_MIN_SAMPLES` / `_WEIGHT` / `_SUPPRESS_BELOW` | Der staerkste handelbare Ticker "lernt" aus seiner EIGENEN historischen Trefferquote (braucht Preis-Tracking): hebt/senkt den Ueberzeugungs-Score, optional harte Unterdrueckung bei belegt schlechter Bilanz. Standard **aus** |
| `ENABLE_GAP_CHASE_EVALUATION` / `GAP_CHASE_WINDOW_MINUTES` / `_MIN_GAP_PCT` / `_TOO_LATE_RATIO` / `_TOO_LATE_ABS_PCT` | Gegenstueck zur Uebernacht-Gap-Antizipation: der Gap ist schon passiert (Markt war zu, jetzt zur Boersenoeffnung extrem hoch/niedrig) - lohnt sich ein Einstieg noch, und falls ja, wann verkaufen? Braucht Preis-Tracking. Standard **an** |
| `ENABLE_BORDERLINE_ESCALATION` / `CLAUDE_ESCALATION_MODEL` / `ESCALATION_BAND` | Grenzfälle nahe der Schwelle mit stärkerem Modell zweitprüfen (Standard **aus**, kostet Extra-Calls) (#4) |
| `ENABLE_PRICE_TRACKING` / `PRICE_OUTCOME_HORIZON_MINUTES` | Kurs-Feedback/Backtesting + heutige Bewegung im Alert, best-effort über Stooq. Im ausgelieferten Workflow/`.env.example` **an** (Code-Standard aus); Repo-Variable `ENABLE_PRICE_TRACKING=false` schaltet ab (#2/#3/#8) |
| `PRICE_CACHE_TTL_SECONDS` | Kurz-Cache für Live-Kursabfragen (gleiche Quote nicht doppelt holen); `0` = aus |
| `ENABLE_WEEKLY_DIGEST` / `WEEKLY_DIGEST_WEEKDAY` / `WEEKLY_DIGEST_MIN_HOUR` | Wöchentlicher Performance-Digest per Telegram (braucht ausgewertete Ergebnisse) |
| `MAX_CONCURRENT_CLASSIFICATIONS` | Wie viele Claude-Calls parallel laufen dürfen (Standard **1** = seriell, siehe Duplikat-Hinweis oben; höher = schneller bei Nachrichtenschüben, aber Risiko doppelter Alerts) |
| `DEDUP_SIMILARITY_THRESHOLD` | Ab welcher Textähnlichkeit (0-1) zwei Statements als Duplikat gelten (Standard 0.82) |
| `DEDUP_WINDOW_SECONDS` | Zeitfenster für die Text-Duplikatsuche, Tier 1 (Standard 24h) |
| `TOPIC_CONTEXT_WINDOW_HOURS` / `TOPIC_CONTEXT_MAX_ITEMS` | Wie viele Stunden zurück / wie viele Meldungen als Themen-Kontext an Claude mitgegeben werden, Tier 2 (Standard 24h / 20) |
| `ALERT_DIGEST_THRESHOLD` | Ab wie vielen gleichzeitigen Alerts zu einer Sammel-Nachricht gebündelt wird (Standard 3) |
| `CLAUDE_MODEL` | Modell fuer die Klassifikation (Standard `claude-haiku-4-5`, guenstig; `claude-sonnet-5` fuer potenziell bessere Qualitaet zu mehrfachen Kosten) |
| `ENABLE_PREFILTER` | Billiger Claude-freier Vorfilter: verwirft offensichtliche Nicht-Ereignisse (Listicles/Ratgeber/Personal-Finance-Clickbait) per Muster-Denylist, **bevor** ein Claude-Call ausgegeben wird - erste Trichterstufe, spart Calls ohne den Deckel anzuheben (Standard an, konservativ) - siehe Abschnitt oben |
| `MAX_CLASSIFICATIONS_PER_DAY` | Harter Kostendeckel: mehr Claude-Calls finden an einem Tag (UTC) nicht mehr statt (Standard 100 ≈ max. 0.15-0.25 €/Tag bei Haiku) - siehe Abschnitt oben |
| `PRIORITY_CLASSIFICATIONS_PER_DAY` | Extra-Reserve oberhalb von `MAX`, die nur wichtige Meldungen (direkte Original-Quellen-Posts, harte Wirtschaftsthemen) nutzen dürfen - absolutes Tages-Maximum = `MAX + PRIORITY` (Standard 30, also 130 gesamt; `0` deaktiviert die Reserve) - siehe Abschnitt oben |
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
| `GET /api/calibration` | Trefferquote gesamt / je Konfidenz-Bucket / je Richtung + Schwellen-Empfehlung (`recommendation`), nur mit Preis-Tracking befüllt | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/performance` | Aggregierte Performance + beste/schlechteste Ticker, Trefferquote je Quelle (`by_source`) und Kelly-lite Anteil (nur mit Preis-Tracking befüllt) | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/hourly-performance` | Trefferquote/Durchschnittsrendite je Alarm-**Stunde** (UTC) — zeigt Time-of-Day-Muster, nur mit Preis-Tracking befüllt | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/gate-stats?hours=` | Je Zustell-Gate (u.a. `vix_regime`, `ensemble_model`, `historical_performance`, `conviction_score`) wie oft geprüft/blockiert | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/model-performance` | Trefferquote je verwendetem Claude-Modell (Haiku vs. Sonnet-Eskalation) | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/pipeline-stats?hours=` | Durchschnitts-/Min-/Max-Dauer je Verarbeitungsphase eines Poll-Zyklus | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/gap-impact?threshold_pct=` | Trefferquote stark gegappter Alerts im Vergleich zu allen anderen | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/ensemble-status` | Trainingsstatus des Ensemble-Modells (trainiert? wie viele Samples?) | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/paper` | Paper-Depot-Status (Wert, P&L, offene/geschlossene Positionen, Win-Rate) | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/outcomes.csv` | Alle Ergebnis-Datensätze als CSV für die Offline-Analyse | `X-API-Key`, falls `DASHBOARD_API_KEY` gesetzt |
| `GET /api/health` | Status pro Quelle (inkl. `prefiltered`/`stale`), Konfigurationsfehler/-warnungen, Uptime, `classification_calls_today`/`_limit`, `alerts_sent_today`, Zyklus-Timing (`last_cycle_seconds`/`avg_cycle_seconds`) und `active_gates` | – (bewusst offen für Uptime-Checks) |
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

## Lizenz

MIT License, siehe [`LICENSE`](LICENSE). Kurz gesagt: der Code darf frei genutzt,
verändert und weiterverbreitet werden - **ohne jede Gewährleistung** ("as is"). Das
gilt zusätzlich zum Disclaimer oben: **keine Finanzberatung, keine Anlageempfehlung,
Nutzung auf eigenes Risiko.**
