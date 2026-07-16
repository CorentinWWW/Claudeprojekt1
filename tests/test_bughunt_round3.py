"""Regressionstests fuer Runde-3-Bug-Hunt-Funde (5 parallele Agenten nach den 10
Trading-Verbesserungen): Zweitmeinungs-Sicherheitsnetze, Blockliste in der Anzeige,
Ticker-Zeilen-Obergrenze, kaputtes Chart-URL-Template, robuste Ticker-Typpruefung,
und nicht-endliche Kurswerte (inf/nan).
"""
import asyncio
import importlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _reload_orch(**env):
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.orchestrator as orch
    importlib.reload(orch)
    return orch


def test_escalation_keeps_original_on_fallback():
    """_maybe_escalate darf eine gute Erstbewertung NICHT verwerfen, wenn die
    Zweitmeinung ohne Exception einen FALLBACK_CLASSIFICATION liefert (unparsebare
    Antwort des Eskalations-Modells) - sonst wird ein echter Alert stillschweigend
    zu 'nicht marktrelevant' herabgestuft."""
    orch = _reload_orch(
        ALERT_MIN_TICKER_CONFIDENCE="0.9", ENABLE_BORDERLINE_ESCALATION="true",
        CLAUDE_ESCALATION_MODEL="claude-sonnet-5", ESCALATION_BAND="0.1",
    )
    from app.classifier import FALLBACK_CLASSIFICATION
    from app.db import Classification

    original = Classification(is_market_relevant=True, sentiment="negative", confidence=0.85,
                              ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.85}])

    async def fake_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        return FALLBACK_CLASSIFICATION

    orig = orch.classify
    orch.classify = fake_classify
    try:
        result = asyncio.run(orch._maybe_escalate("t", [], False, original))
    finally:
        orch.classify = orig
    check("FALLBACK von der Zweitmeinung wird verworfen, Original bleibt", result is original)
    check("Original bleibt marktrelevant", result.is_market_relevant is True)


def test_escalation_uses_normal_tier_not_priority():
    """Die Zweitmeinung darf NIE die Prioritaets-Reserve verwenden, unabhaengig vom
    priority-Flag der Erstbewertung - sonst koennte eine einzelne wichtige
    Grenzfall-Meldung zwei Reserve-Slots (Primaer + Eskalation) statt einem
    verbrauchen."""
    orch = _reload_orch(
        ALERT_MIN_TICKER_CONFIDENCE="0.9", ENABLE_BORDERLINE_ESCALATION="true",
        CLAUDE_ESCALATION_MODEL="claude-sonnet-5", ESCALATION_BAND="0.1",
    )
    from app.db import Classification

    cls = Classification(is_market_relevant=True, sentiment="negative", confidence=0.85,
                         ticker_calls=[{"ticker": "NVDA", "direction": "long", "confidence": 0.85}])

    received_priority = []

    async def fake_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        received_priority.append(priority)
        return cls

    orig = orch.classify
    orch.classify = fake_classify
    try:
        asyncio.run(orch._maybe_escalate("t", [], True, cls))  # urspruengliche Meldung IST priority=True
    finally:
        orch.classify = orig
    check("Eskalations-Call bekommt priority=False, auch wenn Original priority=True war",
          received_priority == [False])


def test_escalation_disabled_when_threshold_zero():
    """Bei deaktiviertem Praezisions-Filter (Schwelle 0) entartet 'Grenzfall um die
    Schwelle' zu 'jede Meldung ohne Ticker' - Eskalation muss in diesem Fall komplett
    ausbleiben, sonst wuerde fast jede unspezifische Meldung eine (kostenpflichtige)
    Zweitmeinung ausloesen."""
    orch = _reload_orch(
        ALERT_MIN_TICKER_CONFIDENCE="0", ENABLE_BORDERLINE_ESCALATION="true",
        CLAUDE_ESCALATION_MODEL="claude-sonnet-5", ESCALATION_BAND="0.1",
    )
    from app.db import Classification

    cls = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9, ticker_calls=[])
    calls = []

    async def fake_classify(text, recent_context=None, priority=False, model=None, _bypass_daily_cap=False):
        calls.append(1)
        return cls

    orig = orch.classify
    orch.classify = fake_classify
    try:
        result = asyncio.run(orch._maybe_escalate("t", [], False, cls))
    finally:
        orch.classify = orig
    check("keine Eskalation bei deaktiviertem Filter (kein Call)", calls == [])
    check("Original bleibt unveraendert", result is cls)


