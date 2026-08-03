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

# --- Uebernacht-/Vorboersen-Gap-Antizipation ---
# Sagt frueh, dass eine Aktie steigen/fallen duerfte, BEVOR ein Katalysator kurz vor/nach
# Boersenschluss ausgehypt wird und der Kurs VORBOERSLICH schon extrem gegappt ist. Kommt
# ein klar gerichteter Alert in einem Fenster, in dem der Markt ihn nicht mehr voll
# einpreisen kann (nachboerslich, ueber Nacht, uebers Wochenende oder kurz vor Schluss),
# weist der Alert auf ein wahrscheinliches Gap am naechsten Open hin - "jetzt rein, bevor
# es hochschiesst". Rein zeit-/richtungsbasiert (kein Extra-Call). Standardmaessig AN.
ENABLE_GAP_PREDICTION = _bool("ENABLE_GAP_PREDICTION", True)
# Ab wie vielen Minuten VOR Boersenschluss ein Alert in der laufenden Session schon als
# Gap-Kandidat gilt (kaum noch Zeit, heute einzupreisen).
GAP_NEAR_CLOSE_MINUTES = _int("GAP_NEAR_CLOSE_MINUTES", 45)
# Optionaler Mindest-Erwartungswert (%, aus Claudes Schaetzung), damit Mini-Katalysatoren
# keinen Gap-Hinweis ausloesen. 0 = aus (jeder klar gerichtete Alert im Fenster zaehlt).
GAP_MIN_EXPECTED_MOVE_PCT = _float("GAP_MIN_EXPECTED_MOVE_PCT", 0.0)

# --- Gap-Chase-Bewertung (Gegenstueck: der Gap ist schon passiert) ---
# Nutzerwunsch: wurde ein Ticker bereits UEBER NACHT/VORBOERSLICH stark gepusht (Markt
# war zu) und steht zur Boersenoeffnung entsprechend extrem hoch/niedrig - lohnt sich ein
# Einstieg dann ueberhaupt noch, und falls ja, wann sollte man wieder verkaufen? Prueft
# kurz NACH der Eroeffnung (siehe GAP_CHASE_WINDOW_MINUTES) den tatsaechlichen Gap
# (heutiger Eroeffnungskurs vs. gestriger Schluss, aus der Kurshistorie) gegen Claudes
# erwartete Bewegung: ist der Groteil davon schon gelaufen, wird vom Nachkaufen
# abgeraten (Gap-Fade-Risiko); ist noch Luft, wird ein grobes Ausstiegs-Kursziel
# genannt. Braucht ENABLE_PRICE_TRACKING (Kurs + Historie). Standardmaessig AN wie die
# uebrigen reinen Anreicherungs-Funktionen (ENABLE_HISTORICAL_HITRATE/ENABLE_RISK_LEVELS)
# - aendert nie, OB alarmiert wird, nur was im Alert dazu steht.
ENABLE_GAP_CHASE_EVALUATION = _bool("ENABLE_GAP_CHASE_EVALUATION", True)
# Nur innerhalb so vieler Minuten NACH Boersenoeffnung relevant - danach ist "der Markt
# war zu" nicht mehr die Erklaerung fuer eine grosse Kursbewegung.
GAP_CHASE_WINDOW_MINUTES = _int("GAP_CHASE_WINDOW_MINUTES", 30)
# Ab welcher (richtungsbereinigten) Gap-Groesse (%) die Bewertung ueberhaupt erst
# auftaucht - ein normaler kleiner Sprung zur Eroeffnung ist kein "extrem hoch/tief".
GAP_CHASE_MIN_GAP_PCT = _float("GAP_CHASE_MIN_GAP_PCT", 3.0)
# Liegt Claudes Erwartungswert vor: "zu spaet", wenn der Gap bereits >= diesem Anteil
# (0-1) der erwarteten Gesamtbewegung ausgemacht hat.
GAP_CHASE_TOO_LATE_RATIO = _float("GAP_CHASE_TOO_LATE_RATIO", 0.8)
# Ohne Erwartungswert (Claude liefert nicht immer eine Schaetzung): grobe Ersatzschwelle
# in % - ab dieser Gap-Groesse allein gilt es als "zu spaet", unabhaengig vom Kontext.
GAP_CHASE_TOO_LATE_ABS_PCT = _float("GAP_CHASE_TOO_LATE_ABS_PCT", 6.0)

