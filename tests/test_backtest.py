"""Tests fuer die Backtest-Engine (app/backtest.py): Kostenschaetzung, Ergebnis-
Auswertung (Handelstage-Horizont) und den Gesamtlauf (run_backtest). Kein echtes Netz -
GDELT/Claude/Kursabfragen werden gemockt. Wichtigster Einzeltest ist die
Dry-Run-Sicherheitsgarantie (test_dry_run_macht_niemals_einen_claude_call): ohne
--execute darf NIEMALS echtes Geld ausgegeben werden."""
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

import app.backtest as bt
from app.db import Classification, RawStatement

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _tmp_db_path() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


# --- Kostenschaetzung ---------------------------------------------------------------
def test_kostenschaetzung_waechst_mit_artikellaenge():
    short = bt.estimate_call_cost_usd("kurz")
    long = bt.estimate_call_cost_usd("x" * 5000)
    check("laengerer Artikel kostet mehr (mehr Input-Token)", long > short)
    check("Kostenschaetzung ist immer positiv", short > 0)


def test_kostenschaetzung_nutzt_modellspezifische_preise():
    haiku = bt.estimate_call_cost_usd("dieselbe Meldung", model="claude-haiku-4-5")
    sonnet = bt.estimate_call_cost_usd("dieselbe Meldung", model="claude-sonnet-5")
    check("Sonnet ist teurer geschaetzt als Haiku bei identischem Text", sonnet > haiku)

    unbekannt = bt.estimate_call_cost_usd("dieselbe Meldung", model="claude-irgendwas-9000")
    check(
        "unbekanntes Modell faellt auf Haiku-Preise zurueck statt abzustuerzen",
        unbekannt == haiku,
    )


# --- Ergebnis-Auswertung (evaluate_ticker_outcome) -----------------------------------
_HIST = {
    "date": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06", "2026-01-07"],
    "close": [100.0, 102.0, 101.0, 99.0, 108.0],
}


def _ts(date_str: str) -> float:
    return datetime.datetime.fromisoformat(date_str).replace(tzinfo=datetime.timezone.utc).timestamp()


def test_outcome_long_position_die_richtig_lag():
    o = bt.evaluate_ticker_outcome("AAA", "long", 0.9, _ts("2026-01-01"), _HIST, horizon_days=2)
    check("Ergebnis vorhanden", o is not None)
    check("Long, Kurs steigt -> hit", o is not None and o.hit is True)
    check("signed_move_pct stimmt (100 -> 101 = +1%)", o is not None and abs(o.signed_move_pct - 1.0) < 1e-9)


def test_outcome_short_position_die_falsch_lag():
    o = bt.evaluate_ticker_outcome("AAA", "short", 0.9, _ts("2026-01-01"), _HIST, horizon_days=2)
    check("Short bei steigendem Kurs -> kein hit", o is not None and o.hit is False)
    check("signed_move_pct fuer Short gespiegelt (-1%)", o is not None and abs(o.signed_move_pct + 1.0) < 1e-9)


def test_outcome_pending_wenn_horizont_ueber_verfuegbare_historie_hinausgeht():
    o = bt.evaluate_ticker_outcome("AAA", "long", 0.9, _ts("2026-01-07"), _HIST, horizon_days=2)
    check("kein Fehler, sondern None (noch nicht auswertbar)", o is None)


def test_outcome_none_ohne_historie():
    o = bt.evaluate_ticker_outcome("AAA", "long", 0.9, _ts("2026-01-01"), None, horizon_days=2)
    check("fehlende Historie -> None", o is None)


def test_outcome_none_bei_ungueltiger_richtung():
    o = bt.evaluate_ticker_outcome("AAA", "neutral", 0.9, _ts("2026-01-01"), _HIST, horizon_days=2)
    check("Richtung ausserhalb long/short -> None", o is None)


