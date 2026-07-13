"""Best-Effort Zugriff auf Truth-Social-Posts.

WICHTIG - Einschraenkungen:
- Es gibt keine offizielle, unterstuetzte Public-API von Truth Social.
- Primaerer Versuch: direkter, unauthentifizierter Call gegen die (undokumentierte)
  JSON-API der Web-App. Guenstig und schnell, kann aber jederzeit durch Bot-Schutz
  oder Aenderungen an der API blockiert werden.
- Fallback: ein echter headless Chromium (Playwright) laedt die oeffentliche
  Profilseite; wir hoeren dabei auf die Netzwerk-Antworten, die die Seite selbst
  von ihrer eigenen API bekommt, und werten dieselben JSON-Daten aus. Das ist
  robuster gegen Bot-Blocking (echter Browser-Fingerprint/Cookies/Header), aber
  CPU/RAM-intensiv - deshalb nur in einem Mindestabstand versucht, nicht bei jedem
  Poll-Zyklus.
- Falls beides fehlschlaegt, liefert die Quelle einfach 0 neue Statements statt
  den Rest des Systems zum Absturz zu bringen.
- Optional kann ein eigenes Bearer-Token (TRUTH_SOCIAL_BEARER_TOKEN, z.B. aus
  einer eigenen eingeloggten Session) gesetzt werden, falls verfuegbar.
"""
import asyncio
import logging
import os
import re
import time

import httpx

from app.config import (
    TRUTH_SOCIAL_BEARER_TOKEN,
    TRUTH_SOCIAL_BROWSER_FALLBACK,
    TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL,
    TRUTH_SOCIAL_HANDLE,
)
from app.db import RawStatement
from app.sources.base import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://truthsocial.com/api/v1"
PROFILE_URL_TMPL = "https://truthsocial.com/@{handle}"
STATUSES_PATTERN = re.compile(r"/api/v1/accounts/[^/]+/statuses")
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Manche Umgebungen liefern ein vorinstalliertes Chromium unter einem festen Pfad,
# dessen Revision von der per pip installierten Playwright-Version abweichen kann.
# Falls vorhanden, direkt darauf zeigen statt auf die (evtl. fehlende) von
# Playwright selbst erwartete Standard-Revision - ansonsten normales Verhalten
# (Standardpfad nach "playwright install chromium").
_LOCAL_CHROMIUM_PATH = "/opt/pw-browsers/chromium"


class TruthSocialSource(Source):
    name = "truth_social"

    def __init__(self, handle: str = TRUTH_SOCIAL_HANDLE):
        self.handle = handle
        self._account_id: str | None = None
        self._seen: set[str] = set()
        self._browser_deps_missing = False
        self._last_browser_attempt = 0.0

    def _headers(self) -> dict:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; trump-market-monitor/1.0)"}
        if TRUTH_SOCIAL_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {TRUTH_SOCIAL_BEARER_TOKEN}"
        return headers

    # ---- Primaerer Pfad: direkter API-Call ---------------------------------

    async def _resolve_account_id(self, client: httpx.AsyncClient) -> str | None:
        if self._account_id:
            return self._account_id
        resp = await client.get(
            f"{BASE_URL}/accounts/lookup",
            params={"acct": self.handle},
            headers=self._headers(),
        )
        resp.raise_for_status()
        self._account_id = resp.json().get("id")
        return self._account_id

    async def _fetch_via_direct_api(self) -> list[dict] | None:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                account_id = await self._resolve_account_id(client)
                if not account_id:
                    return None
                resp = await client.get(
                    f"{BASE_URL}/accounts/{account_id}/statuses",
                    params={"exclude_replies": "true", "limit": "20"},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else None
        except Exception:
            logger.info(
                "Truth-Social direkter API-Call fehlgeschlagen (Endpoint evtl. "
                "blockiert/geaendert), versuche ggf. Browser-Fallback.",
                exc_info=True,
            )
            return None

    # ---- Fallback: echter Browser, Netzwerk-Antworten mitschneiden --------

    async def _fetch_via_browser(self) -> list[dict] | None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            if not self._browser_deps_missing:
                logger.warning(
                    "Playwright nicht installiert - Truth-Social-Browser-Fallback "
                    "deaktiviert. 'pip install playwright' + 'playwright install "
                    "chromium' fuer robusteren Zugriff."
                )
                self._browser_deps_missing = True
            return None

        captured: list[dict] = []
        pending_tasks = []

        async def handle_response(response):
            if response.status == 200 and STATUSES_PATTERN.search(response.url):
                try:
                    data = await response.json()
                    if isinstance(data, list):
                        captured.extend(data)
                except Exception:
                    logger.debug("Konnte Response-JSON nicht parsen: %s", response.url)

        try:
            async with async_playwright() as p:
                launch_kwargs = {
                    "headless": True,
                    "args": ["--no-sandbox", "--disable-dev-shm-usage"],
                }
                if os.path.exists(_LOCAL_CHROMIUM_PATH):
                    launch_kwargs["executable_path"] = _LOCAL_CHROMIUM_PATH
                browser = await p.chromium.launch(**launch_kwargs)
                try:
                    context = await browser.new_context(user_agent=BROWSER_USER_AGENT)
                    page = await context.new_page()
                    page.on(
                        "response",
                        lambda r: pending_tasks.append(asyncio.create_task(handle_response(r))),
                    )
                    url = PROFILE_URL_TMPL.format(handle=self.handle)
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(5000)
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)
                finally:
                    await browser.close()
        except Exception:
            logger.warning(
                "Truth-Social-Browser-Fallback fehlgeschlagen (z.B. fehlende "
                "System-Abhaengigkeiten fuer Chromium).",
                exc_info=True,
            )
            return None

        if not captured:
            logger.info(
                "Truth-Social-Browser-Fallback hat die Seite geladen, aber keine "
                "passenden API-Antworten abgefangen (Pattern: %s). Die Web-App "
                "koennte ihre interne API-Struktur geaendert haben.",
                STATUSES_PATTERN.pattern,
            )
            return None

        return captured

    # ---- Gemeinsames Parsing ------------------------------------------------

    def _to_raw_statements(self, statuses: list[dict]) -> list[RawStatement]:
        results: list[RawStatement] = []
        for status in statuses:
            source_id = str(status.get("id", ""))
            if not source_id or source_id in self._seen:
                continue
            self._seen.add(source_id)

            text = _strip_html(status.get("content", ""))
            if not text:
                continue

            results.append(
                RawStatement(
                    source=self.name,
                    source_id=source_id,
                    text=text,
                    url=status.get("url"),
                    published_at=time.time(),
                )
            )
        return results

    async def poll(self) -> list[RawStatement]:
        statuses = await self._fetch_via_direct_api()

        if statuses is None and TRUTH_SOCIAL_BROWSER_FALLBACK:
            now = time.time()
            if now - self._last_browser_attempt >= TRUTH_SOCIAL_BROWSER_FALLBACK_MIN_INTERVAL:
                self._last_browser_attempt = now
                statuses = await self._fetch_via_browser()

        if not statuses:
            return []

        return self._to_raw_statements(statuses)


def _strip_html(html_content: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_content or "")
    return " ".join(text.split()).strip()