# Nutzerwunsch: EINE klare, verdichtete Kauf-/Verkaufsempfehlung im Alert (statt die
# einzelnen Signale - Score, Technik, Gap-Timing, Geruecht-Warnung - selbst zusammen-
# reimen zu muessen), siehe scoring.trade_recommendation(). Rein additiv aus bereits
# vorhandenen Signalen, kein zusaetzlicher Claude-Call. Standardmaessig AN wie die
# uebrigen reinen Anreicherungs-Funktionen. KEINE Anlageberatung.
ENABLE_TRADE_RECOMMENDATION = _bool("ENABLE_TRADE_RECOMMENDATION", True)

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

# --- Historische-Performance-Feedback (Ticker "lernt" aus eigenen vergangenen Alerts) ---
# ENABLE_HISTORICAL_HITRATE (oben) zeigt die historische Pro-Ticker-Trefferquote nur an -
# sie fliesst NICHT in die Alarm-Entscheidung ein, ein Ticker mit belegt schlechter Bilanz
# wird also genauso behandelt wie einer mit durchweg guter. Dieses Gate schliesst die
# Luecke: der staerkste handelbare Ticker eines Alerts wird anhand SEINER eigenen
# historischen Trefferquote (aus den ausgewerteten alert_outcomes) im Ueberzeugungs-Score
# hoch-/heruntergestuft - dieselbe Meldung wirkt also je nachdem, ob der Bot bei GENAU
# diesem Ticker bisher meist richtig oder meist falsch lag. Braucht ENABLE_PRICE_TRACKING,
# um ueberhaupt Daten zu haben (sonst keine Wirkung). Standardmaessig AUS wie alle
# verhaltensaendernden Zustell-Gates.
ENABLE_HISTORICAL_PERFORMANCE_GATE = _bool("ENABLE_HISTORICAL_PERFORMANCE_GATE", False)
# Mindestanzahl ausgewerteter Alerts fuer GENAU diesen Ticker, bevor seine Trefferquote als
# belastbar genug gilt, um den Score zu beeinflussen - verhindert, dass 1-2 Zufallstreffer/
# -verluste den Score verzerren.
HISTORICAL_PERFORMANCE_MIN_SAMPLES = _int("HISTORICAL_PERFORMANCE_MIN_SAMPLES", 5)
# Maximaler Zu-/Abschlag (Punkte, 0-100) bei 100%/0% historischer Trefferquote; linear
# skaliert um die 50%-Coinflip-Marke (50% Trefferquote wirkt neutral). 0 = kein Effekt.
HISTORICAL_PERFORMANCE_WEIGHT = _int("HISTORICAL_PERFORMANCE_WEIGHT", 15)
# Optionales hartes Gate: faellt die historische Trefferquote eines Tickers (bei
# ausreichend Samples, siehe MIN_SAMPLES) unter diesen Wert (0-1), wird der Alert
# unterdrueckt statt nur den Score zu senken - "bei diesem Ticker hat es bisher meistens
# nicht gestimmt, hier aufhoeren". 0 = aus (nur der Score-Effekt oben wirkt).
HISTORICAL_PERFORMANCE_SUPPRESS_BELOW = _float("HISTORICAL_PERFORMANCE_SUPPRESS_BELOW", 0.0)

# Woechentlicher Performance-Digest per Telegram (#10): einmal pro Woche (am
# WEEKLY_DIGEST_WEEKDAY, 0=Montag .. 6=Sonntag, ab WEEKLY_DIGEST_MIN_HOUR UTC) eine
# Zusammenfassung der Trefferquote/besten/schlechtesten Calls. Braucht ausgewertete
# Ergebnisse (ENABLE_PRICE_TRACKING), sonst wird nichts verschickt.
ENABLE_WEEKLY_DIGEST = _bool("ENABLE_WEEKLY_DIGEST", True)
WEEKLY_DIGEST_WEEKDAY = _int("WEEKLY_DIGEST_WEEKDAY", 0)
WEEKLY_DIGEST_MIN_HOUR = _int("WEEKLY_DIGEST_MIN_HOUR", 8)

# Taegliches Lebenszeichen des Servers, angehaengt an das MORGENDLICHE Depot-Update
# (08:00 UTC): Laufzeit, Alter des letzten Poll-Zyklus, Status je Quelle und die
# heutigen Analyse-/Alert-Zahlen. Macht STILLE von "nichts passiert" unterscheidbar -
# ohne dieses Signal sieht ein abgestuerzter Bot genauso aus wie ein Tag ohne
# marktrelevante Nachrichten, ein Ausfall bliebe womoeglich tagelang unbemerkt.
ENABLE_DAILY_LIVE_SIGNAL = _bool("ENABLE_DAILY_LIVE_SIGNAL", True)

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

