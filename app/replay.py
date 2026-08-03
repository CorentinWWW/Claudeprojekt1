"""Replay: wertet die BEREITS vorhandenen Claude-Klassifikationen aus der Produktions-
Datenbank gegen echte Kursverlaeufe aus - ohne einen einzigen neuen Claude-Call und
ohne GDELT.

Warum das dem Backtest (app/backtest.py) ueberlegen ist, wo es geht:

1. Kosten: null. Die teure Arbeit (Claude-Klassifikation) hat der Live-Bot fuer jedes
   gespeicherte Statement laengst erledigt und bezahlt. Ausgewertet wurden bisher aber
   nur die wenigen Statements, die durch ALLE Alarm-Gates kamen (alert_outcomes) - die
   grosse Mehrheit der Klassifikationen lag ungenutzt herum.

2. Ehrlichkeit: das ist echtes Out-of-Sample. Die Einschaetzungen wurden LIVE getroffen,
   bevor sich der Kurs bewegt hat. Ein klassischer Backtest klassifiziert alte
   Nachrichten mit dem heutigen Modell und traegt damit ein Lookahead-Risiko (das Modell
   koennte den Ausgang aus seinem Training kennen). Dieses Risiko gibt es hier
   prinzipiell nicht.

3. Zusatzfragen, die der Backtest nicht beantworten kann:
   - Korreliert Claudes Konfidenz ueberhaupt mit der Trefferquote? Hier liegen
     Ticker-Calls ueber die GANZE Konfidenz-Spanne vor, nicht nur die, die das
     ALERT_MIN_TICKER_CONFIDENCE-Gate passiert haben.
   - Bringen die Alarm-Gates etwas? alert_sent trennt durchgelassene von unterdrueckten
     Signalen - schneiden die Unterdrueckten gleich gut ab, wirft das System gute
     Signale weg.

Grenzen (ehrlich): reicht nur so weit zurueck wie der Bot laeuft, nur die tatsaechlich
genutzten Quellen, und wie beim Backtest nur TAGES-Schlusskurse (keine historischen
Intraday-Kurse aus kostenlosen Quellen) - der Horizont wird also in Handelstagen
gemessen, nicht in Minuten wie im Live-Betrieb.

Sicherheit: die Produktions-DB wird STRIKT LESEND geoeffnet (SQLite-URI mit mode=ro).
Ein Schreibversuch wuerde von SQLite selbst abgelehnt - der laufende Bot kann durch
diesen Auswertungslauf nicht gestoert werden.

Aufruf: python -m app.replay --db-path /data/trump_monitor.db
"""
import argparse
import asyncio
import json
import logging
import sqlite3
from typing import Optional

from app import prices
from app.backtest import TickerOutcome, _bucket_summary, _tier_breakdown, evaluate_ticker_outcomes_multi

logger = logging.getLogger(__name__)

# Grenzen bewusst um die Live-Alarmschwelle (ALERT_MIN_TICKER_CONFIDENCE, Standard 0.90)
# herum gelegt: nur so wird sichtbar, ob die Schwelle tatsaechlich dort liegt, wo die
# Trefferquote springt - oder ob sie willkuerlich ist.
CONFIDENCE_BUCKETS = (
    ("<70%", 0.0, 0.70),
    ("70-80%", 0.70, 0.80),
    ("80-90%", 0.80, 0.90),
    ("90-95%", 0.90, 0.95),
    ("95-100%", 0.95, 1.01),
)


def _confidence_bucket(conf: Optional[float]) -> str:
    if not isinstance(conf, (int, float)):
        return "unbekannt"
    for name, lo, hi in CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return name
    return "unbekannt"


