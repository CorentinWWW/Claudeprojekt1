#!/usr/bin/env bash
# Einmal-Setup fuer eine frische Oracle Cloud "Always Free" VM (Ubuntu-Image).
# Installiert Docker, oeffnet Port 8000 in der VM-eigenen Firewall, klont das
# Repo und bereitet .env vor. Danach nur noch .env ausfuellen und
# "sudo docker compose up -d --build" ausfuehren.
#
# Nutzung (auf der VM, per SSH eingeloggt):
#   curl -fsSL https://raw.githubusercontent.com/CorentinWWW/Claudeprojekt1/claude/trump-market-impact-analyzer-dbjvu5/deploy/oracle_bootstrap.sh | bash
set -euo pipefail

REPO_URL="https://github.com/CorentinWWW/Claudeprojekt1.git"
BRANCH="claude/trump-market-impact-analyzer-dbjvu5"
APP_DIR="$HOME/trump-market-monitor"

echo "== [1/4] Docker installieren =="
if ! command -v docker &> /dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
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

echo "== [2/4] Port 8000 in der VM-Firewall oeffnen =="
# Oracle-Ubuntu-Images blocken eingehende Ports standardmaessig per iptables,
# zusaetzlich zur Security List auf Netzwerk-Ebene (siehe README/Anleitung).
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT || true
sudo netfilter-persistent save 2>/dev/null || sudo iptables-save | sudo tee /etc/iptables/rules.v4 > /dev/null 2>&1 || true
# Falls stattdessen ufw aktiv ist:
sudo ufw allow 8000/tcp 2>/dev/null || true

echo "== [3/4] Repo klonen =="
if [ ! -d "$APP_DIR" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  echo "$APP_DIR existiert bereits, ueberspringe Klonen."
fi
cd "$APP_DIR"

echo "== [4/4] .env vorbereiten =="
if [ ! -f .env ]; then
  cp .env.example .env
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
  echo ".env existiert bereits - starte den Stack direkt."
  cd "$APP_DIR" && sudo docker compose up -d --build
fi