def test_multi_horizon_wertet_alle_gleichzeitig_aus():
    # _HIST hat 5 Handelstage (Indizes 0-4) - ab Index 0 (2026-01-01) sind die
    # Horizonte 1-4 auswertbar (exit_idx 1-4 < len=5), Horizont 5 nicht mehr
    # (exit_idx 5 >= len=5, Historie zu kurz).
    results = bt.evaluate_ticker_outcomes_multi(
        "AAA", "long", 0.9, _ts("2026-01-01"), _HIST, [1, 2, 3, 4, 5],
    )
    check("Horizonte 1/2/3/4 auswertbar", set(results.keys()) == {1, 2, 3, 4})
    check("Horizont 5 fehlt (Historie zu kurz), kein Fehler", 5 not in results)
    check("Horizont 1 nutzt denselben Einstiegskurs wie Horizont 3",
          results[1].entry_close == results[3].entry_close == 100.0)
    check("unterschiedliche Horizonte liefern unterschiedliche Ausstiegskurse",
          results[1].exit_close != results[3].exit_close)


def test_outcome_wochenende_springt_zum_naechsten_handelstag():
    # 2026-01-04 (Sonntag) ist kein Handelstag in _HIST - Veroeffentlichung an einem
    # Wochenende muss auf den naechsten verfuegbaren Handelstag (01-06) rutschen.
    # Mittags-UTC (nicht Mitternacht) verwendet, damit die UTC->ET-Umrechnung
    # (current_market_date) nicht ueber die Datumsgrenze auf den Vortag zurueckfaellt.
    published_at = _ts("2026-01-04") + 18 * 3600
    o = bt.evaluate_ticker_outcome("AAA", "long", 0.9, published_at, _HIST, horizon_days=1)
    check("Einstieg springt auf naechsten Handelstag (2026-01-06)", o is not None and o.entry_date == "2026-01-06")


# --- Tier-Aufschluesselung ------------------------------------------------------------
def test_tier_breakdown_gruppiert_und_aggregiert():
    outcomes = [
        bt.TickerOutcome("AAA", "long", 0.9, 0.0, "d1", 100.0, "d2", 110.0, 10.0, True, "small"),
        bt.TickerOutcome("BBB", "long", 0.9, 0.0, "d1", 100.0, "d2", 90.0, -10.0, False, "small"),
        bt.TickerOutcome("CCC", "long", 0.9, 0.0, "d1", 100.0, "d2", 105.0, 5.0, True, "large"),
        bt.TickerOutcome("DDD", "long", 0.9, 0.0, "d1", 100.0, "d2", 105.0, 5.0, True, None),
    ]
    result = bt._tier_breakdown(outcomes)
    check("small-Tier hat 2 Eintraege", result.get("small", {}).get("n") == 2)
    check("small-Tier Hit-Rate ist 0.5 (1 von 2)", result.get("small", {}).get("hit_rate") == 0.5)
    check("large-Tier hat 1 Eintrag mit Hit-Rate 1.0", result.get("large") == {
        "n": 1, "hit_rate": 1.0, "avg_signed_move_pct": 5.0,
    })
    check("fehlende Marktkapitalisierung landet unter 'unbekannt'", result.get("unbekannt", {}).get("n") == 1)


