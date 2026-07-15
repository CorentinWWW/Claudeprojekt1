"""Tests gegen stille Ausfaelle: die einmalige Telegram-Notiz beim Erreichen des
Tages-Kostendeckels (ein stummgeschalteter Monitor darf nicht wie ein ruhiger
Nachrichtentag aussehen) und das Durchschlagen permanenter Claude-Konfigfehler
(kaputter API-Key/geloeschtes Modell -> roter GitHub-Actions-Lauf statt ewig
gruen-aber-tot)."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

failures = []


def check(name, condition):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _fresh_modules():
    import importlib
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    import app.orchestrator as orch
    importlib.reload(orch)
    return tmp.name, db, orch


def test_try_claim_meta_key():
    tmp_name, db, _ = _fresh_modules()
    check("meta-Key: erster Claim gewinnt", db.try_claim_meta_key("cap_notice_2026-07-14") is True)
    check("meta-Key: zweiter Claim desselben Keys verliert", db.try_claim_meta_key("cap_notice_2026-07-14") is False)
    check("meta-Key: anderer Key (naechster Tag) gewinnt wieder", db.try_claim_meta_key("cap_notice_2026-07-15") is True)
    os.unlink(tmp_name)


def test_cap_notice_sent_exactly_once():
    tmp_name, db, orch = _fresh_modules()

    async def fake_classify(text, recent_context=None, priority=False, _bypass_daily_cap=False):
        raise orch.DailyCapExceeded("Tages-Limit erreicht (Test)")

    sent = []

    async def fake_send_text(text):
        sent.append(text)
        return True

    orig_classify, orig_send = orch.classify, orch.send_text
    orch.classify = fake_classify
    orch.send_text = fake_send_text
    try:
        async def run():
            sem = asyncio.Semaphore(1)
            for i in range(3):
                raw = db.RawStatement(source="test", source_id=f"cap-{i}", text=f"Meldung {i}")
                result = await orch._classify_and_store(raw, sem, [])
                assert result is None
        asyncio.run(run())
    finally:
        orch.classify, orch.send_text = orig_classify, orig_send

    check("Cap-Notiz: trotz 3 gecappter Statements genau EINE Telegram-Notiz", len(sent) == 1)
    check("Cap-Notiz: Text nennt das Limit und die Stellschraube",
          sent and "Tages-Limit" in sent[0] and "MAX_CLASSIFICATIONS_PER_DAY" in sent[0])
    os.unlink(tmp_name)


class _FakeSource:
    name = "fake_permanent"

    def __init__(self, db):
        self._db = db

    async def poll(self):
        return [self._db.RawStatement(source=self.name, source_id="perm-1", text="Testmeldung")]


def _make_auth_error():
    import httpx
    from anthropic import AuthenticationError
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request)
    return AuthenticationError(message="invalid x-api-key", response=response, body=None)


def test_permanent_error_propagates():
    from anthropic import AuthenticationError
    tmp_name, db, orch = _fresh_modules()
    orch.source_health["fake_permanent"] = {
        "last_poll_at": None, "last_success_at": None,
        "last_error": None, "last_error_at": None, "total_fetched": 0,
    }

    async def fake_classify(text, recent_context=None, priority=False, _bypass_daily_cap=False):
        raise _make_auth_error()

    orig_classify = orch.classify
    orch.classify = fake_classify
    try:
        async def run():
            try:
                await orch.poll_once([_FakeSource(db)], asyncio.Semaphore(1))
                return False
            except AuthenticationError:
                return True
        raised = asyncio.run(run())
    finally:
        orch.classify = orig_classify

    check("permanenter Fehler (401): poll_once wirft ihn weiter (-> roter Actions-Lauf)", raised)
    os.unlink(tmp_name)


def test_transient_error_does_not_propagate():
    tmp_name, db, orch = _fresh_modules()
    orch.source_health["fake_permanent"] = {
        "last_poll_at": None, "last_success_at": None,
        "last_error": None, "last_error_at": None, "total_fetched": 0,
    }

    async def fake_classify(text, recent_context=None, priority=False, _bypass_daily_cap=False):
        raise RuntimeError("transienter Fehler (Test)")

    orig_classify = orch.classify
    orch.classify = fake_classify
    try:
        async def run():
            await orch.poll_once([_FakeSource(db)], asyncio.Semaphore(1))
            return True
        completed = asyncio.run(run())
    finally:
        orch.classify = orig_classify

    check("transienter Fehler: poll_once laeuft normal durch (kein roter Lauf)", completed)
    os.unlink(tmp_name)


def main():
    test_try_claim_meta_key()
    test_cap_notice_sent_exactly_once()
    test_permanent_error_propagates()
    test_transient_error_does_not_propagate()

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
