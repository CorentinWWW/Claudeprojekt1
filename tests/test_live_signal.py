"""Tests fuer das taegliche Live-Signal (Nutzerwunsch: "live signal vom server jeden
tag in der frueh passend zum depot update"). Das Signal haengt bewusst am MORGENDLICHEN
Depot-Update und macht STILLE von "nichts passiert" unterscheidbar - ohne es sieht ein
abgestuerzter Bot genauso aus wie ein Tag ohne marktrelevante Nachrichten. Kein Netz
(Telegram-Versand gemockt)."""
import asyncio
import datetime
import importlib
import os
import sys
import tempfile
import time
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
    os.environ["DB_PATH"] = tmp.name
    os.environ["PAPER_TRADING"] = "true"
    os.environ.pop("ENABLE_DAILY_LIVE_SIGNAL", None)
    for k, v in env.items():
        os.environ[k] = v
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return config, db, orch


def _capture(orch):
    sent = []

    async def fake_send(text, **kwargs):
        sent.append(text)
        return True

    import app.telegram_alert as tg
    tg.send_text = fake_send
    return sent


def _healthy(orch):
    now = time.time()
    orch.run_health.update({
        "started_at": now - 270000, "last_cycle_at": now - 42, "loop_restarts": 0,
    })
    orch.source_health.clear()
    orch.source_health.update({
        "news_rss": {"last_success_at": now - 10, "last_error_at": None},
        "truth_social": {"last_success_at": now - 500, "last_error_at": now - 20},
    })


def _at_hour(orch, hour):
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 27, hour, 5, tzinfo=tz)
    orch.datetime.datetime = FakeDT


def test_dauer_formatierung():
    _, _, orch = _fresh()
    cases = [(42, "42s"), (90, "1m"), (3700, "1h 1m"), (90000, "1d 1h"), (None, "?")]
    for seconds, expected in cases:
        got = orch._format_duration(seconds)
        check(f"_format_duration({seconds}) -> {expected}", got == expected)


def test_quellen_status_erkennt_stoerung():
    """Eine Quelle gilt als gestoert, wenn ihr letzter Fehler JUENGER ist als ihr
    letzter Erfolg - ein alter Fehler, auf den ein Erfolg folgte, ist erledigt."""
    _, _, orch = _fresh()
    _healthy(orch)
    text = "\n".join(orch._format_live_signal())
    check("gesunde Quelle bekommt ✅", "news_rss ✅" in text)
    check("gestoerte Quelle bekommt ⚠️", "truth_social ⚠️" in text)


def test_live_signal_nur_morgens():
    _, _, orch = _fresh()
    _healthy(orch)

    sent = _capture(orch)
    _at_hour(orch, 8)
    asyncio.run(orch._maybe_send_daily_depot_update())
    check("morgens: Depot-Update kommt", len(sent) == 1)
    check("morgens: Live-Signal ist dabei", sent and "Live-Signal" in sent[0])
    check("morgens: Laufzeit wird genannt", sent and "Läuft seit" in sent[0])

    sent = _capture(orch)
    _at_hour(orch, 20)
    asyncio.run(orch._maybe_send_daily_depot_update())
    check("abends: Depot-Update kommt", len(sent) == 1)
    check("abends: KEIN Live-Signal (keine taegliche Wiederholung)",
          sent and "Live-Signal" not in sent[0])


def test_abschaltbar():
    _, _, orch = _fresh(ENABLE_DAILY_LIVE_SIGNAL="false")
    _healthy(orch)
    sent = _capture(orch)
    _at_hour(orch, 8)
    asyncio.run(orch._maybe_send_daily_depot_update())
    check("deaktiviert: Depot-Update kommt weiterhin", len(sent) == 1)
    check("deaktiviert: aber ohne Live-Signal", sent and "Live-Signal" not in sent[0])


def test_nur_einmal_pro_fenster():
    _, _, orch = _fresh()
    _healthy(orch)
    sent = _capture(orch)
    _at_hour(orch, 8)
    asyncio.run(orch._maybe_send_daily_depot_update())
    asyncio.run(orch._maybe_send_daily_depot_update())
    check("zweiter Aufruf im selben Fenster schickt nichts nach", len(sent) == 1)


def main():
    test_dauer_formatierung()
    test_quellen_status_erkennt_stoerung()
    test_live_signal_nur_morgens()
    test_abschaltbar()
    test_nur_einmal_pro_fenster()

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