# --- Gesamtlauf (run_backtest) --------------------------------------------------------
def test_dry_run_macht_niemals_einen_claude_call():
    """Die wichtigste Garantie des gesamten Moduls: ohne --execute darf NIE Geld
    ausgegeben werden."""
    orig_fetch_range, orig_classify = bt.fetch_range, bt.classify
    calls = {"classify": 0}

    async def fake_fetch_range(start, end, client=None):
        return [
            RawStatement(
                source="news_gdelt", source_id=f"https://x/{start.date()}",
                text="Fed hikes interest rates sharply amid tariff war escalation.",
                published_at=start.timestamp(),
            )
        ]

    async def fake_classify(*a, **kw):
        calls["classify"] += 1
        raise AssertionError("classify() darf im Dry-Run NIE aufgerufen werden")

    bt.fetch_range = fake_fetch_range
    bt.classify = fake_classify
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 1, 1), datetime.date(2026, 1, 1),
            execute=False, db_path=_tmp_db_path(),
        ))
    finally:
        bt.fetch_range, bt.classify = orig_fetch_range, orig_classify

    check("kein einziger classify()-Aufruf im Dry-Run", calls["classify"] == 0)
    check("Report als dry_run markiert", report.get("dry_run") is True)
    check("Rohtreffer erfasst", report.get("raw_fetched") == 1)
    check("Kostenvoranschlag vorhanden und > 0", report.get("estimated_cost_usd", 0) > 0)
    check("kein actual_cost_usd im Dry-Run-Report", "actual_cost_usd" not in report)


def test_fehlgeschlagene_tage_werden_sichtbar_nicht_mit_leerer_historie_verwechselt():
    """Regressionstest: raw_fetched=0 sieht identisch aus, egal ob GDELT fuer den
    Zeitraum wirklich nichts hat ODER der Abruf trotz Retries fehlgeschlagen ist (siehe
    fetch_all_raw()-Docstring) - days_failed/days_total machen den Unterschied im
    Report sichtbar, statt einen unvollstaendigen Lauf wie ein sauberes
    Leer-Ergebnis aussehen zu lassen."""
    orig_fetch_range = bt.fetch_range

    async def flaky_fetch_range(start, end, client=None):
        # Tag 1 schlaegt (trotz fetch_range()-eigener Retries) komplett fehl, Tag 2
        # liefert normal Daten.
        if start.date() == datetime.date(2026, 1, 1):
            raise Exception("GDELT dauerhaft 429 trotz Retries")
        return [
            RawStatement(
                source="news_gdelt", source_id=f"https://x/{start.date()}",
                text="Central bank hints at surprise rate move, markets react sharply.",
                published_at=start.timestamp(),
            )
        ]

    bt.fetch_range = flaky_fetch_range
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 1, 1), datetime.date(2026, 1, 2),
            execute=False, db_path=_tmp_db_path(),
        ))
    finally:
        bt.fetch_range = orig_fetch_range

    check("days_total zaehlt beide Tage im Zeitraum", report.get("days_total") == 2)
    check("days_failed zaehlt genau den einen fehlgeschlagenen Tag", report.get("days_failed") == 1)
    check("raw_fetched trotzdem korrekt (nur der erfolgreiche Tag)", report.get("raw_fetched") == 1)
    check(
        "Hinweis auf fehlgeschlagenen Abruf im Report, nicht stillschweigend uebergangen",
        "Rate-Limit" in report.get("note", ""),
    )


def test_execute_liefert_aufgeloeste_ergebnisse_mit_tier():
    orig_fetch_range, orig_classify = bt.fetch_range, bt.classify
    orig_get_history, orig_get_caps = bt.prices.get_history, bt.prices.get_market_caps

    async def fake_fetch_range(start, end, client=None):
        return [
            RawStatement(
                source="news_gdelt", source_id="https://x/1",
                text="Tariffs imposed on AAA imports, shares expected to react.",
                published_at=_ts("2026-01-01"),
            )
        ]

    async def fake_classify(text, recent_context=None, _bypass_daily_cap=False, **kw):
        return Classification(
            is_market_relevant=True, sentiment="negative", confidence=0.95,
            ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
        )

    async def fake_get_history(ticker, client=None):
        return _HIST

    async def fake_get_market_caps(tickers, client=None):
        return {"AAA": 500_000_000}

    bt.fetch_range = fake_fetch_range
    bt.classify = fake_classify
    bt.prices.get_history = fake_get_history
    bt.prices.get_market_caps = fake_get_market_caps
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 1, 1), datetime.date(2026, 1, 1),
            execute=True, max_calls=10, horizon_days=2, db_path=_tmp_db_path(),
        ))
    finally:
        bt.fetch_range, bt.classify = orig_fetch_range, orig_classify
        bt.prices.get_history, bt.prices.get_market_caps = orig_get_history, orig_get_caps

    check("nicht laenger als Dry-Run markiert", report.get("dry_run") is False)
    check("genau ein Klassifikations-Call", report.get("classified") == 1)
    check("als alarmwuerdig erkannt", report.get("alert_worthy") == 1)
    check("ein aufgeloestes Ergebnis", report.get("outcomes_resolved") == 1)
    check("Hit-Rate 1.0 (Long bei steigendem Kurs)", report.get("hit_rate") == 1.0)
    check("small-Tier-Aufschluesselung vorhanden", report.get("by_tier", {}).get("small", {}).get("n") == 1)
    check("actual_cost_usd > 0 nach echtem Lauf", report.get("actual_cost_usd", 0) > 0)