# --- Technische Analyse (TradingView-Stil, siehe app/indicators.py) ---
# Wenn aktiv: fuer die handelbaren Ticker eines Alerts werden aus den historischen
# Tageskursen (Stooq, best-effort) gaengige technische Indikatoren berechnet und zu einer
# Gesamtbewertung (Strong Buy..Strong Sell) verdichtet. Diese wird im Alert angezeigt und
# darauf geprueft, ob sie die von Claude eingeschaetzte Long/Short-Richtung BESTAETIGT
# oder ihr WIDERSPRICHT - eine technische Zweitmeinung zusaetzlich zum Nachrichtensignal.
# Best-effort (Historie nicht erreichbar -> Feature entfaellt still). Standardmaessig AUS.
ENABLE_TECHNICALS = _bool("ENABLE_TECHNICALS", False)
# Optionales, konservatives Alarm-Gate: nur wirksam, wenn ENABLE_TECHNICALS aktiv ist.
# Ist es an, wird ein Alert unterdrueckt, wenn die technische Gesamtbewertung dem Signal
# KLAR widerspricht (Long trotz 'Strong Sell' bzw. Short trotz 'Strong Buy'). Standard
# AUS - die Technik reichert den Alert dann nur an, unterdrueckt aber nichts.
TECHNICALS_REQUIRE_AGREEMENT = _bool("TECHNICALS_REQUIRE_AGREEMENT", False)
# Um wie viele Punkte (0-100) der Ueberzeugungs-Score angehoben/gesenkt wird, wenn die
# Technik das Signal bestaetigt/widerlegt. 0 = Score unveraendert lassen (nur Anzeige).
TECHNICALS_CONVICTION_WEIGHT = _int("TECHNICALS_CONVICTION_WEIGHT", 10)
# Wie lange (Sekunden) die Kurs-Historie eines Tickers zwischengespeichert wird - Tages-
# bars aendern sich innerhalb eines Tages kaum, das spart wiederholte Downloads.
HISTORY_CACHE_TTL_SECONDS = _int("HISTORY_CACHE_TTL_SECONDS", 900)

# --- Ensemble-Modell: klassische, von Claude UNABHAENGIGE Zweitmeinung ---
# Ein Bag-of-Words-Naive-Bayes-Modell (siehe app/ensemble.py), das aus der EIGENEN
# bisherigen Erfolgsbilanz (ausgewertete alert_outcomes) lernt, ob Meldungen mit
# AEHNLICHEM Wortschatz frueher eher zu einem Treffer oder Fehlschlag gefuehrt haben,
# und daraus je Alert eine geschaetzte Trefferwahrscheinlichkeit liefert - eine echte,
# von Claude unabhaengige Zweitmeinung (kein zweiter Claude-Call, kein externes
# ML-Framework). Braucht ENABLE_PRICE_TRACKING (sonst gibt es keine ausgewerteten
# Ergebnisse zum Trainieren) und mindestens ENSEMBLE_MIN_TRAINING_SAMPLES ausgewertete
# Alerts BEIDER Klassen (Treffer UND Fehlschlag) - vorher bleibt das Modell inaktiv
# (kein Effekt, kein Fehler). Standardmaessig AUS wie alle verhaltensaendernden
# Zustell-Gates.
ENABLE_ENSEMBLE_MODEL = _bool("ENABLE_ENSEMBLE_MODEL", False)
# Mindestanzahl ausgewerteter Alerts (insgesamt, beide Klassen), bevor das Modell als
# belastbar genug gilt - verhindert, dass ein paar Zufallstreffer/-verluste ueber
# Wortschatz-Zufaelle entscheiden.
ENSEMBLE_MIN_TRAINING_SAMPLES = _int("ENSEMBLE_MIN_TRAINING_SAMPLES", 30)
# Wie oft (Sekunden) das Modell hoechstens neu trainiert wird - Training ist billig
# (reine Wortzaehlung), aber unnoetig bei jedem einzelnen Statement desselben Zyklus.
ENSEMBLE_RETRAIN_SECONDS = _int("ENSEMBLE_RETRAIN_SECONDS", 900)
# Optionales hartes Gate: liegt die vom Ensemble-Modell geschaetzte
# Trefferwahrscheinlichkeit unter diesem Wert (0-1), wird der Alert unterdrueckt statt
# nur den Score zu senken - "die eigene Wort-Statistik spricht klar dagegen". 0 = aus
# (nur der Score-Effekt unten wirkt).
ENSEMBLE_SUPPRESS_BELOW = _float("ENSEMBLE_SUPPRESS_BELOW", 0.0)
# Maximaler Zu-/Abschlag (Punkte, 0-100) bei 100%/0% geschaetzter Trefferwahrschein-
# lichkeit; linear skaliert um die 50%-Coinflip-Marke (50% wirkt neutral). 0 = kein
# Effekt auf den Score (Modell wirkt dann nur ueber ENSEMBLE_SUPPRESS_BELOW).
ENSEMBLE_CONVICTION_WEIGHT = _int("ENSEMBLE_CONVICTION_WEIGHT", 10)

