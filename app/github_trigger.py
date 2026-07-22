"""Triggert GitHub Actions Workflows via repository_dispatch-Events bei neu erkannten Reden.

Die Live-Audio-Quelle erkennt kontinuierlich Sprache in konfigurierten Livestreams;
wenn ein neuer, nicht-wiederholter Text erkannt wird, kann ein GitHub Actions Workflow
automatisch ausgeloest werden (z.B. fuer Sofort-Analyse oder Echtzeit-Monitoring), statt
auf den naechsten Cron-Poll-Zyklus zu warten.
"""
import asyncio
import logging
import json
from typing import Optional

logger = logging.getLogger(__name__)


async def trigger_speech_detected_workflow(
    text: str,
    stream_url: str,
    github_token: str,
    github_repo: str,
) -> bool:
    """Loest ein GitHub Actions Workflow aus, wenn eine neue Rede erkannt wurde.

    Args:
        text: Transkribierter/erkannter Sprach-Text
        stream_url: Die Stream-URL, von der der Text kam
        github_token: GitHub Personal Access Token (oder Organization token) mit
                      "repo" Scope fuer repository_dispatch
        github_repo: GitHub-Repository im Format "owner/repo"

    Returns:
        True, wenn der Trigger erfolgreich war; False bei Fehler.
    """
    if not github_token or not github_repo:
        return False

    loop = asyncio.get_running_loop()

    def _dispatch():
        import urllib.request
        import urllib.error

        url = f"https://api.github.com/repos/{github_repo}/dispatches"
        payload = {
            "event_type": "speech_detected",
            "client_payload": {
                "text": text[:500],  # Basis-Laenge um Payload-Limit nicht zu sprengen
                "stream_url": stream_url,
            }
        }
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 204  # GitHub API gibt 204 No Content zurueck
        except urllib.error.HTTPError as e:
            logger.warning(
                "GitHub repository_dispatch fehlgeschlagen (HTTP %d): %s",
                e.code, e.reason
            )
            return False
        except Exception as e:
            logger.warning("GitHub repository_dispatch Fehler: %s", e)
            return False

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _dispatch),
            timeout=15
        )
        if result:
            logger.info(
                "Speech-Detected Workflow ausgeloest fuer: %.80s",
                text
            )
        return result
    except asyncio.TimeoutError:
        logger.warning("GitHub repository_dispatch-Timeout (>15s)")
        return False
    except Exception as e:
        logger.warning("GitHub repository_dispatch ueberraschender Fehler: %s", e)
        return False
