import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    # Ein gesetzter, aber leerer/nur-Whitespace-Wert (z.B. eine CI-Variable, die zu
    # einem leeren String aufgeloest wird) soll wie "nicht gesetzt" behandelt werden -
    # sonst wuerde z.B. ENABLE_NEWS="" eine Quelle stillschweigend deaktivieren, obwohl
    # der Default eigentlich True waere.
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _str(name: str, default: str = "") -> str:
    # .strip() faengt versehentlich mitkopierte Zeilenumbrueche/Leerzeichen ab
    # (z.B. beim Einfuegen eines Tokens aus Telegram/der Console in GitHub Secrets) -
    # ohne das wuerden ungueltige Zeichen erst spaeter als kryptischer URL-Fehler auftauchen.
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    # Analog zu _bool: ein gesetzter, aber leerer/nur-Whitespace-Wert soll wie "nicht
    # gesetzt" behandelt werden, statt int("") mit einem ValueError den kompletten
    # Prozess schon beim Modul-Import abstuerzen zu lassen.
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val.strip())


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return float(val.strip())


def _strlist(name: str, upper: bool = False) -> list[str]:
    """Komma-separierte Liste aus einer Env-Variable (leere Elemente/Whitespace werden
    verworfen). Fuer Watchlists o.ae. Leere/ungesetzte Variable -> leere Liste."""
    raw = os.getenv(name, "")
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return [p.upper() for p in items] if upper else items


ANTHROPIC_API_KEY = _str("ANTHROPIC_API_KEY")
# Haiku statt Sonnet als Default: die Klassifikation ist eine strukturierte,
# schema-gefuehrte Aufgabe (Tool-Use mit festem JSON-Schema) - dafuer reicht Haiku in
# der Praxis gut aus, kostet aber nur einen Bruchteil pro Call. Bei Bedarf in .env auf
# z.B. "claude-sonnet-5" fuer potenziell bessere Einschaetzungsqualitaet umstellen.
CLAUDE_MODEL = _str("CLAUDE_MODEL", "claude-haiku-4-5")
CLAUDE_MAX_RETRIES = _int("CLAUDE_MAX_RETRIES", 3)
CLAUDE_TIMEOUT_SECONDS = _float("CLAUDE_TIMEOUT_SECONDS", 30)

TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")
# Kurze Nachricht beim Start schicken, damit sofort sichtbar ist ob Telegram korrekt verbunden ist
TELEGRAM_STARTUP_NOTICE = _bool("TELEGRAM_STARTUP_NOTICE", True)

POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)

# Billiger, Claude-FREIER Relevanz-Vorfilter (erste Trichter-Stufe, siehe
# app/prefilter.py): verwirft offensichtlich nicht marktbewegende Schlagzeilen
# (Listicles, Ratgeber, Personal-Finance-/Werbe-Clickbait), BEVOR ein teurer
# Claude-Call ausgegeben wird und einen Slot des Tages-Kostendeckels belegt. Seit der
# Verallgemeinerung (kein Personen-/Themenfilter mehr an den Quellen) kommt viel mehr
# Rohmaterial herein - der Vorfilter haelt die Kosten niedrig, ohne die Alarmqualitaet
# zu beruehren (er entscheidet nichts ueber Alerts, sondern nur, was ueberhaupt eine
# Claude-Analyse wert ist). Bewusst konservativ; auf false setzen, um jede Meldung
# direkt von Claude bewerten zu lassen (mehr Abdeckung, hoehere Kosten).
ENABLE_PREFILTER = _bool("ENABLE_PREFILTER", True)

ALERT_CONFIDENCE_THRESHOLD = _float("ALERT_CONFIDENCE_THRESHOLD", 0.5)
# Praezisions-Filter fuer die Telegram-Alerts: es wird NUR noch alarmiert, wenn die
# Meldung mindestens EINEN konkreten Boersenticker mit klarer Long/Short-Richtung UND
# einer Pro-Ticker-Konfidenz >= diesem Wert enthaelt. Zweck: der Nutzer will nur die
# sichersten, direkt handelbaren Signale ("eine bestimmte Aktie geht hoch/runter") und
# keine allgemeinen marktrelevanten Meldungen ohne konkrete, hochsichere Aktie. Hoeher
# = weniger, aber sicherere Nachrichten; niedriger = mehr Nachrichten. 0.90 = sehr
# streng (nur die absolut sichersten Signale, teils tagelang keine); 0.85 = streng;
# 0.80 = moderat. Auf 0 setzen, um wieder JEDE marktrelevante Meldung oberhalb von
# ALERT_CONFIDENCE_THRESHOLD zu alarmieren (altes Verhalten).
ALERT_MIN_TICKER_CONFIDENCE = _float("ALERT_MIN_TICKER_CONFIDENCE", 0.90)