# --- VIX-Marktregime-Gate ---
# Der VIX (CBOE Volatility Index) misst die implizite Volatilitaet des S&P 500 - ein
# grober marktweiter "Angst-Indikator", unabhaengig vom einzelnen Katalysator. Ist er
# sehr hoch (Panik-/Crash-Modus), bewegt sich oft der GESAMTE Markt chaotisch - ein
# einzelnes direktionales Signal ("Aktie X geht hoch") ist dann unzuverlaessiger, weil
# Korrelationen ueber Sektoren hinweg steigen und Kursbewegungen eher vom Gesamtmarkt als
# vom konkreten Katalysator getrieben werden. Dieses Gate daempft/unterdrueckt Alerts bei
# hohem VIX. Best-effort ueber Stooq (^vix, kostenlos, kein Key), wie das uebrige
# Preis-Tracking - ist der Kursdienst nicht erreichbar, entfaellt das Gate still (kein
# Effekt, kein Fehler). Standardmaessig AUS wie alle verhaltensaendernden Zustell-Gates.
ENABLE_VIX_GATE = _bool("ENABLE_VIX_GATE", False)
# Ab diesem VIX-Schlusskurs gilt der Markt als "hohe Angst" - historisch deuten Werte
# > 30 auf ernsthaften Marktstress hin (grobe Faustregel, keine wissenschaftliche
# Konstante). Ab hier greift der Ueberzeugungs-Score-Abschlag (VIX_CONVICTION_PENALTY).
VIX_HIGH_THRESHOLD = _float("VIX_HIGH_THRESHOLD", 30.0)
# Optionales hartes Gate: liegt der VIX >= diesem Wert, wird der Alert unterdrueckt statt
# nur den Score zu senken - "der Gesamtmarkt ist gerade zu chaotisch fuer ein einzelnes
# direktionales Signal". 0 = aus (nur der Score-Abschlag unten wirkt).
VIX_SUPPRESS_ABOVE = _float("VIX_SUPPRESS_ABOVE", 0.0)
# Punkte-Abschlag (0-100) auf den Ueberzeugungs-Score, wenn der VIX >= VIX_HIGH_THRESHOLD
# liegt. 0 = kein Score-Effekt (Gate wirkt dann nur ueber VIX_SUPPRESS_ABOVE).
VIX_CONVICTION_PENALTY = _int("VIX_CONVICTION_PENALTY", 10)

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
# Hoechstens so viele gleichzeitig offene virtuelle Positionen. Nutzerwunsch: fuer JEDEN
# tatsaechlich verschickten Alert soll eine Position eroeffnet werden, nicht nur fuer die
# ersten paar - der praktische Deckel gegen zu viele Kleinstpositionen ist ohnehin schon
# der freie Barbestand (siehe compute_position/PAPER_MIN_STAKE: ohne freies Kapital wird
# kein neuer Trade mehr eroeffnet, unabhaengig von diesem Wert). Dieser Wert ist daher nur
# noch ein hohes Sicherheitsnetz, kein praktisches Limit mehr.
PAPER_MAX_POSITIONS = _int("PAPER_MAX_POSITIONS", 100)
# Mindesteinsatz je Position in EUR - faellt der freie Barbestand darunter, wird keine
# neue Position mehr eroeffnet (kein sinnloser Dust-Trade).
PAPER_MIN_STAKE = _float("PAPER_MIN_STAKE", 10.0)