def test_execute_mit_mehreren_horizonten_liefert_by_horizon():
    """Der eigentliche Zweck des Multi-Horizont-Umbaus: EIN bezahlter Klassifikations-
    Call, aber ein Vergleich ueber mehrere Haltezeitraeume - ohne fuer jeden Horizont
    erneut zu klassifizieren."""
    orig_fetch_range, orig_classify = bt.fetch_range, bt.classify
    orig_get_history, orig_get_caps = bt.prices.get_history, bt.prices.get_market_caps
    calls = {"n": 0}

    async def fake_fetch_range(start, end, client=None):
        return [
            RawStatement(
                source="news_gdelt", source_id="https://x/1",
                text="Tariffs imposed on AAA imports, shares expected to react.",
                published_at=_ts("2026-01-01"),
            )
        ]

    async def fake_classify(text, recent_context=None, _bypass_daily_cap=False, **kw):
        calls["n"] += 1
        return Classification(
            is_market_relevant=True, sentiment="negative", confidence=0.95,
            ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
        )

    long_hist = {
        "date": [f"2026-01-{d:02d}" for d in range(1, 13)],
        "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0],
    }

    async def fake_get_history(ticker, client=None):
        return long_hist

    async def fake_get_market_caps(tickers, client=None):
        return {"AAA": 500_000_000}

    bt.fetch_range = fake_fetch_range
    bt.classify = fake_classify
    bt.prices.get_history = fake_get_history
    bt.prices.get_market_caps = fake_get_market_caps
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 1, 1), datetime.date(2026, 1, 1),
            execute=True, max_calls=10, horizon_days=[5, 1, 3], db_path=_tmp_db_path(),
        ))
    finally:
        bt.fetch_range, bt.classify = orig_fetch_range, orig_classify
        bt.prices.get_history, bt.prices.get_market_caps = orig_get_history, orig_get_caps

    check("genau EIN Claude-Call fuer alle Horizonte zusammen", calls["n"] == 1)
    check("horizon_days im Report als sortierte Liste", report.get("horizon_days") == [1, 3, 5])
    check(
        "by_horizon enthaelt alle drei Horizonte",
        set(report.get("by_horizon", {}).keys()) == {"1", "3", "5"},
    )
    check(
        "jeder Horizont hat genau ein Ergebnis",
        all(v["n"] == 1 for v in report.get("by_horizon", {}).values()),
    )
    check(
        "laengerer Horizont zeigt bei durchgehend steigendem Kurs groessere Ø-Bewegung",
        report["by_horizon"]["5"]["avg_signed_move_pct"] > report["by_horizon"]["1"]["avg_signed_move_pct"],
    )
    check(
        "hit_rate/by_tier beziehen sich auf den primaeren (kleinsten) Horizont",
        report.get("hit_rate") == 1.0 and report.get("by_tier", {}).get("small", {}).get("n") == 1,
    )


