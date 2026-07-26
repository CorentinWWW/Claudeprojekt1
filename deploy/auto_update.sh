#!/usr/bin/env bash
# Pull-basiertes Auto-Update fuer den Dauerbetrieb (siehe README "Oracle Cloud Free
# Tier"): per Cron alle 15 Minuten aufgerufen, prueft ob es auf dem Produktions-Branch
# neue Commits gibt und deployt sie automatisch. Bewusst PULL-basiert (die VM zieht
# sich selbst Updates) statt push-basiert per SSH-von-aussen - keine Zugangsdaten
# muessen auf GitHub hinterlegt werden, kein zusaetzlicher offener Port, keine neue
# Angriffsflaeche. Rollt automatisch auf den zuletzt funktionierenden Commit zurueck,
# falls der Container nach dem Update nicht "healthy" wird - ein kaputter Push legt so
# nie den laufenden Bot lahm.
set -euo pipefail

APP_DIR="$HOME/trump-market-monitor"
BRANCH="claude/trump-market-impact-analyzer-dbjvu5"
LOCK_FILE="/tmp/trump-monitor-autoupdate.lock"
LOG_FILE="$APP_DIR/auto_update.log"
HEALTH_URL="http://localhost:8000/api/health"
HEALTH_TIMEOUT_SECONDS=120

# Verhindert ueberlappende Laeufe, falls ein vorheriger Build (z.B. nach einer
# requirements.txt-Aenderung) laenger als 15 Minuten dauert.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Iseconds) Vorheriger Lauf noch aktiv, ueberspringe." >> "$LOG_FILE"
  exit 0
fi

cd "$APP_DIR"
git fetch origin "$BRANCH" --quiet

LOCAL_SHA=$(git rev-parse HEAD)
REMOTE_SHA=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  exit 0
fi

echo "$(date -Iseconds) Neuer Commit gefunden: $LOCAL_SHA -> $REMOTE_SHA, deploye..." >> "$LOG_FILE"

# --ff-only statt "git pull": scheitert laut statt still einen Merge-Commit
# anzulegen, falls hier je lokale Aenderungen liegen sollten (sollten es nicht -
# .env ist gitignored, alles andere kommt ausschliesslich aus dem Repo).
git merge --ff-only "origin/$BRANCH" >> "$LOG_FILE" 2>&1
sudo docker compose up -d --build >> "$LOG_FILE" 2>&1

# HEALTHCHECK im Dockerfile braucht start-period=20s + ggf. mehrere 30s-Intervalle,
# bis er zuverlaessig "unhealthy" meldet - deshalb aktiv pollen statt fix zu warten.
DEADLINE=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
HEALTHY=""
while [ $SECONDS -lt "$DEADLINE" ]; do
  if curl -fsS --max-time 4 "$HEALTH_URL" 2>/dev/null | grep -q '"ok":true'; then
    HEALTHY=1
    break
  fi
  sleep 5
done

if [ -n "$HEALTHY" ]; then
  echo "$(date -Iseconds) Deploy erfolgreich, Container healthy auf $REMOTE_SHA." >> "$LOG_FILE"
else
  echo "$(date -Iseconds) FEHLER: nicht healthy nach Deploy von $REMOTE_SHA - Rollback auf $LOCAL_SHA." >> "$LOG_FILE"
  git reset --hard "$LOCAL_SHA"
  sudo docker compose up -d --build >> "$LOG_FILE" 2>&1
  echo "$(date -Iseconds) Rollback auf $LOCAL_SHA abgeschlossen." >> "$LOG_FILE"
fi
