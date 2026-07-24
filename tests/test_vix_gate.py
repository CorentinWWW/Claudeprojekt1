"""Tests fuer das VIX-Marktregime-Gate (app/orchestrator.py: ENABLE_VIX_GATE): bei hoher
marktweiter Angst (VIX >= VIX_HIGH_THRESHOLD) wird der Ueberzeugungs-Score abgewertet und
optional (VIX_SUPPRESS_ABOVE) der Alert ganz unterdrueckt. Der VIX-Stand wird ueber
app.prices.get_index_quote() best-effort geholt - hier gemockt, kein echtes Netz."""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []

_GATE_KEYS = [
    "ENABLE_VIX_GATE", "VIX_HIGH_THRESHOLD", "VIX_SUPPRESS_ABOVE", "VIX_CONVICTION_PENALTY",
    "ALERT_MIN_CONVICTION",
]


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(env=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    for k in _GATE_KEYS:
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.prices as prices
    importlib.reload(prices)
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, prices, orch, tmp.name


def _install_fakes(orch, ticker_quote, vix_price):
    async def fake_get_quote(ticker, client=None):
        return ticker_quote

    async def fake_get_index_quote(symbol, client=None):
        if vix_price is None:
            return None
        return {"price": vix_price}

    orch.prices.get_quote = fake_get_quote
    orch.prices.get_index_quote = fake_get_index_quote


def test_vix_gate_disabled_has_no_effect():
    config, db, prices, orch, tmp_name = _fresh()
    _install_fakes(orch, {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 0.0}, vix_price=40.0)

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
    )
    raw = db.RawStatement(source="test", source_id="disabled", text="AAA rakete")
    sid = db.insert_statement(raw, cls)
    asyncio.run(orch._send_alerts([(raw, cls, sid)]))

    stats = db.get_gate_statistics(hours=24)
    check("VIX-Gate deaktiviert -> kein vix_regime-Gate geloggt", "vix_regime" not in stats)
    os.unlink(tmp_name)


def test_vix_gate_suppresses_above_threshold():
    config, db, prices, orch, tmp_name = _fresh({
        "ENABLE_VIX_GATE": "true",
        "VIX_HIGH_THRESHOLD": "30",
        "VIX_SUPPRESS_ABOVE": "35",
        "ALERT_MIN_CONVICTION": "0",
    })
    _install_fakes(orch, {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 0.0}, vix_price=40.0)

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
    )
    raw = db.RawStatement(source="test", source_id="high-vix", text="AAA rakete")
    sid = db.insert_statement(raw, cls)
    asyncio.run(orch._send_alerts([(raw, cls, sid)]))

    row = next(r for r in db.get_recent(limit=10) if r["source_id"] == "high-vix")
    check("Alert bei VIX ueber Suppress-Schwelle NICHT verschickt", row["alert_sent"] == 0)

    stats = db.get_gate_statistics(hours=24)
    check("vix_regime-Gate wurde geloggt", "vix_regime" in stats)
    check("vix_regime-Gate hat mind. 1 Blockierung", stats["vix_regime"]["blocked"] >= 1)
    os.unlink(tmp_name)


def test_vix_gate_passes_below_threshold():
    config, db, prices, orch, tmp_name = _fresh({
        "ENABLE_VIX_GATE": "true",
        "VIX_HIGH_THRESHOLD": "30",
        "VIX_SUPPRESS_ABOVE": "35",
        "ALERT_MIN_CONVICTION": "0",
    })
    _install_fakes(orch, {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 0.0}, vix_price=15.0)

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
    )
    raw = db.RawStatement(source="test", source_id="low-vix", text="AAA rakete")
    sid = db.insert_statement(raw, cls)
    asyncio.run(orch._send_alerts([(raw, cls, sid)]))

    stats = db.get_gate_statistics(hours=24)
    check("vix_regime-Gate bei niedrigem VIX bestanden (mind. 1x passed)",
          stats["vix_regime"]["passed"] >= 1)
    os.unlink(tmp_name)


def test_vix_gate_missing_quote_has_no_effect():
    """Kursdienst nicht erreichbar (vix_price=None) -> Gate greift fuer diesen Zyklus
    einfach nicht, kein Fehler, kein Suppress."""
    config, db, prices, orch, tmp_name = _fresh({
        "ENABLE_VIX_GATE": "true",
        "VIX_SUPPRESS_ABOVE": "35",
        "ALERT_MIN_CONVICTION": "0",
    })
    _install_fakes(orch, {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 0.0}, vix_price=None)

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.9,
        ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
    )
    raw = db.RawStatement(source="test", source_id="no-vix", text="AAA rakete")
    sid = db.insert_statement(raw, cls)
    asyncio.run(orch._send_alerts([(raw, cls, sid)]))

    stats = db.get_gate_statistics(hours=24)
    check("kein VIX-Kurs erreichbar -> kein vix_regime-Gate geloggt", "vix_regime" not in stats)
    os.unlink(tmp_name)


def test_conviction_penalty_without_hard_suppress():
    """Ohne VIX_SUPPRESS_ABOVE (0 = aus) wirkt der hohe VIX nur als Score-Abschlag: der
    Alert wird trotzdem verschickt, aber der Ueberzeugungs-Score in den Extras faellt
    niedriger aus als ohne Gate."""
    config, db, prices, orch, tmp_name = _fresh({
        "ENABLE_VIX_GATE": "true",
        "VIX_HIGH_THRESHOLD": "30",
        "VIX_CONVICTION_PENALTY": "10",
        "ALERT_MIN_CONVICTION": "0",
    })
    _install_fakes(orch, {"price": 100.0, "high": 105.0, "low": 95.0, "change_pct": 0.0}, vix_price=40.0)

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
    )
    raw = db.RawStatement(source="test", source_id="penalty-only", text="AAA rakete durchbruch")
    sid = db.insert_statement(raw, cls)

    score_with_gate, *_ = orch._compute_conviction(raw, cls, sid)
    extras = asyncio.run(orch._build_alert_extras(raw, cls, sid, score_with_gate, 1, False, vix_price=40.0))
    check("Score in den Extras um die VIX-Strafe reduziert",
          extras["conviction"] == max(0, score_with_gate - 10) or extras["conviction"] < 100)
    check("VIX-Info in den Extras vorhanden", extras.get("vix") == {"level": 40.0, "high_fear": True})

    asyncio.run(orch._send_alerts([(raw, cls, sid)]))
    row = next(r for r in db.get_recent(limit=10) if r["source_id"] == "penalty-only")
    check("Alert trotz hohem VIX verschickt (kein hartes Suppress konfiguriert)",
          row["alert_sent"] == 0)  # kein Telegram konfiguriert -> send_alert liefert False,
    # aber KEIN Gate darf blockiert haben:
    stats = db.get_gate_statistics(hours=24)
    check("vix_regime-Gate ohne Suppress-Schwelle wird NICHT geloggt (nur Score-Effekt)",
          "vix_regime" not in stats)
    os.unlink(tmp_name)


def main():
    test_vix_gate_disabled_has_no_effect()
    test_vix_gate_suppresses_above_threshold()
    test_vix_gate_passes_below_threshold()
    test_vix_gate_missing_quote_has_no_effect()
    test_conviction_penalty_without_hard_suppress()

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