def test_visible_ticker_calls_strips_blocklist_regardless_of_threshold():
    """BLOCKLIST_TICKERS muss ticker in der ANZEIGE entfernen, auch wenn
    ALERT_MIN_TICKER_CONFIDENCE=0 ist (Filter deaktiviert) - vorher wurde der
    geblockte Ticker in diesem Modus trotzdem als normale Zeile gerendert."""
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0"
    os.environ["BLOCKLIST_TICKERS"] = "XYZ"
    import app.config as config
    importlib.reload(config)
    import app.telegram_alert as ta
    importlib.reload(ta)
    from app.db import Classification, RawStatement

    cls = Classification(is_market_relevant=True, sentiment="negative", confidence=0.9,
                         ticker_calls=[{"ticker": "XYZ", "direction": "long", "confidence": 0.99},
                                      {"ticker": "NVDA", "direction": "long", "confidence": 0.5}])
    raw = RawStatement(source="t", source_id="x", text="x")
    msg = ta._format_message(raw, cls)
    check("geblockter Ticker XYZ erscheint NICHT in der Nachricht", "XYZ" not in msg)
    check("nicht-geblockter Ticker NVDA erscheint weiterhin", "NVDA" in msg)

    # Aufraeumen fuer nachfolgende Tests in dieser Datei.
    os.environ["ALERT_MIN_TICKER_CONFIDENCE"] = "0.9"
    os.environ["BLOCKLIST_TICKERS"] = ""
    importlib.reload(config)
    importlib.reload(ta)


def test_ticker_line_cap_prevents_message_overflow():
    """Eine sehr lange ticker_calls-Liste (vom Claude-Tool-Schema nicht begrenzt)
    darf die Nachricht nicht ueber TELEGRAM_MAX_LENGTH aufblaehen - sonst lehnt
    Telegram die GESAMTE Nachricht ab und der Alert geht komplett verloren."""
    import app.telegram_alert as ta
    from app.db import Classification, RawStatement

    many = [{"ticker": f"T{i:03d}", "direction": "long", "confidence": 0.95} for i in range(150)]
    cls = Classification(is_market_relevant=True, sentiment="positive", confidence=0.9,
                         reasoning="Test", ticker_calls=many)
    raw = RawStatement(source="t", source_id="x", text="x")
    msg = ta._format_message(raw, cls)
    check("Nachricht mit 150 Tickern bleibt <= TELEGRAM_MAX_LENGTH", len(msg) <= ta.TELEGRAM_MAX_LENGTH)
    check("Ueberlauf-Hinweis vorhanden", "weitere Ticker" in msg)
    check("nur die ersten (konfidenzstaerksten) Ticker werden gezeigt",
          msg.count("🔺") == ta._MAX_TICKER_LINES)

    compact = ta._format_ticker_calls_compact(many)
    check("kompakte Form (Digest) begrenzt ebenfalls", "weitere" in compact)


def test_chart_url_template_missing_placeholder_is_safe():
    """Ein CHART_URL_TEMPLATE ohne {ticker}-Platzhalter (Tippfehler/falsch kopierte
    feste URL) darf nicht fuer jeden Ticker denselben (falschen) Button erzeugen -
    str.format() ignoriert ueberzaehlige Keyword-Args ohne Fehler."""
    os.environ["CHART_URL_TEMPLATE"] = "https://www.tradingview.com/chart/AAAA1234"
    import app.config as config
    importlib.reload(config)
    import app.telegram_alert as ta
    importlib.reload(ta)

    markup = ta._build_chart_markup([
        {"ticker": "NVDA", "direction": "long", "confidence": 0.99},
        {"ticker": "TSLA", "direction": "long", "confidence": 0.99},
    ])
    check("kaputtes Template (kein {ticker}) -> keine Buttons statt falscher Buttons",
          markup is None)

    os.environ["CHART_URL_TEMPLATE"] = "https://www.tradingview.com/chart/?symbol={ticker}"
    importlib.reload(config)
    importlib.reload(ta)
    markup2 = ta._build_chart_markup([{"ticker": "NVDA", "direction": "long", "confidence": 0.99}])
    check("intaktes Template erzeugt weiterhin normale Buttons", markup2 is not None)


