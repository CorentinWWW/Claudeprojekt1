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
# Merkt sich den Commit, der zuletzt NACHGEWIESEN gesund lief. Bewusst nicht "git HEAD"
# als Massstab: bricht ein Lauf zwischen "git merge" und dem fertigen Rebuild ab (z.B.
# Strg+C bei manuellem Aufruf, Reboot, OOM), dann steht HEAD schon auf dem neuen Commit,
# waehrend der Container noch das alte Image faehrt. Ein HEAD-Vergleich wuerde das als
# "aktuell" werten und den Container dauerhaft veraltet stehen lassen.
DEPLOYED_FILE="$APP_DIR/.deployed_sha"
# Verhindert, dass ein Commit, der den Healthcheck reisst, alle 15 Minuten erneut
# gebaut und zurueckgerollt wird. Ein spaeterer Fix hat eine andere SHA und wird
# normal wieder versucht.
FAILED_FILE="$APP_DIR/.failed_sha"
HEALTH_URL="http://localhost:8000/api/health"
HEALTH_TIMEOUT_SECONDS=120

log() { echo "$(date -Iseconds) $*" >> "$LOG_FILE"; }

# Verhindert ueberlappende Laeufe, falls ein vorheriger Build (z.B. nach einer
# requirements.txt-Aenderung) laenger als 15 Minuten dauert.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Vorheriger Lauf noch aktiv, ueberspringe."
  exit 0
fi

cd "$APP_DIR"
git fetch origin "$BRANCH" --quiet

REMOTE_SHA=$(git rev-parse "origin/$BRANCH")
CURRENT_SHA=$(git rev-parse HEAD)
# Erster Lauf nach der Einrichtung: es laeuft bereits ein gesunder Container auf dem
# ausgecheckten Stand, den nehmen wir als Ausgangsbasis.
DEPLOYED_SHA=$(cat "$DEPLOYED_FILE" 2>/dev/null || echo "$CURRENT_SHA")

if [ "$DEPLOYED_SHA" = "$REMOTE_SHA" ]; then
  exit 0
fi

if [ "$(cat "$FAILED_FILE" 2>/dev/null || true)" = "$REMOTE_SHA" ]; then
  exit 0
fi

log "Neuer Commit gefunden: $DEPLOYED_SHA -> $REMOTE_SHA, deploye..."

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
  echo "$REMOTE_SHA" > "$DEPLOYED_FILE"
  rm -f "$FAILED_FILE"
  log "Deploy erfolgreich, Container healthy auf $REMOTE_SHA."
else
  log "FEHLER: nicht healthy nach Deploy von $REMOTE_SHA - Rollback auf $DEPLOYED_SHA."
  # Erst die kaputte SHA vermerken, dann zuruecksetzen: bricht der Rollback selbst ab,
  # wird der defekte Commit trotzdem nicht alle 15 Minuten erneut versucht.
  echo "$REMOTE_SHA" > "$FAILED_FILE"
  git reset --hard "$DEPLOYED_SHA" >> "$LOG_FILE" 2>&1
  sudo docker compose up -d --build >> "$LOG_FILE" 2>&1
  log "Rollback auf $DEPLOYED_SHA abgeschlossen."
fi
