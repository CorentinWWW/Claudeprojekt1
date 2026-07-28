"""Best-effort Kursabfrage ueber eine kostenlose, key-freie Quelle (Stooq) - fuer das
Ergebnis-Tracking/Backtesting (#2), die Konfidenz-Kalibrierung (#3) und die heutige
Bewegung im Alert (#8).

Bewusst best-effort: schlaegt die Abfrage fehl (Netz nicht erreichbar, Rate-Limit,
unbekanntes Symbol), wird None zurueckgegeben und das jeweilige Feature entfaellt
still - der Poll-Zyklus darf daran NIE scheitern. Deshalb ist das gesamte Preis-Tracking
per ENABLE_PRICE_TRACKING standardmaessig aus.
"""
import logging
import math
import time
from typing import Optional

import httpx

from app import config

logger = logging.getLogger(__name__)

# Stooq-Live-Quote als CSV, ohne API-Key:
#   https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv
# Antwort (mit Header): Symbol,Date,Time,Open,High,Low,Close,Volume
_STOOQ_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

# Stooq-Tageshistorie als CSV, ohne API-Key:
#   https://stooq.com/q/d/l/?s=aapl.us&i=d
# Antwort (mit Header): Date,Open,High,Low,Close,Volume (aelteste Zeile zuerst)
_STOOQ_HISTORY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"