# --- Persoenlicher Filter (Watchlist/Blocklist) ---
# Wenn WATCHLIST_TICKERS und/oder WATCHLIST_SECTORS gesetzt sind, wird nur noch
# alarmiert, wenn ein handelbarer Ticker in der Watchlist ist ODER ein betroffener
# Sektor auf WATCHLIST_SECTORS passt (Teilstring, case-insensitive). Leer = kein
# Filter (alles erlaubt). BLOCKLIST_TICKERS entfernt einzelne Ticker generell aus der
# Alarm-Bewertung (z.B. Werte, die du ohnehin nicht handelst).
WATCHLIST_TICKERS = _strlist("WATCHLIST_TICKERS", upper=True)
WATCHLIST_SECTORS = [s.lower() for s in _strlist("WATCHLIST_SECTORS")]
BLOCKLIST_TICKERS = _strlist("BLOCKLIST_TICKERS", upper=True)

# --- Handelbares Universum / Liquiditaets-Gate (#8) ---
# Optionale Positivliste tatsaechlich handelbarer, liquider Ticker (z.B. deine
# Broker-Watchlist oder die S&P-100). Ist sie gesetzt, loesen NUR Ticker aus dieser
# Liste einen Alert aus - obskure/illiquide Kuerzel (die Claude vereinzelt nennt) und
# Werte, die du ohnehin nicht handeln kannst, fallen so vor dem Alert heraus. Leer =
# kein Universum-Filter (jeder handelbare Ticker zaehlt, altes Verhalten).
TICKER_UNIVERSE = _strlist("TICKER_UNIVERSE", upper=True)

# --- Zustellungs-Gates (alle default AUS, damit sie das Verhalten nur auf Wunsch aendern) ---
# Stale-News-Filter (#14): Meldungen, deren Veroeffentlichung laenger als so viele
# Minuten zurueckliegt, werden gar nicht erst klassifiziert (alte Nachrichten sind meist
# eingepreist und kosten sonst nur einen Claude-Call). 0 = aus (jede Meldung, auch ohne
# verlaesslichen Zeitstempel, wird analysiert).
MAX_NEWS_AGE_MINUTES = _int("MAX_NEWS_AGE_MINUTES", 0)

# Ticker-Cooldown (#6): nach einem Alert fuer einen Ticker in eine Richtung wird derselbe
# Ticker+Richtung fuer so viele Minuten nicht erneut alarmiert - verhindert
# Mehrfach-Einstiege in dieselbe laufende Story. 0 = aus.
TICKER_ALERT_COOLDOWN_MINUTES = _int("TICKER_ALERT_COOLDOWN_MINUTES", 0)

# Ruhezeiten (#7): in diesem Stundenfenster (lokale Zeit QUIET_HOURS_TZ, Format
# 'START-ENDE', z.B. '23-7') werden nur noch Alerts mit sehr hoher Ueberzeugung
# (>= QUIET_HOURS_MIN_CONVICTION, 0-100) sofort zugestellt; schwaechere warten bis zum
# Fensterende (der Resend-Pfad schickt sie dann automatisch nach). Leer = aus.
QUIET_HOURS = _str("QUIET_HOURS")
QUIET_HOURS_TZ = _str("QUIET_HOURS_TZ", "Europe/Berlin")
QUIET_HOURS_MIN_CONVICTION = _int("QUIET_HOURS_MIN_CONVICTION", 90)

# Globales Ueberzeugungs-Gate: es wird NUR alarmiert, wenn der (Claude-freie)
# Ueberzeugungs-Score >= diesem Wert (0-100) ist. Ein einziger, geldorientierter Regler
# oberhalb der Ticker-Konfidenz-Schwelle - beruecksichtigt zusaetzlich Frische, Quellen-
# Bestaetigung und Hedge-/Volatilitaets-Abschlag. 0 = aus. Unterdrueckte Meldungen
# bleiben unmarkiert und werden vom Resend-Pfad erneut versucht (falls der Score spaeter
# z.B. durch eine bestaetigende zweite Quelle steigt).
ALERT_MIN_CONVICTION = _int("ALERT_MIN_CONVICTION", 0)

