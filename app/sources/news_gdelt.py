"""GDELT DOC 2.0 API: kostenlos, kein API-Key, aktualisiert im ~15-Minuten-Takt.

Limitation: GDELT liefert Artikel-Titel, nicht das woertliche Zitat. Das reicht als
Signal ("worueber berichten Medien gerade im Marktkontext"), ersetzt aber keine
woertliche Aussage. Fuer woertliche Trump-Zitate siehe truth_social.py.
"""
import datetime
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.db import RawStatement
from app.sources.base import Source
from app.util import BoundedSeenSet, parse_compact_utc_epoch, retry_async

logger = logging.getLogger(__name__)

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Bewusst KEIN Personen-/Themenfilter (z.B. "Trump") mehr - deckt allgemein alle
# marktrelevanten Nachrichten ab, unabhaengig davon wer/was sie ausloest (Politiker,
# Zentralbanken, Unternehmen, Wirtschaftsdaten, Geopolitik). Die eigentliche
# Praezision entsteht nicht hier, sondern nachgelagert durch die strenge
# Claude-Klassifikation + den Tages-Kostendeckel (siehe app/config.py:
# MAX_CLASSIFICATIONS_PER_DAY) - der begrenzt die Kosten unabhaengig davon, wie viel
# Rohmaterial hier hereinkommt.
MARKET_KEYWORDS = (
    "tariff OR tariffs OR stock OR stocks OR market OR markets OR \"Federal Reserve\" "
    "OR \"interest rate\" OR economy OR trade OR sanctions OR shares OR earnings OR "
    "\"Wall Street\" OR acquisition OR merger OR takeover OR bankruptcy OR layoffs OR "
    "downgrade OR upgrade OR \"guidance\" OR recall OR \"central bank\" OR inflation OR "
    "recession OR IPO"
)


# GDELT deckelt eine einzelne DOC-2.0-Abfrage hart auf 250 Treffer - es gibt KEINE
# Offset-/Seiten-Parameter, um darueber hinauszukommen. Fuer den Live-Poll (kurzes
# Zeitfenster, MAXRECORDS=75) ist das nie relevant, aber fuer historische Abfragen
# ueber laengere Zeitraeume (siehe fetch_range/app/backtest.py) heisst das: an einem
# nachrichtenreichen Tag koennen mehr als 250 passende Artikel schlicht nicht alle
# abgeholt werden. Der Backtest chunked deshalb in kurze Zeitfenster (Standard:
# taeglich), um das Risiko zu verkleinern, nicht zu eliminieren - das ist eine
# dokumentierte, nicht wegprogrammierbare Grenze der kostenlosen API.
GDELT_MAX_RECORDS_PER_QUERY = 250

# Ein sprechender User-Agent statt des httpx-Defaults ("python-httpx/0.x"). Zwei
# Gruende: (a) es ist schlicht hoeflich, sich gegenueber einer kostenlosen API zu
# identifizieren, (b) generische Client-Defaults sind ein gaengiges Signal fuer
# automatisierten Traffic und werden von Anbietern haerter gedrosselt/gesperrt als
# identifizierte Clients. Kein Versuch, sich als Browser zu tarnen - der Bot gibt
# ehrlich an, was er ist.
GDELT_USER_AGENT = (
    "MarketImpactPredictor/1.0 (hobby news-monitoring bot; "
    "https://github.com/CorentinWWW/Claudeprojekt1)"
)
GDELT_HEADERS = {"User-Agent": GDELT_USER_AGENT}

# --- Rate-Limit-Schutzschalter (Circuit Breaker) --------------------------------------
# GDELT antwortet auf Ueberlastung mit HTTP 429 ("Please limit requests to one every 5
# seconds"). Live beobachtet: sobald diese Sperre einmal greift, liefert sie auch fuer
# EINZELNE, weit auseinanderliegende Anfragen (>15 Minuten Abstand, simples query=stock)
# weiterhin 429 - es ist also keine reine Frequenz-Drosselung, sondern eine laenger
# anhaltende IP-seitige Sperre.
#
# Ohne Schutzschalter fragt der Live-Poll trotzdem in JEDEM Zyklus rund um die Uhr
# weiter an und produziert dabei nichts ausser Fehlern und Log-Rauschen - im besten Fall
# nutzlos, im schlechtesten verlaengert es die Sperre. Nach einem 429 wird die Quelle
# deshalb fuer eine wachsende Zeitspanne komplett stillgelegt und meldet das sichtbar
# (siehe Source.last_failure), statt es still zu tun. Ein einziger Erfolg setzt alles
# zurueck.
GDELT_RATE_LIMIT_COOLDOWNS_SECONDS = (15 * 60, 30 * 60, 60 * 60, 120 * 60)