# Fallback-Kursquelle (#Kursausfaelle), falls Stooq keinen Kurs liefert: Yahoo Finance's
# inoffizielle Chart-API, ebenfalls kostenlos und ohne Key. Live beobachtet: ein
# vorboerslicher Alert fuer RKLB (kleinerer/juengerer NASDAQ-Wert) bekam trotz gueltigem
# Ticker keine Paper-Position, weil Stooqs kostenloser Feed vorboerslich fuer solche
# Werte oft noch kein frisches 'close' fuer den Tag hat ('N/D'). Yahoo deckt Vor-/
# Nachboerslich zuverlaessiger ab (siehe parse_yahoo_quote_json: preMarketPrice/
# postMarketPrice werden bevorzugt vor dem regulaeren Kurs verwendet).
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Marktkapitalisierung (#Small-Cap-Fokus): der Chart-Endpunkt oben liefert kein
# marketCap-Feld, dafuer aber Yahoos Batch-Quote-Endpunkt - EIN Call fuer beliebig
# viele Ticker (Komma-getrennt), statt N Einzelabfragen. Vor allem fuer den Backtest
# gedacht (app/backtest.py): dort werden viele Ticker auf einmal ausgewertet und in
# Groessen-Klassen (Small/Mid/Large-Cap) aufgeschluesselt, um die These "Small-Caps
# haben weniger Verzoegerungs-Nachteil" mit echten Daten zu pruefen statt nur zu
# behaupten.
_YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

# --- Kurz-Cache + Circuit-Breaker (#13) -------------------------------------------
# Cache: dieselbe Ticker-Quote wird innerhalb von PRICE_CACHE_TTL_SECONDS nicht erneut
# vom Kursdienst geholt (spart HTTP-Calls, wenn ein Ticker in einem Zyklus mehrfach
# vorkommt - z.B. bei Baseline-Erfassung und Anzeige). Circuit-Breaker: nach mehreren
# aufeinanderfolgenden Fehlern kurz gar nicht mehr anfragen, statt einen down/rate-limited
# Kursdienst bei jedem Ticker erneut zu hammern.
_QUOTE_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
# Getrennter Cache fuer die (groessere, sich langsam aendernde) Tageshistorie.
_HISTORY_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_breaker = {"consecutive_failures": 0, "open_until": 0.0}
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 120.0


def clear_cache() -> None:
    """Setzt Cache + Circuit-Breaker zurueck (fuer Tests / manuellen Reset)."""
    _QUOTE_CACHE.clear()
    _HISTORY_CACHE.clear()
    _breaker["consecutive_failures"] = 0
    _breaker["open_until"] = 0.0


def suggest_risk_levels(
    price: Optional[float],
    direction: Optional[str],
    day_high: Optional[float] = None,
    day_low: Optional[float] = None,
) -> Optional[dict]:
    """Grobe, UNVERBINDLICHE Stop-Loss-/Take-Profit-Marken (#5) aus der heutigen
    Tagesspanne als einfachem Volatilitaets-Mass (kein ATR/Historie noetig). Stop = eine
    Tagesspanne entfernt, Ziel = das 1.5-fache in Richtung des Trades (Chance-Risiko ~1.5).
    Faellt die Spanne aus (fehlend/0), wird ersatzweise 1.5% des Kurses genommen. None,
    wenn kein gueltiger Kurs/Richtung vorliegt. Ausdruecklich keine Anlageberatung."""
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return None
    if direction not in ("long", "short"):
        return None
    span = None
    if (
        isinstance(day_high, (int, float)) and isinstance(day_low, (int, float))
        and math.isfinite(day_high) and math.isfinite(day_low) and day_high > day_low
    ):
        span = day_high - day_low
    if not span or span <= 0:
        span = price * 0.015  # Fallback: 1.5% des Kurses
    rr = 1.5
    # Bei sehr volatilen/duennen Titeln kann die Tagesspanne groesser als der Kurs
    # selbst ausfallen. Ungebremst wuerde das den Long-Stop auf <= 0 druecken (ein
    # Kurs kann nie <= 0 werden, also nie erreichbar) bzw. beim Short sogar ein
    # negatives Ziel ergeben (ebenfalls nie erreichbar) - und damit den automatischen
    # Stop-/Ziel-Ausstieg im Paper-Depot (stop_or_target_hit) lautlos dauerhaft
    # deaktivieren, ausgerechnet bei den volatilsten (und damit riskantesten)
    # Positionen. In dem Fall auf denselben prozentualen Fallback zurueckfallen wie
    # bei fehlender/nuller Spanne, statt auf einen unerreichbaren Wert zu klemmen.
    if direction == "long" and span >= price:
        span = price * 0.015
    elif direction == "short" and span >= price / rr:
        span = price * 0.015
    if direction == "long":
        stop = price - span
        target = price + rr * span
    else:
        stop = price + span
        target = price - rr * span
    stop = max(0.0, stop)
    target = max(0.0, target)
    return {"stop": round(stop, 2), "target": round(target, 2), "rr": rr}


def to_stooq_symbol(ticker: str) -> str:
    """US-Ticker -> Stooq-Symbol: Kleinbuchstaben, Klassensuffix mit Bindestrich statt
    Punkt (BRK.B -> brk-b), Suffix '.us'. (BRK.B wird so zu 'brk-b.us'.)"""
    return ticker.strip().lower().replace(".", "-") + ".us"


def parse_stooq_csv(text: str) -> Optional[dict]:
    """Parst die Stooq-CSV-Antwort zu {'price', 'open', 'change_pct'} (change_pct =
    Tagesbewegung Open->Close in Prozentpunkten, oder None). None bei fehlenden/
    ungueltigen Daten ('N/D')."""
    if not isinstance(text, str):
        return None
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    values = [v.strip() for v in lines[1].split(",")]
    if len(values) != len(header):
        return None
    row = dict(zip(header, values))

    def _num(key) -> Optional[float]:
        v = row.get(key, "")
        if not v or v.upper() == "N/D":
            return None
        try:
            parsed = float(v)
        except ValueError:
            return None
        # float() akzeptiert auch "inf"/"-inf"/"nan"/"Infinity" - eine korrupte/
        # unerwartete Kursdienst-Antwort mit so einem Wert soll nicht als gueltiger
        # Kurs durchgehen (sonst koennte im Alert z.B. "heute +inf%" erscheinen).
        return parsed if math.isfinite(parsed) else None

    price = _num("close")
    open_ = _num("open")
    if price is None:
        return None
    change_pct = None
    if open_ is not None and open_ > 0:
        change_pct = (price - open_) / open_ * 100.0
    return {
        "price": price,
        "open": open_,
        "high": _num("high"),
        "low": _num("low"),
        "change_pct": change_pct,
    }


async def _fetch_yahoo_chart_json(
    ticker: str, params: dict, client: Optional[httpx.AsyncClient] = None
) -> Optional[dict]:
    """Gemeinsamer Netzwerk-Kern fuer den Yahoo-Fallback (Quote UND Historie nutzen
    denselben Chart-Endpunkt, nur mit unterschiedlichen range/interval-Parametern).
    Best-effort wie der Rest dieses Moduls: liefert bei jedem Problem None, nie eine
    Exception nach aussen. Bewusst OHNE eigenen Circuit-Breaker - wird nur als zweiter
    Versuch NACH einem Stooq-Fehlschlag aufgerufen (siehe get_quote/get_history), ist
    also inhaerent selten; der Stooq-Breaker schuetzt weiterhin vor haeufigem Hammern
    der PRIMAEREN Quelle."""
    url = _YAHOO_CHART_URL.format(ticker=ticker.strip().upper())
    try:
        if client is not None:
            resp = await client.get(url, params=params, headers=_YAHOO_HEADERS)
        else:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                resp = await c.get(url, params=params, headers=_YAHOO_HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.debug("Yahoo-Fallback-Abfrage fuer %s fehlgeschlagen.", ticker, exc_info=True)
        return None


def parse_yahoo_quote_json(data: Optional[dict]) -> Optional[dict]:
    """Parst die Antwort von query1.finance.yahoo.com/v8/finance/chart/{ticker} zu
    {'price','open','high','low','change_pct'} - dieselbe Form wie parse_stooq_csv(),
    damit Aufrufer die Quelle nicht unterscheiden muessen. Nutzt bevorzugt einen
    tatsaechlichen Vor-/Nachboerslich-Kurs (preMarketPrice/postMarketPrice), falls Yahoo
    einen liefert - genau die Luecke, die Stooq vorboerslich oft nicht deckt."""
    try:
        result = (data or {}).get("chart", {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
    except (AttributeError, TypeError):
        return None

    def _num(key) -> Optional[float]:
        v = meta.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        v = float(v)
        return v if math.isfinite(v) and v > 0 else None

    price = _num("preMarketPrice") or _num("postMarketPrice") or _num("regularMarketPrice")
    if price is None:
        return None
    open_ = _num("regularMarketOpen") or _num("previousClose")
    change_pct = None
    if open_ is not None and open_ > 0:
        change_pct = (price - open_) / open_ * 100.0
    return {
        "price": price,
        "open": open_,
        "high": _num("regularMarketDayHigh"),
        "low": _num("regularMarketDayLow"),
        "change_pct": change_pct,
    }


def parse_yahoo_history_json(data: Optional[dict], max_bars: int = 400) -> Optional[dict]:
    """Parst dieselbe Chart-Antwort zur Tageshistorie - gleiche Form wie
    parse_stooq_history_csv(). Zeitstempel (Unix, meist Handelstag-Mittag/-Ende in UTC)
    werden zu einem reinen Datum (YYYY-MM-DD) verkuerzt, damit previous_close() (Vergleich
    auf ISO-Datumsstrings) unveraendert funktioniert."""
    import datetime as _dt

    try:
        result = (data or {}).get("chart", {}).get("result") or []
        if not result:
            return None
        r0 = result[0]
        timestamps = r0.get("timestamp") or []
        quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    except (AttributeError, TypeError, IndexError):
        return None
    if not timestamps:
        return None

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    if not (len(timestamps) == len(opens) == len(highs) == len(lows) == len(closes)):
        return None

    def _finite(v) -> Optional[float]:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        v = float(v)
        return v if math.isfinite(v) else None

    dates, out_o, out_h, out_l, out_c, out_v = [], [], [], [], [], []
    for i, ts in enumerate(timestamps):
        o, h, l, c = _finite(opens[i]), _finite(highs[i]), _finite(lows[i]), _finite(closes[i])
        if None in (o, h, l, c) or not isinstance(ts, (int, float)):
            continue
        v = _finite(volumes[i]) if i < len(volumes) else None
        dates.append(_dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d"))
        out_o.append(o)
        out_h.append(h)
        out_l.append(l)
        out_c.append(c)
        out_v.append(v if v is not None else 0.0)

    if len(out_c) < 2:
        return None
    return {
        "date": dates[-max_bars:],
        "open": out_o[-max_bars:],
        "high": out_h[-max_bars:],
        "low": out_l[-max_bars:],
        "close": out_c[-max_bars:],
        "volume": out_v[-max_bars:],
    }


def parse_yahoo_market_cap_json(data: Optional[dict]) -> dict[str, float]:
    """Parst die Antwort von query1.finance.yahoo.com/v7/finance/quote zu
    {TICKER: marktkapitalisierung_usd}. Ticker ohne gueltiges marketCap-Feld (z.B.
    unbekanntes Symbol, ETF ohne Market Cap) fehlen im Ergebnis-Dict komplett statt mit
    None drin zu stehen - Aufrufer pruefen einfach per 'in'/.get()."""
    try:
        results = (data or {}).get("quoteResponse", {}).get("result") or []
    except (AttributeError, TypeError):
        return {}
    out: dict[str, float] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        cap = row.get("marketCap")
        if not symbol or not isinstance(cap, (int, float)) or isinstance(cap, bool):
            continue
        cap = float(cap)
        if math.isfinite(cap) and cap > 0:
            out[symbol.upper()] = cap
    return out


async def get_market_caps(
    tickers: list[str], client: Optional[httpx.AsyncClient] = None
) -> dict[str, float]:
    """Marktkapitalisierung (USD) fuer mehrere Ticker in EINEM Batch-Call (Yahoo erlaubt
    Komma-getrennte Symbole). Best-effort wie der Rest dieses Moduls: liefert bei jedem
    Problem ein leeres Dict, nie eine Exception nach aussen. Fehlende Ticker im
    Ergebnis-Dict = keine Daten verfuegbar, nicht zwingend ein Fehler."""
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {}
    params = {"symbols": ",".join(dict.fromkeys(tickers))}  # dedupe, Reihenfolge egal
    try:
        if client is not None:
            resp = await client.get(_YAHOO_QUOTE_URL, params=params, headers=_YAHOO_HEADERS)
        else:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                resp = await c.get(_YAHOO_QUOTE_URL, params=params, headers=_YAHOO_HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.debug("Marktkapitalisierungs-Abfrage fehlgeschlagen (best-effort).", exc_info=True)
        return {}
    return parse_yahoo_market_cap_json(data)


async def get_market_cap(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[float]:
    """Einzel-Ticker-Komfortfunktion um get_market_caps() - fuer Stellen, an denen nur
    ein Ticker gebraucht wird (die Batch-Funktion ist effizienter, wenn mehrere Ticker
    auf einmal ausgewertet werden, siehe app/backtest.py)."""
    if not ticker:
        return None
    caps = await get_market_caps([ticker], client=client)
    return caps.get(ticker.strip().upper())


# Groessen-Klassen fuer die Backtest-Auswertung (grobe, gebraeuchliche US-Markt-
# Konvention - keine Anlageberatung, nur eine Analyse-Kategorisierung).
MARKET_CAP_TIERS = (
    ("micro", 0, 300_000_000),
    ("small", 300_000_000, 2_000_000_000),
    ("mid", 2_000_000_000, 10_000_000_000),
    ("large", 10_000_000_000, float("inf")),
)


def market_cap_tier(market_cap_usd: Optional[float]) -> Optional[str]:
    """Ordnet eine Marktkapitalisierung einer groben Groessen-Klasse zu (micro/small/
    mid/large), fuer die Backtest-Aufschluesselung nach Ticker-Groesse. None bei
    fehlendem/ungueltigem Wert."""
    if not isinstance(market_cap_usd, (int, float)) or market_cap_usd <= 0:
        return None
    for name, lo, hi in MARKET_CAP_TIERS:
        if lo <= market_cap_usd < hi:
            return name
    return None


async def _fetch_quote_by_symbol(
    symbol: str, client: Optional[httpx.AsyncClient] = None
) -> Optional[dict]:
    """Gemeinsamer Kern von get_quote()/get_index_quote(): holt eine Live-Quote fuer ein
    BEREITS fertiges Stooq-Symbol (z.B. 'aapl.us' oder '^vix'), best-effort mit Kurz-
    Cache + Circuit-Breaker (#13). Gibt bei jedem Problem None zurueck (nie eine
    Exception nach aussen)."""
    now = time.time()
    ttl = config.PRICE_CACHE_TTL_SECONDS

    if ttl > 0:
        cached = _QUOTE_CACHE.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

    if now < _breaker["open_until"]:
        # Circuit-Breaker offen: Kursdienst gilt gerade als gestoert, gar nicht anfragen.
        logger.debug("Kurs-Circuit-Breaker offen, ueberspringe Abfrage fuer %s.", symbol)
        return None

    url = _STOOQ_URL.format(symbol=symbol)
    try:
        if client is not None:
            resp = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
                resp = await c.get(url)
        resp.raise_for_status()
        quote = parse_stooq_csv(resp.text)
    except Exception:
        logger.debug("Kursabfrage fuer %s fehlgeschlagen (best-effort).", symbol, exc_info=True)
        _breaker["consecutive_failures"] += 1
        if _breaker["consecutive_failures"] >= _BREAKER_THRESHOLD:
            _breaker["open_until"] = now + _BREAKER_COOLDOWN_SECONDS
            logger.info(
                "Kurs-Circuit-Breaker fuer %.0fs geoeffnet (%d Fehler in Folge).",
                _BREAKER_COOLDOWN_SECONDS, _breaker["consecutive_failures"],
            )
        return None

    # Erfolgreicher HTTP-Call (auch wenn die Antwort keine gueltigen Kursdaten enthielt):
    # Fehlerzaehler zuruecksetzen und Ergebnis cachen (kurz, damit ein "N/D" nicht sofort
    # erneut abgefragt wird, aber schnell wieder frisch geholt werden kann).
    _breaker["consecutive_failures"] = 0
    if ttl > 0:
        _QUOTE_CACHE[symbol] = (now, quote)
    return quote


async def get_quote(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Holt eine Live-Quote fuer einen US-Ticker (best-effort). Gibt bei jedem Problem
    None zurueck (nie eine Exception nach aussen). Nutzt einen Kurz-Cache
    (PRICE_CACHE_TTL_SECONDS) und einen Circuit-Breaker (#13), damit ein mehrfach
    vorkommender Ticker nicht mehrfach abgefragt und ein down/rate-limited Kursdienst
    nicht bei jedem Ticker erneut angefragt wird.

    Liefert Stooq keinen Kurs (Netzproblem ODER 'N/D' - z.B. vorboerslich bei kleineren/
    juengeren Tickern haeufig, live beobachtet bei RKLB), wird Yahoo Finance als zweite,
    unabhaengige kostenlose Quelle versucht, bevor komplett aufgegeben wird - siehe
    parse_yahoo_quote_json."""
    if not ticker:
        return None
    symbol = to_stooq_symbol(ticker)
    quote = await _fetch_quote_by_symbol(symbol, client=client)
    if quote is not None:
        return quote

    data = await _fetch_yahoo_chart_json(ticker, {"interval": "1d", "range": "5d"}, client=client)
    fallback = parse_yahoo_quote_json(data)
    if fallback is not None:
        logger.info("Kurs fuer %s ueber Yahoo-Fallback bezogen (Stooq lieferte nichts).", ticker)
        if config.PRICE_CACHE_TTL_SECONDS > 0:
            _QUOTE_CACHE[symbol] = (time.time(), fallback)
    return fallback


async def get_index_quote(symbol: str, client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """Wie get_quote(), aber fuer einen Stooq-INDEX statt eines US-Einzeltitels (z.B.
    '^vix' fuer den CBOE Volatility Index) - dort wird KEIN '.us'-Suffix angehaengt
    (siehe to_stooq_symbol), das Symbol wird 1:1 an Stooq durchgereicht. Fuer den
    VIX-Marktregime-Gate (ENABLE_VIX_GATE, siehe app/orchestrator.py)."""
    if not symbol:
        return None
    return await _fetch_quote_by_symbol(symbol.strip().lower(), client=client)


async def get_price(ticker: str, client: Optional[httpx.AsyncClient] = None) -> Optional[float]:
    quote = await get_quote(ticker, client=client)
    return quote["price"] if quote else None


def parse_stooq_history_csv(text: str, max_bars: int = 400) -> Optional[dict]:
    """Parst die Stooq-Tageshistorie-CSV zu parallelen Listen
    {'date','open','high','low','close','volume'} (aelteste zuerst). Nur Zeilen mit
    vollstaendigen, endlichen O/H/L/C werden uebernommen; Volumen fehlt teils ('N/D') und
    wird dann 0.0. Gibt die letzten max_bars Zeilen zurueck. None bei unbrauchbarer Antwort."""
    if not isinstance(text, str):
        return None
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    try:
        di = header.index("date")
        oi = header.index("open")
        hi = header.index("high")
        li = header.index("low")
        ci = header.index("close")
    except ValueError:
        return None
    vi = header.index("volume") if "volume" in header else None

    dates, opens, highs, lows, closes, volumes = [], [], [], [], [], []

    def _num(cell: str) -> Optional[float]:
        cell = cell.strip()
        if not cell or cell.upper() == "N/D":
            return None
        try:
            v = float(cell)
        except ValueError:
            return None
        return v if math.isfinite(v) else None

    for ln in lines[1:]:
        cols = ln.split(",")
        if len(cols) < len(header):
            continue
        o, h, l, c = _num(cols[oi]), _num(cols[hi]), _num(cols[li]), _num(cols[ci])
        if None in (o, h, l, c):
            continue
        v = _num(cols[vi]) if vi is not None else None
        dates.append(cols[di].strip())
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(v if v is not None else 0.0)

    if len(closes) < 2:
        return None
    return {
        "date": dates[-max_bars:],
        "open": opens[-max_bars:],
        "high": highs[-max_bars:],
        "low": lows[-max_bars:],
        "close": closes[-max_bars:],
        "volume": volumes[-max_bars:],
    }


async def get_history(
    ticker: str, client: Optional[httpx.AsyncClient] = None, max_bars: int = 400
) -> Optional[dict]:
    """Holt die Tageshistorie eines US-Tickers (best-effort, nie eine Exception nach
    aussen). Nutzt einen eigenen Cache (HISTORY_CACHE_TTL_SECONDS) und denselben
    Circuit-Breaker wie die Live-Quote, damit ein gestoerter Kursdienst nicht bei jedem
    Ticker erneut angefragt wird.

    Liefert Stooq keine Historie, wird - wie bei get_quote() - Yahoo Finance als zweite
    Quelle versucht, bevor komplett aufgegeben wird (siehe parse_yahoo_history_json)."""
    if not ticker:
        return None
    symbol = to_stooq_symbol(ticker)
    now = time.time()
    ttl = config.HISTORY_CACHE_TTL_SECONDS
    if ttl > 0:
        cached = _HISTORY_CACHE.get(symbol)
        if cached is not None and (now - cached[0]) < ttl:
            return cached[1]

    history = None
    if now >= _breaker["open_until"]:
        url = _STOOQ_HISTORY_URL.format(symbol=symbol)
        try:
            if client is not None:
                resp = await client.get(url)
            else:
                async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                    resp = await c.get(url)
            resp.raise_for_status()
            history = parse_stooq_history_csv(resp.text, max_bars=max_bars)
        except Exception:
            logger.debug("Historie fuer %s fehlgeschlagen (best-effort).", ticker, exc_info=True)
            _breaker["consecutive_failures"] += 1
            if _breaker["consecutive_failures"] >= _BREAKER_THRESHOLD:
                _breaker["open_until"] = now + _BREAKER_COOLDOWN_SECONDS
        else:
            _breaker["consecutive_failures"] = 0
    else:
        logger.debug("Kurs-Circuit-Breaker offen, ueberspringe Historie fuer %s.", ticker)

    if history is None:
        data = await _fetch_yahoo_chart_json(
            ticker, {"interval": "1d", "range": "2y"}, client=client
        )
        history = parse_yahoo_history_json(data, max_bars=max_bars)
        if history is not None:
            logger.info(
                "Historie fuer %s ueber Yahoo-Fallback bezogen (Stooq lieferte nichts).", ticker
            )

    if ttl > 0:
        _HISTORY_CACHE[symbol] = (now, history)
    return history


def previous_close(history: Optional[dict], today: str) -> Optional[float]:
    """Schlusskurs des letzten VOR `today` (ISO 'YYYY-MM-DD', US-Marktzeit - siehe
    market_hours.current_market_date) abgeschlossenen Handelstags aus der
    Tageshistorie - fuer die Gap-Berechnung (heutiger Eroeffnungskurs vs. gestriger
    Schluss). Filtert explizit auf Datum < today, statt sich auf einen festen Index
    (z.B. "-1" oder "-2") zu verlassen: ob Stooqs Tageshistorie den heutigen, noch
    laufenden Handelstag schon als eigene (unvollstaendige) Zeile enthaelt, ist nicht
    garantiert - der Datums-Filter funktioniert in beiden Faellen gleich zuverlaessig.
    None bei fehlenden/unvollstaendigen Daten."""
    if not history or not today:
        return None
    dates = history.get("date") or []
    closes = history.get("close") or []
    if len(dates) != len(closes) or not dates:
        return None
    # Ueber ALLE passenden Zeilen das Maximum bestimmen statt beim ersten Treffer von
    # hinten abzubrechen: Stooqs CSV-Zeilenreihenfolge wird nirgends erzwungen/geprueft
    # (parse_stooq_history_csv uebernimmt sie unveraendert), eine einzelne unsortierte/
    # doppelte Zeile (Symbol-Relisting, Datenfehler) wuerde sonst den falschen Schluss-
    # kurs liefern statt des tatsaechlich juengsten Handelstags vor `today`.
    best_date, best_close = None, None
    for d, c in zip(dates, closes):
        if isinstance(d, str) and d < today and (best_date is None or d > best_date):
            best_date, best_close = d, c
    return best_close