# Anti-Fatigue-Ratelimit: hoechstens so viele ALERT-Meldungen pro rollierender Stunde.
# Bei Ueberschreitung werden die ueberzaehligen (nach Ueberzeugung schwaecheren) Meldungen
# zurueckgestellt und vom Resend-Pfad spaeter erneut versucht, sobald das Fenster wieder
# Luft hat. 0 = aus (keine Begrenzung).
MAX_ALERTS_PER_HOUR = _int("MAX_ALERTS_PER_HOUR", 0)

# Sektor-Cluster-Hinweis (#4): tauchen innerhalb von SECTOR_CLUSTER_WINDOW_HOURS
# mindestens SECTOR_CLUSTER_MIN alarmierte Meldungen zu DEMSELBEN Sektor auf, markiert der
# Alert das als Cluster (mehrere Werte einer Branche bewegen sich = staerkeres Makro-
# Signal). Rein informativ. MIN <= 1 schaltet den Hinweis ab.
SECTOR_CLUSTER_MIN = _int("SECTOR_CLUSTER_MIN", 3)
SECTOR_CLUSTER_WINDOW_HOURS = _int("SECTOR_CLUSTER_WINDOW_HOURS", 6)

# Kurs-Divergenz-Warnung (#3): laeuft der Kurs am Alarm-Tag bereits um mehr als so viele
# Prozentpunkte GEGEN die eingeschaetzte Richtung (z.B. long, aber schon -X% heute),
# warnt der Alert. Nur mit ENABLE_PRICE_TRACKING (braucht die heutige Bewegung). 0 = aus.
DIVERGENCE_WARN_PCT = _float("DIVERGENCE_WARN_PCT", 2.0)

# "Zu spaet"-Warnung: laeuft der Kurs am Alarm-Tag bereits um mehr als so viele
# Prozentpunkte MIT der eingeschaetzten Richtung (z.B. long und heute schon +X%), warnt
# der Alert, dass die Bewegung moeglicherweise schon groesstenteils gelaufen ist ("er
# predigt erst, wenn es schon hardcore im Geschehen ist"). Macht sichtbar, wenn ein
# Signal spaet dran ist, statt es unkommentiert zu melden. Nur mit ENABLE_PRICE_TRACKING
# (braucht die heutige Bewegung). 0 = aus.
LATE_MOVE_WARN_PCT = _float("LATE_MOVE_WARN_PCT", 3.0)

# Kelly-lite Positionsanteil (#5): aus der historischen Trefferquote + mittlerem Gewinn/
# Verlust einen groben, ausdruecklich unverbindlichen Bankroll-Anteil (Half-Kelly,
# gedeckelt) ableiten und im Alert anzeigen. Braucht ausgewertete Ergebnisse
# (ENABLE_PRICE_TRACKING). Keine Anlageberatung.
ENABLE_KELLY_SUGGESTION = _bool("ENABLE_KELLY_SUGGESTION", True)

# --- Ueberzeugungs-Score / Anreicherung ---
# Verdichtete Ueberzeugungs-Zahl (0-100) aus Konfidenzen + Frische + Quellen-
# Korroboration + Hedge-/Volatilitaets-Abschlag im Alert anzeigen (#1), samt grober,
# unverbindlicher Positionsgroessen-Einordnung (#4). Reine Verdichtung vorhandener
# Signale, kein zusaetzlicher Claude-Call.
ENABLE_CONVICTION_SCORE = _bool("ENABLE_CONVICTION_SCORE", True)
# Erwartete Bewegung (#3): Claude im Schema um eine grobe Prozent-/Horizont-Schaetzung
# bitten und sie im Alert anzeigen. Optionaler Mindest-Erwartungswert als Alarm-Gate:
# nur alarmieren, wenn die geschaetzte Bewegung >= diesem Prozentwert ist (0 = aus).
ALERT_MIN_EXPECTED_MOVE_PCT = _float("ALERT_MIN_EXPECTED_MOVE_PCT", 0.0)
# Historische Pro-Ticker-Trefferquote im Alert anzeigen (#9), sofern schon ausgewertete
# Ergebnisse vorliegen (braucht ENABLE_PRICE_TRACKING, um befuellt zu werden).
ENABLE_HISTORICAL_HITRATE = _bool("ENABLE_HISTORICAL_HITRATE", True)
# Vorgeschlagene Stop-Loss-/Take-Profit-Marken aus der heutigen Tagesspanne (#5) -
# nur wirksam mit ENABLE_PRICE_TRACKING (braucht einen Live-Kurs).
ENABLE_RISK_LEVELS = _bool("ENABLE_RISK_LEVELS", True)