def load_classified_statements(db_path: str) -> list[dict]:
    """Liest alle klassifizierten, nicht-doppelten Statements mit Ticker-Einschaetzung.

    STRIKT LESEND (mode=ro): dieser Auswertungslauf darf die Datenbank des laufenden
    Bots unter keinen Umstaenden veraendern. mode=ro laesst SQLite jeden Schreibversuch
    ablehnen - das ist eine harte Garantie, kein blosses Versprechen im Kommentar.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, published_at, ingested_at, confidence, tickers, sentiment,
                   expected_move_pct, alert_sent
            FROM statements
            WHERE is_market_relevant = 1
              AND tickers IS NOT NULL
              AND duplicate_of_id IS NULL
            ORDER BY published_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        try:
            ticker_calls = json.loads(row["tickers"]) or []
        except (TypeError, ValueError):
            continue
        if not isinstance(ticker_calls, list) or not ticker_calls:
            continue
        # published_at ist der Zeitpunkt der Veroeffentlichung; fehlt er (oder ist er
        # unbrauchbar), faellt das Statement raus statt auf ingested_at auszuweichen -
        # der Ingest-Zeitpunkt kann Stunden spaeter liegen und wuerde den Einstiegstag
        # verfaelschen.
        published_at = row["published_at"]
        if not isinstance(published_at, (int, float)) or published_at <= 0:
            continue
        out.append({
            "id": row["id"],
            "published_at": published_at,
            "confidence": row["confidence"],
            "ticker_calls": ticker_calls,
            "sentiment": row["sentiment"],
            "expected_move_pct": row["expected_move_pct"],
            "alert_sent": bool(row["alert_sent"]),
        })
    return out


async def run_replay(
    db_path: str,
    horizon_days=(1, 3, 5, 10),
    min_confidence: float = 0.0,
    max_tickers: int = 400,
) -> dict:
    """Wertet die vorhandenen Klassifikationen gegen echte Kursverlaeufe aus.

    min_confidence filtert Ticker-Calls unterhalb einer Pro-Ticker-Konfidenz heraus
    (Standard 0 = ALLE auswerten, gerade damit die Konfidenz-Aufschluesselung ueberhaupt
    aussagekraeftig ist). max_tickers deckelt die Anzahl verschiedener Ticker, fuer die
    Kurshistorie geholt wird - schuetzt vor einem sehr langen Lauf, wenn die Datenbank
    hunderte verschiedener Kuerzel enthaelt.
    """
    horizons = sorted({int(h) for h in horizon_days}) or [3]
    primary = horizons[0]

    statements = load_classified_statements(db_path)
    report: dict = {
        "db_path": db_path,
        "horizon_days": horizons,
        "statements_klassifiziert": len(statements),
    }
    if not statements:
        report["note"] = (
            "Keine auswertbaren Statements gefunden (keine marktrelevanten Eintraege mit "
            "Ticker-Einschaetzung und brauchbarem Veroeffentlichungszeitpunkt)."
        )
        return report

    # Alle (Statement, Ticker)-Paare einsammeln, die eine klare Richtung haben.
    pairs = []
    for st in statements:
        for tc in st["ticker_calls"]:
            if not isinstance(tc, dict) or tc.get("direction") not in ("long", "short"):
                continue
            conf = tc.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            if conf < min_confidence:
                continue
            ticker = (tc.get("ticker") or "").upper()
            if ticker:
                pairs.append({
                    "ticker": ticker, "direction": tc["direction"], "confidence": conf,
                    "published_at": st["published_at"], "alert_sent": st["alert_sent"],
                })
    report["ticker_calls_gesamt"] = len(pairs)
    if not pairs:
        report["note"] = "Keine Ticker-Einschaetzungen mit klarer Long/Short-Richtung."
        return report

    tickers = sorted({p["ticker"] for p in pairs})
    report["verschiedene_ticker"] = len(tickers)
    if len(tickers) > max_tickers:
        logger.warning(
            "%d verschiedene Ticker, deckle auf %d (siehe --max-tickers).",
            len(tickers), max_tickers,
        )
        tickers = tickers[:max_tickers]
        allowed = set(tickers)
        pairs = [p for p in pairs if p["ticker"] in allowed]

    history: dict[str, Optional[dict]] = {}
    for t in tickers:
        history[t] = await prices.get_history(t)
    report["ticker_mit_kurshistorie"] = sum(1 for h in history.values() if h)

    outcomes_by_horizon: dict[int, list[TickerOutcome]] = {h: [] for h in horizons}
    # Parallel mitgefuehrt, weil alert_sent kein Feld von TickerOutcome ist (das gehoert
    # zum Statement, nicht zur Kursauswertung) - ueber den Index den Outcomes zugeordnet.
    alert_flags: dict[int, list[bool]] = {h: [] for h in horizons}

    for p in pairs:
        per_horizon = evaluate_ticker_outcomes_multi(
            p["ticker"], p["direction"], p["confidence"], p["published_at"],
            history.get(p["ticker"]), horizons,
        )
        for h, outcome in per_horizon.items():
            outcomes_by_horizon[h].append(outcome)
            alert_flags[h].append(p["alert_sent"])

    primary_outcomes = outcomes_by_horizon[primary]
    report["ausgewertet"] = len(primary_outcomes)
    report["nicht_auswertbar"] = len(pairs) - len(primary_outcomes)

    if not primary_outcomes:
        report["note"] = (
            "Keine Ergebnisse auswertbar - meist fehlende Kurshistorie oder ein "
            "Veroeffentlichungsdatum, das noch keine "
            f"{primary} Handelstage zurueckliegt."
        )
        return report

    caps = await prices.get_market_caps(sorted({o.ticker for o in primary_outcomes}))
    for items in outcomes_by_horizon.values():
        for o in items:
            o.market_cap_tier = prices.market_cap_tier(caps.get(o.ticker))

    report["gesamt"] = _bucket_summary(primary_outcomes)
    if len(horizons) > 1:
        report["nach_horizont"] = {
            str(h): _bucket_summary(items) for h, items in outcomes_by_horizon.items() if items
        }
    report["nach_marktkapitalisierung"] = _tier_breakdown(primary_outcomes)

    # Die eigentlich interessante Frage: steigt die Trefferquote mit der Konfidenz?
    by_conf: dict[str, list[TickerOutcome]] = {}
    for o in primary_outcomes:
        by_conf.setdefault(_confidence_bucket(o.confidence), []).append(o)
    report["nach_konfidenz"] = {k: _bucket_summary(v) for k, v in by_conf.items()}

    # Und: haben die Alarm-Gates die besseren Signale erwischt?
    alerted = [o for o, flag in zip(primary_outcomes, alert_flags[primary]) if flag]
    suppressed = [o for o, flag in zip(primary_outcomes, alert_flags[primary]) if not flag]
    report["alarmiert_vs_unterdrueckt"] = {
        "alarmiert": _bucket_summary(alerted) if alerted else {"n": 0},
        "unterdrueckt": _bucket_summary(suppressed) if suppressed else {"n": 0},
    }
    return report


def _parse_horizons(value: str) -> list[int]:
    try:
        out = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Ungueltige Horizont-Liste: {value!r}") from exc
    if not out:
        raise argparse.ArgumentTypeError("--horizon-days darf nicht leer sein")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Wertet die bereits vorhandenen Claude-Klassifikationen der Produktions-DB "
            "gegen echte Kursverlaeufe aus. Kostet KEINE Claude-Calls und oeffnet die "
            "Datenbank strikt lesend."
        )
    )
    parser.add_argument("--db-path", required=True, help="Pfad zur Produktions-SQLite-Datei")
    parser.add_argument("--horizon-days", type=_parse_horizons, default=[1, 3, 5, 10],
                        help="Kommagetrennte Handelstage-Horizonte (Standard '1,3,5,10')")
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="Ticker-Calls unter dieser Konfidenz ignorieren (Standard 0 = alle, "
                             "damit die Konfidenz-Aufschluesselung aussagekraeftig bleibt)")
    parser.add_argument("--max-tickers", type=int, default=400,
                        help="Obergrenze fuer verschiedene Ticker mit Kursabruf (Standard 400)")
    parser.add_argument("--out", default=None, help="Report zusaetzlich als JSON-Datei schreiben")
    args = parser.parse_args()

    report = asyncio.run(run_replay(
        args.db_path, horizon_days=args.horizon_days,
        min_confidence=args.min_confidence, max_tickers=args.max_tickers,
    ))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nReport geschrieben nach {args.out}")


if __name__ == "__main__":
    main()
