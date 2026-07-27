#!/usr/bin/env bash
# Einmal-Setup fuer eine frische Oracle Cloud "Always Free" VM (Ubuntu-Image).
# Installiert Docker, oeffnet Port 8000 in der VM-eigenen Firewall, klont das
# Repo, bereitet .env vor und richtet automatische Updates per Cron ein (siehe
# deploy/auto_update.sh). Danach nur noch .env ausfuellen und
# "sudo docker compose up -d --build" ausfuehren.
#
# Nutzung (auf der VM, per SSH eingeloggt):
#   curl -fsSL https://raw.githubusercontent.com/CorentinWWW/Claudeprojekt1/claude/trump-market-impact-analyzer-dbjvu5/deploy/oracle_bootstrap.sh | bash
set -euo pipefail

REPO_URL="https://github.com/CorentinWWW/Claudeprojekt1.git"
BRANCH="claude/trump-market-impact-analyzer-dbjvu5"
APP_DIR="$HOME/trump-market-monitor"

if ! command -v apt-get &> /dev/null; then
  echo "FEHLER: Dieses Skript unterstuetzt nur Debian/Ubuntu-Images (braucht apt-get)."
  echo "Oracle Cloud bietet bei der VM-Erstellung auch Oracle-Linux-Images an (dnf/firewalld) -"
  echo "bitte stattdessen ein Ubuntu-Image waehlen (siehe README-Anleitung)."
  exit 1
fi

echo "== [1/6] Docker installieren =="
if ! command -v docker &> /dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  # Kein Pipe von curl direkt in gpg: unter "set -o pipefail" wuerde ein
  # Netzwerkfehler von curl sonst durch den Exit-Code von gpg maskiert und
  # stillschweigend ein leerer/kaputter Keyring geschrieben.
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.gpg
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.gpg
  rm -f /tmp/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "Docker installiert. (Gruppenmitgliedschaft wirkt erst nach Neu-Login - Skript nutzt bis dahin 'sudo docker'.)"
else
  echo "Docker ist bereits installiert, ueberspringe."
fi

echo "== [2/6] Port 8000 in der VM-Firewall oeffnen =="
# Oracle-Ubuntu-Images blocken eingehende Ports standardmaessig per iptables,
# zusaetzlich zur Security List auf Netzwerk-Ebene (siehe README/Anleitung).
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT || true
sudo netfilter-persistent save 2>/dev/null || sudo iptables-save | sudo tee /etc/iptables/rules.v4 > /dev/null 2>&1 || true
# Falls stattdessen ufw aktiv ist:
sudo ufw allow 8000/tcp 2>/dev/null || true

echo "== [3/6] Swap als Sicherheitsnetz einrichten =="
# Die Referenz-VM (VM.Standard.E2.1.Micro) hat nur 1GB RAM. Ohne Swap fuehrt eine
# kurzzeitige Speicherspitze (z.B. FED-Live-Audio, siehe app/sources/fed_audio.py) zum
# harten OOM-Kill des Containers statt zu blossem Langsamerwerden. 2GB Swap ist Best-
# Effort-Kulanz, kein Ersatz fuer echten RAM - idempotent (ueberspringt, falls schon
# vorhanden) und persistiert ueber Reboots per /etc/fstab.
if [ "$(swapon --show=NAME --noheadings | wc -l)" -eq 0 ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
  echo "2GB Swap eingerichtet."
else
  echo "Swap bereits aktiv, ueberspringe."
fi

echo "== [4/6] Repo klonen =="
if [ ! -d "$APP_DIR" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  echo "$APP_DIR existiert bereits - hole stattdessen neue Commits."
  # Ohne dieses Update wuerde ein erneuter Lauf auf einer bestehenden Installation
  # (z.B. um wie hier den Auto-Update-Cron nachzuruesten) mit einem veralteten Stand
  # weiterarbeiten - neue Dateien wie deploy/auto_update.sh waeren dann noch gar
  # nicht vorhanden und Schritt [5/5] wuerde fehlschlagen.
  git -C "$APP_DIR" fetch origin "$BRANCH" --quiet
  git -C "$APP_DIR" merge --ff-only "origin/$BRANCH"
fi
cd "$APP_DIR"

echo "== [5/6] .env vorbereiten =="
if [ ! -f .env ]; then
  cp .env.example .env
  NEEDS_ENV_SETUP=1
else
  echo ".env existiert bereits - ueberspringe."
  NEEDS_ENV_SETUP=0
fi

echo "== [6/6] Automatische Updates einrichten =="
# Cron statt Push-per-SSH: die VM zieht sich neue Commits vom Produktions-Branch
# selbst (alle 15 Min), baut bei Bedarf neu und rollt bei einem fehlgeschlagenen
# Healthcheck automatisch zurueck - siehe deploy/auto_update.sh fuer Details.
chmod +x "$APP_DIR/deploy/auto_update.sh"
CRON_LINE="*/15 * * * * $APP_DIR/deploy/auto_update.sh"
( crontab -l 2>/dev/null | grep -v "auto_update.sh" ; echo "$CRON_LINE" ) | crontab -
echo "Cron eingerichtet (prueft alle 15 Min auf neue Commits, Log: $APP_DIR/auto_update.log)."

if [ "$NEEDS_ENV_SETUP" = "1" ]; then
  echo ""
  echo "############################################################"
  echo "  Fast fertig! Jetzt noch:"
  echo "  1) nano $APP_DIR/.env"
  echo "     -> mindestens ANTHROPIC_API_KEY eintragen"
  echo "     -> optional TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
  echo "  2) cd $APP_DIR && sudo docker compose up -d --build"
  echo "  3) Dashboard: http://<Server-IP>:8000"
  echo "############################################################"
else
  cd "$APP_DIR" && sudo docker compose up -d --build
fi