# Woechentlicher Performance-Digest per Telegram (#10): einmal pro Woche (am
# WEEKLY_DIGEST_WEEKDAY, 0=Montag .. 6=Sonntag, ab WEEKLY_DIGEST_MIN_HOUR UTC) eine
# Zusammenfassung der Trefferquote/besten/schlechtesten Calls. Braucht ausgewertete
# Ergebnisse (ENABLE_PRICE_TRACKING), sonst wird nichts verschickt.
ENABLE_WEEKLY_DIGEST = _bool("ENABLE_WEEKLY_DIGEST", True)
WEEKLY_DIGEST_WEEKDAY = _int("WEEKLY_DIGEST_WEEKDAY", 0)
WEEKLY_DIGEST_MIN_HOUR = _int("WEEKLY_DIGEST_MIN_HOUR", 8)

# Kurz-Cache fuer Live-Kursabfragen (#13): dieselbe Ticker-Quote wird innerhalb dieses
# Fensters nicht erneut vom Kursdienst geholt (spart HTTP-Calls, wenn derselbe Ticker in
# einem Zyklus mehrfach vorkommt). 0 = aus.
PRICE_CACHE_TTL_SECONDS = _int("PRICE_CACHE_TTL_SECONDS", 60)

# --- Alert-Anreicherung ---
# US-Boersen-Session (offen/vor-/nachboerslich/zu) im Alert anzeigen - hilft
# einzuschaetzen, ob ein Signal gerade ueberhaupt handelbar ist.
ENABLE_MARKET_SESSION_INFO = _bool("ENABLE_MARKET_SESSION_INFO", True)
# High-Volatility-Flag (Zoelle/Sanktionen/Krieg/Fed etc.): markiert Meldungen, bei denen
# die Schwankung oft groesser ist als die klare Richtung (ggf. Straddle statt Direktional).
ENABLE_VOLATILITY_FLAG = _bool("ENABLE_VOLATILITY_FLAG", True)
# Inline-Buttons mit Chart-Links (TradingView) pro handelbarem Ticker unter dem Alert.
ENABLE_CHART_BUTTONS = _bool("ENABLE_CHART_BUTTONS", True)
# Basis-URL fuer die Chart-Buttons; {ticker} wird ersetzt.
CHART_URL_TEMPLATE = _str("CHART_URL_TEMPLATE", "https://www.tradingview.com/chart/?symbol={ticker}")

# --- Zweitmeinung fuer Grenzfaelle (#4) ---
# Meldungen, deren staerkste Ticker-Konfidenz knapp um die Alarm-Schwelle liegt
# (+/- ESCALATION_BAND), werden zur Absicherung ein zweites Mal mit einem staerkeren
# Modell klassifiziert. Kostet nur fuer diese Grenzfaelle einen Extra-Call (zaehlt gegen
# das Tages-Limit). Standardmaessig AUS, da es zusaetzliche Kosten verursacht.
ENABLE_BORDERLINE_ESCALATION = _bool("ENABLE_BORDERLINE_ESCALATION", False)
CLAUDE_ESCALATION_MODEL = _str("CLAUDE_ESCALATION_MODEL", "claude-sonnet-5")
ESCALATION_BAND = _float("ESCALATION_BAND", 0.1)

# --- Preis-Feedback / Backtesting (#2/#3/#8) ---
# Nach jedem Alert den Kurs der handelbaren Ticker erfassen und nach einem Horizont
# erneut messen, um die echte Trefferquote zu ermitteln (Dashboard-Kalibrierung) und
# die heutige Bewegung im Alert anzuzeigen. Best-effort ueber eine kostenlose Quelle
# (Stooq), ohne API-Key. Standardmaessig AUS: haengt von ausgehender Netz-Erreichbarkeit
# ab und macht pro Alert zusaetzliche HTTP-Calls - erst einschalten, wenn gewuenscht.
ENABLE_PRICE_TRACKING = _bool("ENABLE_PRICE_TRACKING", False)
PRICE_OUTCOME_HORIZON_MINUTES = _int("PRICE_OUTCOME_HORIZON_MINUTES", 60)

