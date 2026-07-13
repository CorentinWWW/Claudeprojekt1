"""Best-Effort Zugriff auf Truth-Social-Posts ueber die (inoffizielle, undokumentierte)
JSON-API der Web-App.

WICHTIG - Einschraenkungen:
- Es gibt keine offizielle, unterstuetzte Public-API von Truth Social.
- Diese Endpunkte koennen sich jederzeit aendern, Rate-Limits bekommen oder
  komplett Authentifizierung verlangen. Dieser Source-Adapter ist daher bewusst
  defensiv geschrieben: schlaegt ein Request fehl, wird das geloggt und die
  Quelle liefert einfach 0 neue Statements zurueck, statt den Rest des Systems
  zum Absturz zu bringen.
- Falls die unauthentifizierten Calls blockiert werden, kann optional ein eigenes
  Bearer-Token (TRUTH_SOCIAL_BEARER_TOKEN, z.B. aus einer eigenen eingeloggten
  Session) gesetzt werden.
"""
import logging
import time

import httpx

from app.config import TRUTH_SOCIAL_BEARER_TOKEN, TRUTH_SOCIAL_HANDLE
from app.db import RawStatement
from app.sources.base import Source

logger = logging.getLogger(__name__)

BASE_URL = "https://truthsocial.com/api/v1"


class TruthSocialSource(Source):
    name = "truth_social"

    def __init__(self, handle: str = TRUTH_SOCIAL_HANDLE):
        self.handle = handle
        self._account_id: str | None = None
        self._seen: set[str] = set()
        self._disabled = False

    def _headers(self) -> dict:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; trump-market-monitor/1.0)"}
        if TRUTH_SOCIAL_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {TRUTH_SOCIAL_BEARER_TOKEN}"
        return headers

    async def _resolve_account_id(self, client: httpx.AsyncClient) -> str | None:
        if self._account_id:
            return self._account_id
        try:
            resp = await client.get(
                f"{BASE_URL}/accounts/lookup",
                params={"acct": self.handle},
                headers=self._headers(),
            )
            resp.raise_for_status()
            self._account_id = resp.json().get("id")
            return self._account_id
        except Exception:
            logger.warning(
                "Truth-Social-Account konnte nicht aufgeloest werden (%s). "
                "Quelle liefert bis auf Weiteres keine Ergebnisse.",
                self.handle,
                exc_info=True,
            )
            return None

    async def poll(self) -> list[RawStatement]:
        if self._disabled:
            return []

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                account_id = await self._resolve_account_id(client)
                if not account_id:
                    return []

                resp = await client.get(
                    f"{BASE_URL}/accounts/{account_id}/statuses",
                    params={"exclude_replies": "true", "limit": "20"},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                statuses = resp.json()
        except Exception:
            logger.warning(
                "Truth-Social-Statuses konnten nicht geladen werden "
                "(Endpoint evtl. blockiert/geaendert).",
                exc_info=True,
            )
            return []

        results: list[RawStatement] = []
        for status in statuses or []:
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


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", html or "")
    return " ".join(text.split()).strip()