def test_max_calls_begrenzt_echte_ausgaben_hart():
    orig_fetch_range, orig_classify = bt.fetch_range, bt.classify
    calls = {"n": 0}

    # Bewusst 5 THEMATISCH VERSCHIEDENE Meldungen (nicht nur eine Zahl variiert) - sonst
    # wuerde sie die echte Tier-1-Duplikaterkennung (text_similarity, siehe
    # orchestrator._partition_duplicates) zu Recht als Duplikate zusammenfassen und der
    # Test wuerde die max_calls-Kappung gar nicht mehr pruefen koennen.
    topics = [
        "Federal Reserve raises interest rates by 50 basis points amid inflation fears.",
        "Major tariffs imposed on steel imports spark trade war concerns.",
        "Tech giant announces surprise merger with rival chipmaker.",
        "Oil prices plunge after OPEC output increase announcement.",
        "Central bank governor hints at emergency rate cut next quarter.",
    ]

    async def fake_fetch_range(start, end, client=None):
        return [
            RawStatement(
                source="news_gdelt", source_id=f"https://x/{i}", text=topic,
                published_at=start.timestamp(),
            )
            for i, topic in enumerate(topics)
        ]

    async def fake_classify(text, recent_context=None, _bypass_daily_cap=False, **kw):
        calls["n"] += 1
        return Classification(is_market_relevant=False, sentiment="neutral", confidence=0.1)

    bt.fetch_range = fake_fetch_range
    bt.classify = fake_classify
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 1, 1), datetime.date(2026, 1, 1),
            execute=True, max_calls=2, db_path=_tmp_db_path(),
        ))
    finally:
        bt.fetch_range, bt.classify = orig_fetch_range, orig_classify

    check("mehr Kandidaten als max_calls vorhanden", report.get("after_dedup", 0) >= 5)
    check("hartes Limit haelt echte Calls bei max_calls", calls["n"] == 2)
    check("Report meldet die Kappung", report.get("truncated_by_max_calls") is True)


def test_explizite_db_path_wird_tatsaechlich_verwendet():
    """Eine explizit uebergebene db_path muss auch tatsaechlich verwendet werden -
    sonst waere die Isolationsgarantie nur behauptet."""
    orig_fetch_range = bt.fetch_range

    async def fake_fetch_range(start, end, client=None):
        return []

    bt.fetch_range = fake_fetch_range
    custom_path = _tmp_db_path()
    try:
        report = asyncio.run(bt.run_backtest(
            datetime.date(2026, 1, 1), datetime.date(2026, 1, 1),
            execute=False, db_path=custom_path,
        ))
    finally:
        bt.fetch_range = orig_fetch_range

    check("Report nennt exakt die uebergebene DB-Datei", report.get("db_path") == custom_path)
    import app.db as db
    check("app.db.DB_PATH zeigt tatsaechlich auf die uebergebene Datei", db.DB_PATH == custom_path)


def main():
    test_kostenschaetzung_waechst_mit_artikellaenge()
    test_kostenschaetzung_nutzt_modellspezifische_preise()
    test_outcome_long_position_die_richtig_lag()
    test_outcome_short_position_die_falsch_lag()
    test_outcome_pending_wenn_horizont_ueber_verfuegbare_historie_hinausgeht()
    test_outcome_none_ohne_historie()
    test_outcome_none_bei_ungueltiger_richtung()
    test_outcome_wochenende_springt_zum_naechsten_handelstag()
    test_multi_horizon_wertet_alle_gleichzeitig_aus()
    test_tier_breakdown_gruppiert_und_aggregiert()
    test_dry_run_macht_niemals_einen_claude_call()
    test_fehlgeschlagene_tage_werden_sichtbar_nicht_mit_leerer_historie_verwechselt()
    test_execute_liefert_aufgeloeste_ergebnisse_mit_tier()
    test_execute_mit_mehreren_horizonten_liefert_by_horizon()
    test_max_calls_begrenzt_echte_ausgaben_hart()
    test_explizite_db_path_wird_tatsaechlich_verwendet()

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