# --- Paper-Trading (virtuelles Depot, KEIN echtes Geld / kein Broker) ---
# Wenn aktiv: bei jedem tatsaechlich verschickten Alert wird fuer die handelbaren Ticker
# eine VIRTUELLE Position eroeffnet (Einstiegskurs gemerkt), laufend zum aktuellen Kurs
# bewertet und per Telegram gemeldet, "auf wie viel es steht". Stop-Loss/Take-Profit
# schliessen die Position automatisch. Das Sizing bestimmt der Bot selbst aus dem
# Ueberzeugungs-Score (Anteil des Depotwerts, siehe app/paper_trading.py). Reine
# Simulation zum Mitverfolgen der Signalguete - ausdruecklich keine Anlageberatung und
# keine echte Orderausfuehrung. Braucht erreichbare Kursdaten (Stooq, best-effort, wie
# ENABLE_PRICE_TRACKING); ist der Kursdienst nicht erreichbar, entfaellt das Eroeffnen/
# Bewerten still. Standardmaessig AUS.
PAPER_TRADING = _bool("PAPER_TRADING", False)
# Virtuelles Startkapital in EUR (Basiswert des Depots).
PAPER_STARTING_CAPITAL = _float("PAPER_STARTING_CAPITAL", 500.0)
# Hoechstens so viele gleichzeitig offene virtuelle Positionen (verhindert, dass das
# Kapital in zu viele Kleinstpositionen zerfaellt).
PAPER_MAX_POSITIONS = _int("PAPER_MAX_POSITIONS", 8)
# Mindesteinsatz je Position in EUR - faellt der freie Barbestand darunter, wird keine
# neue Position mehr eroeffnet (kein sinnloser Dust-Trade).
PAPER_MIN_STAKE = _float("PAPER_MIN_STAKE", 10.0)
# Wie oft (Minuten) hoechstens ein Depot-Status ("auf wie viel steht alles") per
# Telegram geschickt wird, solange Positionen offen sind. Eroeffnungen und (Stop/Ziel-)
# Schliessungen werden IMMER sofort gemeldet, unabhaengig davon. 0 = bei jedem Zyklus.
PAPER_STATUS_INTERVAL_MINUTES = _int("PAPER_STATUS_INTERVAL_MINUTES", 30)
# Ab wie vielen gleichzeitig alarmwuerdigen Statements in EINEM Poll-Zyklus zu einer
# gebuendelten Sammel-Nachricht gewechselt wird statt einer Einzelnachricht pro Statement
# (verhindert eine Alert-Flut bei einem ploetzlichen Nachrichtenschub).
ALERT_DIGEST_THRESHOLD = _int("ALERT_DIGEST_THRESHOLD", 3)
# Statements, die GLEICHZEITIG (innerhalb derselben Semaphore-Runde) klassifiziert
# werden, sehen sich gegenseitig nicht im Themen-Kontext (recent_context waechst
# erst, NACHDEM eine Klassifikation fertig ist - siehe orchestrator.py:
# _classify_and_store). Bei Werten > 1 koennen zwei fast zeitgleiche Meldungen zum
# selben Thema (z.B. von zwei verschiedenen Nachrichtenquellen) beide unabhaengig
# als "neu" durchgehen und beide einen Alert ausloesen. Default bewusst auf 1
# (seriell) gesetzt, um dieses Duplikat-Risiko auszuschliessen - auf Kosten von
# etwas laengerer Verarbeitungszeit bei einem ploetzlichen Nachrichtenschub.
MAX_CONCURRENT_CLASSIFICATIONS = _int("MAX_CONCURRENT_CLASSIFICATIONS", 1)

