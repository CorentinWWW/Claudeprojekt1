"""Tests fuer die harten Risikogrenzen (Kapitalschutz): Drawdown-Stop vom Hoechststand
und Expositions-Grenze. Beide greifen VOR dem Eroeffnen und sperren nur NEUE Positionen -
laufende behalten ihre Stop-/Ziel-Marken und werden nie zwangsliquidiert.

Wichtig ist hier vor allem die Gegenprobe: eine Bremse, die auch im Normalbetrieb
ausloest, waere schlimmer als keine. Kein Netz (Kursabfragen/Telegram gemockt)."""
import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    print(f"[{'OK' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


def _fresh(**env):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    defaults = {
        "DB_PATH": tmp.name,
        "PAPER_TRADING": "true",
        "PAPER_STARTING_CAPITAL": "1000",
        "ALERT_MIN_TICKER_CONFIDENCE": "0.5",
        "PAPER_MAX_DRAWDOWN_PCT": "20",
        "PAPER_MAX_TOTAL_EXPOSURE_PCT": "60",
    }
    defaults.update(env)
    for k, v in defaults.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    import app.paper_trading as pt
    importlib.reload(pt)
    return config, db, pt


def _capture():
    sent = []

    async def fake_send(text, **kwargs):
        sent.append(text)
        return True

    import app.telegram_alert as tg
    tg.send_text = fake_send
    return sent


def test_normalbetrieb_wird_nicht_gebremst():
    """Gegenprobe zuerst - eine Bremse, die im Normalfall ausloest, waere wertlos."""
    _, _, pt = _fresh()
    snap = {"account_value": 1000.0, "free_cash": 1000.0, "open_stake": 0.0}
    check("frisches Depot: keine Sperre", pt.risk_block_reason(snap) is None)

    snap_half = {"account_value": 1000.0, "free_cash": 700.0, "open_stake": 300.0}
    check("30% investiert (unter der 60%-Grenze): keine Sperre",
          pt.risk_block_reason(snap_half) is None)

    snap_small_loss = {"account_value": 950.0, "free_cash": 950.0, "open_stake": 0.0}
    check("5% Verlust (unter der 20%-Grenze): keine Sperre",
          pt.risk_block_reason(snap_small_loss) is None)


def test_drawdown_stop_greift():
    _, _, pt = _fresh()
    # Hoechststand auf 1000 setzen (frisches Depot), dann tief fallen lassen.
    pt.risk_block_reason({"account_value": 1000.0, "free_cash": 1000.0, "open_stake": 0.0})

    snap_19 = {"account_value": 810.0, "free_cash": 810.0, "open_stake": 0.0}
    check("19% Drawdown: noch keine Sperre", pt.risk_block_reason(snap_19) is None)

    snap_20 = {"account_value": 800.0, "free_cash": 800.0, "open_stake": 0.0}
    reason = pt.risk_block_reason(snap_20)
    check("20% Drawdown: Sperre greift", reason is not None)
    check("Sperre nennt den Drawdown-Grund", reason and "Drawdown-Stop" in reason)


def test_drawdown_bezieht_sich_auf_hoechststand_nicht_startkapital():
    """Ein Depot, das auf 150% gestiegen und dann auf 110% gefallen ist, hat ein Drittel
    seines Werts verloren - obwohl es formal noch im Plus steht. Genau dann soll die
    Bremse greifen."""
    _, _, pt = _fresh()
    # Hoechststand auf 1500 hochziehen.
    pt.risk_block_reason({"account_value": 1500.0, "free_cash": 1500.0, "open_stake": 0.0})

    # 1100 = +10% ggue. Startkapital, aber -26.7% ggue. Hoechststand.
    snap = {"account_value": 1100.0, "free_cash": 1100.0, "open_stake": 0.0}
    reason = pt.risk_block_reason(snap)
    check("noch im Plus ggue. Start, aber -27% vom Hoch: Sperre greift", reason is not None)
    check("Grund nennt den Hoechststand", reason and "1500" in reason)


def test_drawdown_stop_loest_sich_bei_erholung():
    """Selbstheilend: erholt sich das Depot ueber die Schwelle, laeuft es weiter -
    sonst waere ein einmaliger Einbruch ein dauerhaftes Todesurteil."""
    _, _, pt = _fresh()
    pt.risk_block_reason({"account_value": 1000.0, "free_cash": 1000.0, "open_stake": 0.0})
    check("bei 800: gesperrt",
          pt.risk_block_reason({"account_value": 800.0, "free_cash": 800.0, "open_stake": 0.0}))
    check("wieder bei 900: entsperrt",
          pt.risk_block_reason({"account_value": 900.0, "free_cash": 900.0, "open_stake": 0.0}) is None)


def test_expositions_grenze_greift():
    _, _, pt = _fresh()
    snap_59 = {"account_value": 1000.0, "free_cash": 410.0, "open_stake": 590.0}
    check("59% gebunden: noch frei", pt.risk_block_reason(snap_59) is None)

    snap_60 = {"account_value": 1000.0, "free_cash": 400.0, "open_stake": 600.0}
    reason = pt.risk_block_reason(snap_60)
    check("60% gebunden: Sperre greift", reason is not None)
    check("Sperre nennt die Expositions-Grenze", reason and "Expositions-Grenze" in reason)


def test_grenzen_abschaltbar():
    _, _, pt = _fresh(PAPER_MAX_DRAWDOWN_PCT="0", PAPER_MAX_TOTAL_EXPOSURE_PCT="0")
    pt.risk_block_reason({"account_value": 1000.0, "free_cash": 1000.0, "open_stake": 0.0})
    snap = {"account_value": 100.0, "free_cash": 0.0, "open_stake": 100.0}
    check("beide Grenzen auf 0: keine Sperre trotz 90% Verlust und 100% Exposition",
          pt.risk_block_reason(snap) is None)


def test_sperre_verhindert_eroeffnung_und_meldet_sie():
    """Ende-zu-Ende: die Sperre muss das Eroeffnen tatsaechlich verhindern UND sichtbar
    gemeldet werden (keine stille Luecke)."""
    _, db, pt = _fresh()
    sent = _capture()

    async def quote(ticker):
        return {"price": 100.0, "high": 102.0, "low": 98.0}
    pt.prices.get_quote = quote

    cls = db.Classification(
        is_market_relevant=True, sentiment="positive", confidence=0.95,
        ticker_calls=[{"ticker": "AAA", "direction": "long", "confidence": 0.95}],
    )

    # Hoechststand setzen, dann Depot per realisiertem Verlust unter die Schwelle druecken.
    pt.risk_block_reason({"account_value": 1000.0, "free_cash": 1000.0, "open_stake": 0.0})
    import app.db as dbmod
    dbmod.set_meta("paper_account_high_water", "1000.0")

    # account_snapshot() rechnet Startkapital + realisierte Ergebnisse; ohne echte Trades
    # patchen wir den Snapshot direkt auf einen Drawdown-Zustand.
    pt.account_snapshot = lambda: {
        "account_value": 750.0, "free_cash": 750.0, "realized_pnl": -250.0, "open_stake": 0.0,
    }

    asyncio.run(pt.open_positions_for_alert(cls, 1, 90))
    check("Sperre aktiv: KEINE Position eroeffnet", db.count_open_paper_positions() == 0)
    check("Sperre aktiv: genau eine Meldung", len(sent) == 1)
    check("Meldung erklaert die Sperre", sent and "Drawdown-Stop" in sent[0])
    check("Meldung ist als 'keine Position' erkennbar",
          sent and "Keine Paper-Position" in sent[0])


def main():
    test_normalbetrieb_wird_nicht_gebremst()
    test_drawdown_stop_greift()
    test_drawdown_bezieht_sich_auf_hoechststand_nicht_startkapital()
    test_drawdown_stop_loest_sich_bei_erholung()
    test_expositions_grenze_greift()
    test_grenzen_abschaltbar()
    test_sperre_verhindert_eroeffnung_und_meldet_sie()

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