def _parse_gdelt_response(data, seen: set) -> list[RawStatement]:
    """Gemeinsamer Parser fuer Live-Poll UND historische Abfragen (app/backtest.py).
    `seen` wird sowohl gelesen (Duplikate ueberspringen) als auch befuellt (Aufrufer
    entscheidet, ob das ein langlebiges BoundedSeenSet oder ein einmaliges set() fuer
    einen einzelnen Backtest-Chunk ist).

    Bewusst innerhalb dieser Funktion keine eigene Fehlerbehandlung: GDELT liefert bei
    manchen Rand-/Fehlerfaellen valides JSON, das aber nicht die erwartete Dict-
    Struktur hat (z.B. eine Fehlermeldung als Liste/String) - das darf hier nicht mit
    einem AttributeError abschiessen, die Aufrufer faengt das ab."""
    articles = data.get("articles", []) or [] if isinstance(data, dict) else []
    results: list[RawStatement] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        source_id = art.get("url", "")
        if not source_id or source_id in seen:
            continue

        title = (art.get("title") or "").strip()
        if not title:
            # NICHT als gesehen markieren: GDELT kann einen Artikel schon gelistet
            # haben, bevor der Titel indexiert ist - wuerde man ihn trotzdem als
            # "gesehen" vermerken, waere er dauerhaft uebersprungen, selbst wenn ein
            # spaeterer Poll den (dann befuellten) Titel liefert.
            continue
        seen.add(source_id)

        # GDELT liefert pro Artikel ein 'seendate' (Zeitpunkt, zu dem GDELT den Artikel
        # gesehen hat, ~Veroeffentlichungszeit) - deutlich aussagekraeftiger als der
        # Ingest-Zeitpunkt. Fallback auf jetzt, falls Feld fehlt/kaputt.
        published_at = parse_compact_utc_epoch(art.get("seendate")) or time.time()
        results.append(
            RawStatement(
                source="news_gdelt",
                source_id=source_id,
                text=f"{title} (Quelle: {art.get('domain', 'unbekannt')})",
                url=art.get("url"),
                published_at=published_at,
            )
        )
    return results


async def fetch_range(
    start: datetime.datetime, end: datetime.datetime, client: Optional[httpx.AsyncClient] = None
) -> list[RawStatement]:
    """Historische GDELT-Abfrage fuer einen festen Zeitraum (statt des rollierenden
    `timespan`-Fensters von poll()) - fuer app/backtest.py. `start`/`end` muessen
    UTC-aware sein.

    WICHTIG (ehrlich, nicht beworben): Ob und wie weit GDELTs DOC-2.0-API tatsaechlich
    rueckwirkend Daten liefert, ist von hier aus nicht verifizierbar (Netzsperre in der
    Entwicklungsumgebung) und oeffentlich nicht mit letzter Sicherheit dokumentiert -
    es kursieren unterschiedliche Angaben zum Umfang des Suchfensters. Diese Funktion
    behauptet NICHTS ueber die Reichweite; leere Ergebnisse fuer einen weit
    zurueckliegenden Zeitraum sind das ehrliche Signal, dass GDELT dafuer nichts (mehr)
    hat - kein Bug. Der Dry-Run in app/backtest.py macht genau das sichtbar, bevor
    irgendein Claude-Call bezahlt wird.

    Rate-Limit: GDELT nennt es explizit in der eigenen 429-Antwort - "Please limit
    requests to one every 5 seconds" (Stand: live gegengeprueft, kein Cloudflare-/
    WAF-Block, ein ganz normaler serverseitiger Deckel). Die vielen kurz
    aufeinanderfolgenden Tages-Abfragen eines Backtests reissen dieses Limit leicht,
    wenn Aufrufer nicht bewusst Abstand halten - sowohl per HTTP 429 als auch
    (seltener) per leerem/nicht als JSON lesbarem 200er-Body unter Last. Anders als
    beim rollierenden Live-Poll (dort loest sich ein 429 von selbst beim naechsten
    60s-Zyklus, siehe poll()) gibt es hier keinen spaeteren Versuch - schlaegt eine
    Tages-Abfrage fehl, ist dieser Tag fuer den
    gesamten Lauf verloren. Deshalb wird HIER (nicht bei poll()) mit deutlich mehr
    Geduld retried."""
    def _fmt(dt: datetime.datetime) -> str:
        return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")

    params = {
        "query": f'({MARKET_KEYWORDS}) sourcelang:english',
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(GDELT_MAX_RECORDS_PER_QUERY),
        "sort": "DateAsc",  # chronologisch - passend fuer einen Backtest-Replay
        "startdatetime": _fmt(start),
        "enddatetime": _fmt(end),
    }
    url = f"{GDELT_ENDPOINT}?{urlencode(params)}"

    async def _fetch():
        if client is not None:
            resp = await client.get(url, headers=GDELT_HEADERS)
        else:
            async with httpx.AsyncClient(timeout=30, headers=GDELT_HEADERS) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        return resp.json()

    data = await retry_async(
        _fetch, retries=5, backoff_seconds=6.0,
        retry_on=(httpx.TransportError, httpx.HTTPStatusError, json.JSONDecodeError),
    )
    return _parse_gdelt_response(data, seen=set())