# Harter Kostendeckel: mehr als so viele Claude-Klassifikations-Calls finden an einem
# Tag (UTC) nicht mehr statt, egal wie viele neue Statements eintreffen - schuetzt vor
# einem einzelnen Nachrichtenschub, der sonst unbegrenzt Kosten verursachen wuerde
# (jeder NEUE, noch nicht bekannte Statement-Text kostet einen Call, auch wenn er sich
# danach als Themen-Duplikat herausstellt - die Zweistufige-Duplikaterkennung spart
# also Alerts, aber nicht diesen Call selbst). Bei ~1000-1500 Input- und 200-400
# Output-Tokens pro Call kostet der Default von 100 Calls/Tag bei Haiku-4.5-Preisen
# (Stand: $1 / $5 pro 1 Mio. Token) grob geschaetzt max. ca. 0.15-0.25 EUR/Tag,
# unabhaengig vom tatsaechlichen Nachrichtenaufkommen. Persistiert in SQLite, gilt also
# auch ueber einzelne GitHub-Actions-Laeufe hinweg (siehe app/db.py:
# get_classification_calls_today/record_classification_call).
MAX_CLASSIFICATIONS_PER_DAY = _int("MAX_CLASSIFICATIONS_PER_DAY", 100)

# ZUSAETZLICHE Reserve oberhalb von MAX_CLASSIFICATIONS_PER_DAY, die AUSSCHLIESSLICH
# fuer als besonders wichtig eingestufte Meldungen (siehe orchestrator.py:
# is_high_priority - direkte Original-Quellen-Posts, harte Wirtschaftsthemen wie Zoelle/Sanktionen/
# Zinsen) verwendet werden darf. Zweck: an einem Tag mit einem Nachrichtenschub soll das
# normale Limit nicht dazu fuehren, dass eine WIRKLICH wichtige Meldung stillschweigend
# uebersprungen wird, nur weil vorher viel Unwichtiges den Zaehler gefuellt hat. Das
# absolute Tages-Maximum bleibt hart gedeckelt bei MAX + PRIORITY (Default 100 + 30 =
# 130) - die Kosten koennen also nicht davonlaufen. Auf 0 setzen, um die Reserve zu
# deaktivieren (dann gilt wieder ein einziges hartes Limit fuer alle).
PRIORITY_CLASSIFICATIONS_PER_DAY = _int("PRIORITY_CLASSIFICATIONS_PER_DAY", 30)

# Statements, deren Text zu >= diesem Wert (0-1, difflib-Aehnlichkeit) einem kuerzlich
# gesehenen Statement gleicht, gelten als Duplikat (z.B. dieselbe Meldung bei
# GDELT und RSS, oder von vielen Portalen wortgleich syndiziert) und werden nicht
# erneut klassifiziert/alarmiert. Dies ist die schnelle, reine Text-Ebene (Tier 1).
DEDUP_SIMILARITY_THRESHOLD = _float("DEDUP_SIMILARITY_THRESHOLD", 0.82)
DEDUP_WINDOW_SECONDS = _int("DEDUP_WINDOW_SECONDS", 86400)  # 24h ("heute")

# Themen-Ebene (Tier 2, semantisch via Claude): wie viele Stunden zurueck bereits
# alarmierte Statements als Kontext mitgegeben werden, damit Claude erkennen kann,
# ob eine neue Meldung im Kern zu einem heute schon gemeldeten Thema gehoert -
# und nur bei einer echten Eskalation trotzdem erneut alarmiert wird.
TOPIC_CONTEXT_WINDOW_HOURS = _int("TOPIC_CONTEXT_WINDOW_HOURS", 24)
TOPIC_CONTEXT_MAX_ITEMS = _int("TOPIC_CONTEXT_MAX_ITEMS", 20)

ENABLE_NEWS = _bool("ENABLE_NEWS", True)
ENABLE_TRUTH_SOCIAL = _bool("ENABLE_TRUTH_SOCIAL", True)
ENABLE_LIVE_AUDIO = _bool("ENABLE_LIVE_AUDIO", False)