def test_ticker_display_handles_non_string_and_none():
    """Ein ticker_calls-Eintrag mit None oder einem Nicht-String als 'ticker' (z.B.
    aus einer alten/fremden DB-Zeile) darf die Anzeige nicht mit einem
    AttributeError/TypeError abschiessen."""
    import app.telegram_alert as ta

    check("None-Ticker in _format_ticker_calls_compact crasht nicht",
          "?" in ta._format_ticker_calls_compact([{"ticker": None, "direction": "long", "confidence": 0.9}]))
    check("Nicht-String-Ticker (int) in _format_ticker_lines crasht nicht",
          "?" in ta._format_ticker_lines([{"ticker": 12345, "direction": "long", "confidence": 0.9}]))
    check("Nicht-String-Ticker in _build_chart_markup crasht nicht (kein Button)",
          ta._build_chart_markup([{"ticker": 12345, "direction": "long", "confidence": 0.99}]) is None)


def test_prices_rejects_non_finite_values():
    """Ein korrupter Kursdienst-Antworttext mit 'Infinity'/'NaN' darf nicht als
    gueltiger Kurs durchgehen (float() akzeptiert diese Strings klaglos) - sonst
    koennte im Alert z.B. 'heute +inf%' erscheinen."""
    from app.prices import parse_stooq_csv

    csv_inf = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
               "X.US,2026-07-13,22:00:00,150,150,150,Infinity,10")
    check("Close='Infinity' -> gesamtes Ergebnis None (kein gueltiger Preis)",
          parse_stooq_csv(csv_inf) is None)

    csv_nan_open = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                     "X.US,2026-07-13,22:00:00,NaN,150,150,206,10")
    q = parse_stooq_csv(csv_nan_open)
    check("Open='NaN' -> Preis bleibt gueltig, aber change_pct None (kein Open)",
          q is not None and q["price"] == 206.0 and q["change_pct"] is None)


def test_rss_bozo_feed_logs_warning():
    """Ein Feed, der HTTP 200 mit kaputtem/abgeschnittenem Feed-XML liefert (z.B.
    ein Bot-Challenge/Paywall-Zwischenschritt, der mitten in der Antwort abbricht),
    darf nicht unbemerkt wie 'keine Meldungen' aussehen - feedparser markiert
    fehlerhaftes (aber XML-aehnliches) Feed-Markup ueber .bozo, was jetzt geloggt
    wird. Reines Nicht-XML (z.B. eine komplette HTML-Interstitial-Seite) setzt bozo
    NICHT (feedparser behandelt das als leeres Dokument) - das deckt dieser Test
    bewusst nicht ab, da feedparser selbst das nicht als Fehler erkennt."""
    import logging
    from app.sources.news_rss import RssNewsSource

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("app.sources.news_rss")
    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        src = RssNewsSource()

        class FakeClient:
            async def get(self, url):
                class Resp:
                    def raise_for_status(self):
                        pass
                    # Abgeschnittenes/kaputtes Feed-XML - feedparser setzt bozo=1.
                    content = b'<?xml version="1.0"?><rss version="2.0"><channel><title>T<item><title>Unclosed'
                return Resp()

        result = asyncio.run(src._fetch_feed("https://example.com/rss", FakeClient()))
        check("kaputtes Feed-XML liefert leere Entries-Liste (kein Crash)", result == [])
        check("bozo-Warnung wird geloggt",
              any("kein gueltiges Feed-Format" in m for m in records))
    finally:
        logger.removeHandler(handler)


def main():
    test_escalation_keeps_original_on_fallback()
    test_escalation_uses_normal_tier_not_priority()
    test_escalation_disabled_when_threshold_zero()
    test_visible_ticker_calls_strips_blocklist_regardless_of_threshold()
    test_ticker_line_cap_prevents_message_overflow()
    test_chart_url_template_missing_placeholder_is_safe()
    test_ticker_display_handles_non_string_and_none()
    test_prices_rejects_non_finite_values()
    test_rss_bozo_feed_logs_warning()

    print()
    if failures:
        print(f"{len(failures)} FEHLGESCHLAGEN:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("Alle Tests erfolgreich.")


if __name__ == "__main__":
    main()