class GdeltNewsSource(Source):
    name = "news_gdelt"

    def __init__(self):
        self._seen: BoundedSeenSet = BoundedSeenSet(maxlen=5000)
        # Schutzschalter-Zustand (siehe GDELT_RATE_LIMIT_COOLDOWNS_SECONDS).
        self._blocked_until: float = 0.0
        self._rate_limit_strikes: int = 0

    def _enter_cooldown(self) -> float:
        """Legt die Quelle nach einem 429 fuer eine wachsende Zeitspanne still und gibt
        die gewaehlte Dauer in Sekunden zurueck."""
        idx = min(self._rate_limit_strikes, len(GDELT_RATE_LIMIT_COOLDOWNS_SECONDS) - 1)
        cooldown = GDELT_RATE_LIMIT_COOLDOWNS_SECONDS[idx]
        self._rate_limit_strikes += 1
        self._blocked_until = time.time() + cooldown
        return cooldown

    def _reset_cooldown(self) -> None:
        self._blocked_until = 0.0
        self._rate_limit_strikes = 0

    async def poll(self) -> list[RawStatement]:
        remaining = self._blocked_until - time.time()
        if remaining > 0:
            # Bewusst ueber note_failure sichtbar gemacht statt still zu ueberspringen -
            # sonst waere eine pausierte Quelle im Dashboard/Live-Signal von "gerade
            # keine passenden Nachrichten" nicht zu unterscheiden (genau die Luecke, die
            # Source.last_failure schliessen soll).
            self.last_failure = (
                f"GDELT-Rate-Limit: Abfragen pausiert fuer noch {remaining / 60:.0f} Min "
                f"(nach {self._rate_limit_strikes} Sperre(n) in Folge). Die Sperre kommt "
                "von GDELT selbst und loest sich nur durch Abwarten."
            )
            logger.info(
                "[news_gdelt] Rate-Limit-Pause aktiv, ueberspringe Poll (noch %.0f Min).",
                remaining / 60,
            )
            return []

        params = {
            # sourcelang:english schraenkt auf englischsprachige Artikel ein - GDELT
            # deckt Nachrichten global in vielen Sprachen ab, und dieselbe Aussage wird
            # oft von Dutzenden Outlets in unterschiedlichen Sprachen (uebersetzt/
            # umformuliert) gemeldet. Die Text-Duplikaterkennung (Tier 1, difflib) UND
            # der Themen-Abgleich innerhalb einer Charge (siehe orchestrator.py:
            # _partition_duplicates) vergleichen nur auf Zeichenebene und koennen
            # ueber Sprachgrenzen hinweg keine Duplikate erkennen - ohne dieses Filter
            # kam dieselbe Meldung dadurch wiederholt als "neues" Statement durch.
            "query": f'({MARKET_KEYWORDS}) sourcelang:english',
            "mode": "ArtList",
            "format": "json",
            "maxrecords": "75",
            "sort": "DateDesc",
            "timespan": "3h",
        }
        url = f"{GDELT_ENDPOINT}?{urlencode(params)}"

        async def _fetch():
            async with httpx.AsyncClient(timeout=20, headers=GDELT_HEADERS) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()

        try:
            # Nur Verbindungsfehler (DNS/Timeout/Reset) werden wiederholt - ein
            # HTTP-Statusfehler wie 429/403 wuerde sich innerhalb weniger Sekunden
            # ohnehin nicht aendern, das erledigt der naechste Poll-Zyklus.
            data = await retry_async(_fetch, retries=2, backoff_seconds=2.0, retry_on=(httpx.TransportError,))
            result = _parse_gdelt_response(data, seen=self._seen)
            # Erfolg hebt eine zuvor verhaengte Pause vollstaendig auf - eine einmalige
            # Sperre soll die Quelle nicht dauerhaft auf langen Cooldowns halten.
            self._reset_cooldown()
            return result
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                cooldown = self._enter_cooldown()
                # Kein logger.exception(): ein Rate-Limit ist ein erwarteter Zustand
                # dieser kostenlosen API, kein Programmfehler - ein voller Traceback pro
                # Zyklus waere reines Log-Rauschen.
                logger.warning(
                    "[news_gdelt] Von GDELT rate-limited (429) - pausiere Abfragen fuer "
                    "%.0f Minuten, statt weiter anzufragen.", cooldown / 60,
                )
                self.last_failure = (
                    f"GDELT-Rate-Limit (429): Abfragen fuer {cooldown / 60:.0f} Min "
                    "pausiert, um die Sperre nicht zu verlaengern. Loest sich durch "
                    "Abwarten - siehe README (Rate-Limit)."
                )
                return []
            logger.exception("GDELT-Abfrage fehlgeschlagen")
            self.note_failure(exc)
            return []
        except Exception as exc:
            logger.exception("GDELT-Abfrage fehlgeschlagen")
            # Ohne diese Meldung waere ein dauerhaft kaputtes GDELT (Endpoint
            # geaendert, Netzsperre, Rate-Limit) von "keine passenden Nachrichten"
            # nicht unterscheidbar - siehe Source.last_failure.
            self.note_failure(exc)
            return []