# Welcher Truth-Social-Account beobachtet wird, falls ENABLE_TRUTH_SOCIAL aktiv ist -
# frei konfigurierbar, nicht hart auf eine Person festgelegt. Diese Quelle ist eine von
# mehreren (News-Feeds via ENABLE_NEWS decken allgemein marktrelevante Nachrichten ab,
# unabhaengig von einem einzelnen Account).
TRUTH_SOCIAL_HANDLE = _str("TRUTH_SOCIAL_HANDLE", "realDonaldTrump")
# Optional: eigenes Bearer-Token (z.B. aus einer eingeloggten Browser-Session),
# falls die oeffentlichen Endpunkte ohne Auth nicht mehr funktionieren.
TRUTH_SOCIAL_BEARER_TOKEN = _str("TRUTH_SOCIAL_BEARER_TOKEN")
# Fallback: falls der direkte (unauthentifizierte) API-Call fehlschlaegt, mit einem
# echten headless Chromium die Profilseite laden und die Netzwerk-Antworten der
# Seite selbst mitschneiden (robuster gegen Bot-Blocking als ein nackter HTTP-Call,
# aber deutlich teurer an CPU/RAM). Braucht "pip install playwright" + Chromium.
TRUTH_SOCIAL_BROWSER_FALLBACK = _bool("TRUTH_SOCIAL_BROWSER_FALLBACK", True)
# Mindestabstand zwischen zwei Browser-Fallback-Versuchen, damit ein dauerhaft
# blockierter direkter API-Call nicht bei jedem Poll-Zyklus einen vollen Chromium
# startet.
TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL = _int(
    "TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL", 300
)

LIVE_AUDIO_STREAM_URLS = [
    u.strip() for u in os.getenv("LIVE_AUDIO_STREAM_URLS", "").split(",") if u.strip()
]
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
# Laenge der rollierenden Audio-Haeppchen in Sekunden, die ffmpeg kontinuierlich aus
# dem Stream schneidet und die einzeln transkribiert werden. Kuerzer = niedrigere
# Latenz, aber mehr Transkriptions-Overhead pro Sekunde Audio; laenger = effizienter,
# aber Meldungen kommen entsprechend spaeter an.
LIVE_AUDIO_CHUNK_SECONDS = _int("LIVE_AUDIO_CHUNK_SECONDS", 20)
# Sprache fuer die Whisper-Transkription (ISO-639-1, z.B. "en", "de"). Leer =
# automatische Spracherkennung pro Haeppchen (etwas langsamer, aber sinnvoll, wenn die
# ueberwachten Streams nicht durchgehend in derselben Sprache sind).
LIVE_AUDIO_LANGUAGE = _str("LIVE_AUDIO_LANGUAGE", "en")

DASHBOARD_PORT = _int("DASHBOARD_PORT", 8000)
# Falls gesetzt, verlangen alle /api/*-Endpunkte einen passenden "X-API-Key"-Header.
# Ohne das waere z.B. /api/test (kostet einen echten Claude-Call + kann einen echten
# Telegram-Alert ausloesen) fuer JEDEN erreichbar, der die IP:Port kennt - insbesondere
# relevant, weil die README-Anleitung fuer die Oracle-Cloud-Variante explizit dazu
# anleitet, Port 8000 fuer 0.0.0.0/0 zu oeffnen.
DASHBOARD_API_KEY = _str("DASHBOARD_API_KEY")

# GitHub-Authentifizierung fuer repository_dispatch-Events (Speech-Detection Triggering).
# Wenn gesetzt und ENABLE_LIVE_AUDIO aktiv: bei neu erkannten Reden wird automatisch
# ein GitHub Actions Workflow via repository_dispatch ausgeloest. Der Token braucht
# "repo" Scope. Leer/ungesetzt = keine automatischen Workflow-Triggers (Live-Audio
# laeuft trotzdem, Statements landen aber nur in der DB/Telegram).
GITHUB_TOKEN = _str("GITHUB_TOKEN")
# GitHub-Repository im Format "owner/repo" (z.B. "CorentinWWW/Claudeprojekt1") -
# fuer repository_dispatch-Targets. Wird nur benoetigt, falls GITHUB_TOKEN gesetzt ist.
GITHUB_REPO = _str("GITHUB_REPO")

DB_PATH = os.getenv("DB_PATH", "trump_monitor.db")