# --- Harte Risikogrenzen (Kapitalschutz) ------------------------------------------
# Zwei Bremsen, die es vorher NICHT gab. Beide greifen VOR dem Eroeffnen und stoppen
# nur NEUE Positionen - laufende Positionen behalten ihre Stop-/Ziel-Marken, werden
# also nie mittendrin zwangsliquidiert.
#
# 1) Maximaler Drawdown vom bisherigen Hoechststand (nicht vom Startkapital!): faellt
#    der Depotwert um mehr als diesen Prozentsatz unter seinen eigenen Hoechststand,
#    werden keine neuen Positionen mehr eroeffnet. Klassische Risk-off-Reissleine gegen
#    die Abwaertsspirale "Verlust -> aggressiver nachlegen -> groesserer Verlust".
#    Erholt sich das Depot wieder ueber die Schwelle, laeuft es automatisch weiter.
#    0 = aus.
PAPER_MAX_DRAWDOWN_PCT = _float("PAPER_MAX_DRAWDOWN_PCT", 20.0)
# 2) Maximaler Anteil des Depotwerts, der GLEICHZEITIG in offenen Positionen gebunden
#    sein darf. Ohne diese Grenze laeuft das Depot voll, bis der freie Barbestand unter
#    PAPER_MIN_STAKE faellt - also faktisch ~100% investiert, ohne Puffer fuer eine
#    besonders gute spaetere Gelegenheit und mit voller Marktexposition an einem
#    schlechten Tag. 0 = aus (altes Verhalten).
PAPER_MAX_TOTAL_EXPOSURE_PCT = _float("PAPER_MAX_TOTAL_EXPOSURE_PCT", 60.0)
# Wie oft (Minuten) hoechstens ein Depot-Status ("auf wie viel steht alles") per
# Telegram geschickt wird, solange Positionen offen sind. Eroeffnungen und (Stop/Ziel-)
# Schliessungen werden IMMER sofort gemeldet, unabhaengig davon - dieser periodische
# Status ist reines Zusatz-Update oben drauf. 0 = aus (Standard: der zweimal taegliche
# Depot-Digest um 08/20 Uhr UTC, siehe ENABLE_DAILY_LIVE_SIGNAL, deckt die Uebersicht
# schon ab; Nutzerfund: alle 30 Min bei jeder offenen Position war als staendige
# Zusatzmeldung empfunden zu aufdringlich).
PAPER_STATUS_INTERVAL_MINUTES = _int("PAPER_STATUS_INTERVAL_MINUTES", 0)

# --- Maximale Haltedauer (Kapital-Recycling) ---
# Ohne Zeit-Exit bleibt eine Position, die weder Stop noch Ziel erreicht, UNBEGRENZT
# offen und bindet ihren Einsatz dauerhaft. Da der freie Barbestand (nicht
# PAPER_MAX_POSITIONS) der eigentlich bindende Faktor ist, waeren nach den ersten
# Alerts saemtliche Mittel gebunden und JEDER weitere Alert bekaeme keine Position mehr -
# genau das Gegenteil des Nutzerwunsches "bei allen Alerts eine Position eroeffnen".
# Nach so vielen Stunden wird eine Position daher zum aktuellen Kurs glattgestellt
# (close_reason 'timeout'). Sinnvoll am Signal-Horizont orientiert: die Erfolgsmessung
# wertet ohnehin nach PRICE_OUTCOME_HORIZON_MINUTES aus - eine Position tagelang zu
# halten testet etwas anderes als das, was das Signal behauptet. 0 = aus (unbegrenzt).
PAPER_MAX_HOLDING_HOURS = _float("PAPER_MAX_HOLDING_HOURS", 24.0)

# --- Trailing-Stop ("Gewinner laufen lassen") ---
# Ohne Trailing-Stop ist der Gewinn je Trade hart bei ~1.5R gedeckelt (siehe
# prices.suggest_risk_levels: Stop 1 Tagesspanne, Ziel 1.5), waehrend der Verlust 1R
# betraegt. Break-even braucht damit rechnerisch 40% Trefferquote - exakt der Wert, den
# ein reiner Zufallskurs liefert (1/(1+1.5)). Der Erwartungswert haengt also vollstaendig
# davon ab, dass das Nachrichtensignal echte Vorhersagekraft hat, und Spread/Slippage
# fressen den Rest. Ein nachziehender Stop dreht dieses Verhaeltnis: einzelne, stark
# laufende Nachrichten-Bewegungen duerfen weit ueber 1.5R hinauslaufen, was den mittleren
# Gewinn hebt, ohne den Verlust je Trade zu vergroessern. Standard AN.
PAPER_TRAILING_STOP = _bool("PAPER_TRAILING_STOP", True)
# Ab wie viel Gewinn (in Vielfachen des Anfangsrisikos R) der Trailing-Stop aktiv wird.
# Bis dahin gilt der urspruengliche Stop unveraendert. 1.0 = sobald der Trade so weit im
# Plus liegt, wie sein Stop entfernt ist.
PAPER_TRAIL_ACTIVATE_R = _float("PAPER_TRAIL_ACTIVATE_R", 1.0)
# Wie weit (in R) der nachgezogene Stop hinter dem bisherigen Hochpunkt (bei Short:
# Tiefpunkt) der Position bleibt. Kleiner = sichert mehr, wird aber frueher ausgestoppt.
PAPER_TRAIL_DISTANCE_R = _float("PAPER_TRAIL_DISTANCE_R", 1.0)

