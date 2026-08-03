# Hinweise für Claude (dieses Projekt)

## VM-Zugang (Oracle Cloud, Live-Bot + Backtest-Server)

```bash
ssh -i ~/Downloads/ssh-key-2026-07-25.key ubuntu@92.5.120.113
```

Projektverzeichnis auf der VM:

```bash
cd ~/trump-market-monitor
```

Terminal des Nutzers: **Windows PowerShell** (nicht bash/zsh) - Befehle entsprechend
kompatibel halten (kein `&&`-Verkettungsproblem, aber z.B. kein bash-spezifisches
Heredoc ohne Rücksprache).

## Format-Regel für Kommandos (wichtig, expliziter Nutzerwunsch)

**Jeder einzelne, eigenständige Befehl bekommt seine EIGENE Code-Box.** Niemals
mehrere unabhängige Befehle in einer Box zusammenfassen (weder mit `&&` noch einfach
untereinander) - der Nutzer nutzt den Kopieren-Button jeder Box, um GENAU EINEN
Befehl auszuführen. Eine Box mit mehreren Zeilen kopiert alle auf einmal, das ist
nicht gewollt.

Ausnahme: ein einzelner Befehl, der über mehrere Zeilen geht (z.B. mit `\`
Zeilenfortsetzung, oder ein mehrzeiliger `sh -c "..."`-String), bleibt EINE Box - das
ist strukturell ein einziger Befehl, keine Verkettung mehrerer Schritte.

## Standard-Ablauf für einen manuellen Backtest-Lauf auf der VM

Immer in dieser Reihenfolge, jeder Schritt als eigene Code-Box:

1. Einloggen (siehe oben)
2. `cd ~/trump-market-monitor`
3. Auto-Update-Cron PAUSIEREN (er baut sonst mitten im Lauf neu und killt ihn):
   ```bash
   crontab -l > ~/crontab_backup.txt
   crontab -l | grep -v auto_update.sh | crontab -
   ```
4. Aktuellen Branch holen und bauen (nur nötig, wenn sich Code geändert hat):
   ```bash
   git fetch origin claude/market-prediction-review-jgim1j
   git merge --ff-only origin/claude/market-prediction-review-jgim1j
   ```
   ```bash
   sudo docker compose up -d --build
   ```
5. Backtest ABGEKOPPELT starten (überlebt SSH-Trennung/Rechner-Aus) - Logname pro
   Lauf hochzählen (`backtestN.log`), damit nichts überschrieben wird:
   ```bash
   sudo docker compose exec -d trump-monitor sh -c \
     "python -m app.backtest --source finnhub --start 2026-05-05 --end 2026-08-03 \
      --budget-usd 3.0 --max-calls 0 --execute \
      --out /data/backtest_finnhub_report.json \
      > /data/backtestN.log 2>&1; echo FERTIG >> /data/backtestN.log"
   ```
6. Prüfen, dass GENAU EIN Lauf läuft (nicht zwei durch versehentlichen Doppelstart):
   ```bash
   sudo docker compose top trump-monitor
   ```
7. Später: Status prüfen, Report holen, **Cron unbedingt wieder aktivieren**:
   ```bash
   sudo docker compose exec -T trump-monitor sh -c "grep -q FERTIG /data/backtestN.log && echo 'FERTIG' || echo 'laeuft noch'"
   ```
   ```bash
   sudo docker compose cp trump-monitor:/data/backtest_finnhub_report.json ./backtest_finnhub_report.json
   ```
   ```bash
   crontab ~/crontab_backup.txt
   ```

`sudo docker compose top trump-monitor` zeigt Host-PIDs (nicht die interne PID im
Container) - zum Killen eines hängenden Prozesses also `sudo kill -9 <PID>` DIREKT
auf dem Host, nicht über `docker compose exec`.

Bereits gefundene, dauerhafte Stolpersteine in diesem Projekt (nicht erneut
diagnostizieren, einfach wissen):
- GDELT: hartes Rate-Limit (1 Anfrage/5s), bei Verstoß IP-Sperre für Stunden.
- Alpha Vantage: 25 Anfragen/TAG (nicht pro Lauf), `topics`-Parameter mit Komma wird
  als UND statt ODER behandelt (`DEFAULT_TOPICS = None` lassen).
- Yahoo `v7/finance/quote` (Marktkapitalisierung) liefert dauerhaft 401 Unauthorized -
  ersetzt durch Finnhubs `stock/profile2` (`app/prices.py:get_market_caps`).
- Backtest-Chargen-Dedup war früher O(n²) - jetzt zeitfenstergebündelt
  (`DEDUP_WINDOW_SECONDS`, siehe `app/orchestrator.py:_partition_duplicates`).

## Branches

- Entwicklung: `claude/market-prediction-review-jgim1j`
- Produktions-/Deploy-Branch (von der VM per Cron gezogen):
  `claude/trump-market-impact-analyzer-dbjvu5`

## Standing-Anweisungen des Nutzers

- Immer ALLE Befehle geben (inkl. Login, Speichern, Cron-Wiederherstellen usw.) -
  nichts als "selbstverständlich" auslassen.
- Nie eine API-Key-Eingabe im Chat verlangen - Verifikationsbefehle geben nur
  "gesetzt"/"fehlt" aus, nie den tatsächlichen Wert.