def validate() -> tuple[list[str], list[str]]:
    """Prueft die Konfiguration. Gibt (fatal_errors, warnings) zurueck.

    Fatal-Errors bedeuten: die Klassifikations-Pipeline kann nicht laufen.
    Warnings bedeuten: das System laeuft, aber mit eingeschraenkter Funktion.
    """
    errors = []
    warnings = []

    if not ANTHROPIC_API_KEY:
        errors.append(
            "ANTHROPIC_API_KEY ist nicht gesetzt - Klassifikation kann nicht laufen. "
            "In .env eintragen (siehe .env.example)."
        )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        warnings.append(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nicht gesetzt - es werden keine "
            "Telegram-Alerts verschickt, Statements werden nur in der DB erfasst."
        )

    if not ENABLE_NEWS and not ENABLE_TRUTH_SOCIAL and not ENABLE_LIVE_AUDIO:
        errors.append("Alle Quellen sind deaktiviert (ENABLE_NEWS/TRUTH_SOCIAL/LIVE_AUDIO=false).")

    if ENABLE_LIVE_AUDIO and not LIVE_AUDIO_STREAM_URLS:
        warnings.append(
            "ENABLE_LIVE_AUDIO=true aber LIVE_AUDIO_STREAM_URLS ist leer - "
            "Live-Audio-Quelle liefert dadurch nie Ergebnisse."
        )

    if PAPER_TRADING and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        warnings.append(
            "PAPER_TRADING=true aber Telegram ist nicht konfiguriert - die virtuellen "
            "Positionen werden zwar in der DB gefuehrt, aber es gibt keine Depot-/Trade-"
            "Meldungen (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID setzen)."
        )

    if GITHUB_TOKEN and not GITHUB_REPO:
        warnings.append(
            "GITHUB_TOKEN ist gesetzt, aber GITHUB_REPO fehlt - "
            "repository_dispatch-Triggers zum Starten von Workflows werden nicht funktionieren."
        )
    if GITHUB_REPO and not GITHUB_TOKEN:
        warnings.append(
            "GITHUB_REPO ist gesetzt, aber GITHUB_TOKEN fehlt - "
            "repository_dispatch-Triggers zum Starten von Workflows werden nicht funktionieren."
        )

    if not DASHBOARD_API_KEY:
        warnings.append(
            "DASHBOARD_API_KEY ist nicht gesetzt - alle /api/*-Endpunkte (inkl. /api/test, "
            "das echte Claude-Calls + Telegram-Alerts ausloesen kann) sind ungeschuetzt "
            "erreichbar. Falls das Dashboard oeffentlich erreichbar ist (z.B. Oracle-Cloud-"
            "Anleitung mit offenem Port 8000), dringend einen Wert setzen."
        )

    # --- Validierung der optionalen Zustell-Gates / Anreicherungs-Knoepfe (#10) ---
    # Eine still ins Leere laufende Fehlkonfiguration ('warum kommen keine Alerts?') soll
    # als Warnung sichtbar werden, statt das Verhalten unbemerkt zu veraendern.
    if QUIET_HOURS:
        from app.scoring import parse_hour_window
        if parse_hour_window(QUIET_HOURS) is None:
            warnings.append(
                f"QUIET_HOURS='{QUIET_HOURS}' ist kein gueltiges Fenster (erwartet "
                "'START-ENDE' mit ganzen Stunden 0-23, z.B. '23-7') - Ruhezeiten sind "
                "damit unwirksam."
            )
    if QUIET_HOURS:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(QUIET_HOURS_TZ)
        except Exception:
            warnings.append(
                f"QUIET_HOURS_TZ='{QUIET_HOURS_TZ}' ist keine bekannte Zeitzone - "
                "die Ruhezeit wird ersatzweise in UTC ausgewertet."
            )
    if ALERT_MIN_CONVICTION > 100:
        warnings.append(
            f"ALERT_MIN_CONVICTION={ALERT_MIN_CONVICTION} liegt ueber 100 - kein Alert "
            "kann diese Schwelle je erreichen, es wuerde also NIE alarmiert."
        )
    if not (0 <= WEEKLY_DIGEST_WEEKDAY <= 6):
        warnings.append(
            f"WEEKLY_DIGEST_WEEKDAY={WEEKLY_DIGEST_WEEKDAY} liegt ausserhalb 0-6 "
            "(Montag=0..Sonntag=6) - der Wochen-Digest wird nie verschickt."
        )
    if MAX_ALERTS_PER_HOUR < 0 or MAX_NEWS_AGE_MINUTES < 0 or TICKER_ALERT_COOLDOWN_MINUTES < 0:
        warnings.append(
            "Ein Zustell-Gate (MAX_ALERTS_PER_HOUR / MAX_NEWS_AGE_MINUTES / "
            "TICKER_ALERT_COOLDOWN_MINUTES) ist negativ - negativ wird wie 'aus' (0) "
            "behandelt."
        )

    return errors, warnings