# --- Handelskosten-Modell (Realismus der Paper-Ergebnisse) ---
# Das virtuelle Depot rechnet Ein- und Ausstieg bisher zum selben Mittelkurs, ohne
# Spread/Gebuehren - die ausgewiesene Rendite ist damit systematisch zu optimistisch,
# gerade bei vielen kleinen Trades. Kosten je Seite in Basispunkten (10 bps = 0.1%) auf
# den Positionswert. Nicht "profitabler", aber EHRLICHER: eine Strategie, die nur ohne
# Kosten funktioniert, sollte man nicht mit echtem Geld nachbauen. Code-Standard 0
# (haelt die Unit-Tests exakt/deterministisch); in .env.example und im Workflow ist ein
# realistischer Wert gesetzt.
PAPER_COST_BPS = _float("PAPER_COST_BPS", 0.0)

# --- Kapitalerhalt-Modus (dynamisches Paper-Sizing nach Verlustserie) ---
# Nach mehreren aufeinanderfolgenden Verlust-Trades das Positions-Sizing automatisch
# verkleinern (Risk-off), statt nach einer Pechstraehne unveraendert weiterzumachen -
# senkt das Tempo, mit dem eine schlechte Serie das Depot weiter verkleinert, bis sich
# die Bilanz wieder dreht. Rein sizing-seitig (siehe app/paper_trading.py:
# position_fraction) - unterdrueckt keine Alerts und aendert nichts an Stop/Ziel.
# Standardmaessig AN, da rein risikoREDUZIEREND (im Gegensatz zu den verhaltens-
# aendernden Zustell-Gates oben, die standardmaessig AUS sind).
PAPER_CAPITAL_PRESERVATION = _bool("PAPER_CAPITAL_PRESERVATION", True)
# Ab so vielen geschlossenen Verlust-Trades IN FOLGE (vom juengsten rueckwaerts gezaehlt)
# greift die Reduktion.
PAPER_LOSS_STREAK_THRESHOLD = _int("PAPER_LOSS_STREAK_THRESHOLD", 3)
# Faktor, mit dem der normale Positionsanteil bei aktiver Verlustserie multipliziert
# wird (0.5 = Positionen halbiert). Muss in (0, 1] liegen, um tatsaechlich zu reduzieren.
PAPER_LOSS_STREAK_SIZE_FACTOR = _float("PAPER_LOSS_STREAK_SIZE_FACTOR", 0.5)

# Ab wie vielen gleichzeitig alarmwuerdigen Statements in EINEM Poll-Zyklus zu einer
# gebuendelten Sammel-Nachricht gewechselt wird statt einer Einzelnachricht pro Statement
# (verhindert eine Alert-Flut bei einem ploetzlichen Nachrichtenschub).
ALERT_DIGEST_THRESHOLD = _int("ALERT_DIGEST_THRESHOLD", 3)
# Statements, die GLEICHZEITIG (innerhalb derselben Semaphore-Runde) klassifiziert
# werden, sehen sich gegenseitig nicht im Themen-Kontext (recent_context waechst
# erst, NACHDEM eine Klassifikation fertig ist - siehe orchestrator.py:
# _classify_and_store). Bei Werten > 1 koennen zwei fast zeitgleiche Meldungen zum
# selben Thema (z.B. von zwei verschiedenen Nachrichtenquellen) beide unabhaengig
# als "neu" durchgehen und beide einen Alert ausloesen. Code-Default bewusst auf 1
# (seriell) gesetzt, um dieses Duplikat-Risiko fuer neue/lokale Setups komplett
# auszuschliessen. Latenz-Hinweis: die serielle Klassifikation ist typischerweise
# die dominante Zeitquelle eines Poll-Zyklus (mehrere Sekunden je Meldung) - im
# GitHub-Actions-Workflow ist dieser Wert daher auf Nutzerwunsch auf 2 angehoben
# (siehe monitor.yml), was die Klassifikations-Phase spuerbar verkuerzt und das
# Duplikat-Risiko nur geringfuegig erhoeht (die Themen-Duplikaterkennung via
# Claude-Kontext bleibt fuer alle NICHT gleichzeitig laufenden Meldungen wirksam).
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

# --- FED-Live-Audio (Nutzerwunsch: "wenn die FED spricht, besonders gut aufpassen") ---
# Bewusst NUR die FED, nicht Trump/andere Livestreams: FOMC-Sitzungstermine + Presse-
# konferenz-Uhrzeiten stehen Monate im Voraus fest (siehe FED_MEETING_WINDOWS), der Bot
# hoert also nur in einem klar begrenzten Zeitfenster zu statt dauerhaft. Frueher gab es
# eine aehnliche Live-Audio-Quelle fuer beliebige Livestreams, die wegen einer echten
# Sicherheitsluecke komplett entfernt wurde: der transkribierte Text wurde direkt in ein
# GitHub-Actions-Skript interpoliert (Script-Injection, CWE-94). Diese Quelle laeuft
# stattdessen wie jede andere Source in-process im Dauerbetrieb - der transkribierte Text
# durchlaeuft exakt dieselbe Klassifikations-Pipeline wie ein RSS-Artikel, es gibt keine
# Shell-/Workflow-Interpolation an keiner Stelle.
ENABLE_FED_AUDIO = _bool("ENABLE_FED_AUDIO", False)
# URL des Live-Audio-/Video-Streams (z.B. der offizielle YouTube-Kanal der Federal
# Reserve waehrend einer Pressekonferenz). Wird pro Poll-Zyklus per yt-dlp neu aufgeloest
# (die zugrundeliegende Manifest-URL aendert sich/verfaellt) - kein Caching noetig.
FED_AUDIO_STREAM_URL = _str("FED_AUDIO_STREAM_URL")
# Wann zugehoert wird: Komma-Liste aus "ISO-Startzeit(UTC)/Dauer-in-Minuten", z.B.
# "2026-09-16T18:30/90,2026-11-04T19:00/90". BEWUSST nicht automatisch vom Fed-Kalender
# abgerufen (keine stabile oeffentliche API dafuer) und bewusst NICHT mit Terminen
# vorbefuellt - der Bot soll keine geratenen/veralteten Zukunftsdaten verwenden. Aktuelle
# Termine: federalreserve.gov/monetarypolicy/fomccalendars.htm (Pressekonferenz-Beginn
# ist dort angegeben, i.d.R. 30min nach Statement-Veroeffentlichung).
FED_MEETING_WINDOWS = _str("FED_MEETING_WINDOWS")
# Kleinstes/schnellstes Whisper-Modell - bewusst nicht "base"/"small", um RAM/CPU auf der
# 1GB-Referenz-VM nicht zu sprengen (siehe README Oracle-Cloud-Anleitung). Wird bei
# ENABLE_FED_AUDIO=true beim ersten Fenster einmalig geladen (~75MB Download) und danach
# im Prozess wiederverwendet.
FED_AUDIO_WHISPER_MODEL = _str("FED_AUDIO_WHISPER_MODEL", "tiny")
# Wie viele Sekunden Live-Audio pro Poll-Zyklus mitgeschnitten werden - bewusst knapp
# unter POLL_INTERVAL_SECONDS, damit aufeinanderfolgende Mitschnitte sich nicht
# ueberlappen, aber auch keine grosse Luecke zwischen ihnen entsteht.
FED_AUDIO_CHUNK_SECONDS = _int("FED_AUDIO_CHUNK_SECONDS", 50)
# Sicherheitsdeckel: selbst ein falsch konfiguriertes (zu langes) Zeitfenster kann nicht
# laenger als das hier zuhoeren - verhindert dauerhafte Ressourcenlast durch einen
# Tippfehler in FED_MEETING_WINDOWS.
FED_AUDIO_MAX_WINDOW_MINUTES = _int("FED_AUDIO_MAX_WINDOW_MINUTES", 150)

DASHBOARD_PORT = _int("DASHBOARD_PORT", 8000)
# Falls gesetzt, verlangen alle /api/*-Endpunkte einen passenden "X-API-Key"-Header.
# Ohne das waere z.B. /api/test (kostet einen echten Claude-Call + kann einen echten
# Telegram-Alert ausloesen) fuer JEDEN erreichbar, der die IP:Port kennt - insbesondere
# relevant, weil die README-Anleitung fuer die Oracle-Cloud-Variante explizit dazu
# anleitet, Port 8000 fuer 0.0.0.0/0 zu oeffnen.
DASHBOARD_API_KEY = _str("DASHBOARD_API_KEY")

DB_PATH = os.getenv("DB_PATH", "trump_monitor.db")

# Nur fuer den HISTORISCHEN Abruf im Backtest (app/sources/news_alphavantage.py), nicht
# fuer den Live-Betrieb: der kostenlose Tarif erlaubt zu wenige Abfragen pro Tag fuer
# einen Minuten-Poll, reicht fuer einen einmaligen Backtest-Lauf aber locker. Ohne Key
# ist schlicht die Quelle --source alphavantage nicht nutzbar; alles andere laeuft
# unveraendert weiter. Kostenlos unter alphavantage.co/support/#api-key.
ALPHAVANTAGE_API_KEY = _str("ALPHAVANTAGE_API_KEY")

# Ebenfalls nur fuer den historischen Backtest-Abruf (--source finnhub), aus demselben
# Grund wie ALPHAVANTAGE_API_KEY: fuer den Live-Betrieb ungeeignet, fuer einen
# einmaligen Lauf ausreichend. Zweite Quelle NEBEN Alpha Vantage, weil deren Gratis-
# Tarif nur 25 Anfragen/TAG erlaubt (bei mehreren Testlaeufen am selben Tag schnell
# aufgebraucht) - Finnhubs Gratis-Tarif erlaubt stattdessen ~60/MINUTE. Kostenlos ohne
# Kreditkarte: finnhub.io/register
FINNHUB_API_KEY = _str("FINNHUB_API_KEY")


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

    if not ENABLE_NEWS and not ENABLE_TRUTH_SOCIAL:
        errors.append("Alle Quellen sind deaktiviert (ENABLE_NEWS/TRUTH_SOCIAL=false).")

    if PAPER_TRADING and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        warnings.append(
            "PAPER_TRADING=true aber Telegram ist nicht konfiguriert - die virtuellen "
            "Positionen werden zwar in der DB gefuehrt, aber es gibt keine Depot-/Trade-"
            "Meldungen (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID setzen)."
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
    if ENABLE_ENSEMBLE_MODEL and not ENABLE_PRICE_TRACKING:
        warnings.append(
            "ENABLE_ENSEMBLE_MODEL=true, aber ENABLE_PRICE_TRACKING=false - ohne "
            "ausgewertete Ergebnisse gibt es keine Trainingsdaten, das Ensemble-Modell "
            "bleibt dauerhaft inaktiv (kein Effekt)."
        )
    if VIX_SUPPRESS_ABOVE > 0 and VIX_SUPPRESS_ABOVE < VIX_HIGH_THRESHOLD:
        warnings.append(
            f"VIX_SUPPRESS_ABOVE={VIX_SUPPRESS_ABOVE} liegt UNTER VIX_HIGH_THRESHOLD="
            f"{VIX_HIGH_THRESHOLD} - Alerts werden dann schon vor dem eigentlichen "
            "'hohe Marktangst'-Stand hart unterdrueckt statt nur im Score abgewertet."
        )
    if not (0.0 < PAPER_LOSS_STREAK_SIZE_FACTOR <= 1.0):
        warnings.append(
            f"PAPER_LOSS_STREAK_SIZE_FACTOR={PAPER_LOSS_STREAK_SIZE_FACTOR} liegt "
            "ausserhalb (0, 1] - der Kapitalerhalt-Modus wuerde die Positionsgroesse "
            "damit nicht sinnvoll reduzieren (<=0 -> nie ein Trade, >1 -> vergroessern "
            "statt verkleinern)."
        )

    if ENABLE_FED_AUDIO:
        if not FED_AUDIO_STREAM_URL:
            warnings.append(
                "ENABLE_FED_AUDIO=true, aber FED_AUDIO_STREAM_URL ist leer - die "
                "Quelle bleibt dauerhaft inaktiv (kein Effekt)."
            )
        if not FED_MEETING_WINDOWS:
            warnings.append(
                "ENABLE_FED_AUDIO=true, aber FED_MEETING_WINDOWS ist leer - es gibt "
                "keine Zeitfenster zum Zuhoeren, die Quelle bleibt dauerhaft inaktiv. "
                "Termine: federalreserve.gov/monetarypolicy/fomccalendars.htm"
            )
        elif not _parse_fed_meeting_windows(FED_MEETING_WINDOWS):
            errors.append(
                f"FED_MEETING_WINDOWS='{FED_MEETING_WINDOWS}' konnte nicht geparst "
                "werden - erwartetes Format: 'ISO-Startzeit(UTC)/Dauer-in-Minuten', "
                "Komma-getrennt, z.B. '2026-09-16T18:30/90,2026-11-04T19:00/90'."
            )

    return errors, warnings


def _parse_fed_meeting_windows(raw: str) -> list[tuple]:
    """Parst FED_MEETING_WINDOWS in (start, ende)-datetime-Paare (UTC), inkl.
    FED_AUDIO_MAX_WINDOW_MINUTES als hartem Deckel. Eigene Funktion statt Inline-Code
    in app/sources/fed_audio.py, damit validate() denselben Parser fuer die
    Konfigurationspruefung nutzt wie die Quelle selbst zur Laufzeit - ein Tippfehler
    soll beim Start auffallen, nicht erst wenn ein Fenster laengst vorbei ist."""
    import datetime

    windows = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            start_str, minutes_str = entry.split("/")
            start = datetime.datetime.fromisoformat(start_str.strip()).replace(
                tzinfo=datetime.timezone.utc
            )
            minutes = min(int(minutes_str.strip()), FED_AUDIO_MAX_WINDOW_MINUTES)
            end = start + datetime.timedelta(minutes=minutes)
            windows.append((start, end))
        except (ValueError, TypeError):
            return []
    return windows
