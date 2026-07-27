FROM python:3.11-slim

WORKDIR /app

# Fester, von HOME unabhaengiger Pfad: wird sowohl beim "playwright install" unten
# (als root) als auch spaeter zur Laufzeit (als nicht-root appuser) verwendet, damit
# der Browser-Fallback (truth_social.py) die Chromium-Installation in beiden Faellen
# am selben Ort findet.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ffmpeg: fuer app/sources/fed_audio.py (Live-Audio-Mitschnitt waehrend konfigurierter
# FED-Zeitfenster, ENABLE_FED_AUDIO=false per Default - siehe app/config.py). Eigener,
# schlanker RUN-Schritt statt in die pip/playwright-Zeile unten gemischt, damit ein
# Cache-Hit hier nicht von Python-Abhaengigkeits-Aenderungen abhaengt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

ENV DB_PATH=/data/trump_monitor.db

# Nicht als root laufen lassen (Defense-in-Depth: reduziert den Schaden, falls z.B.
# eine Sicherheitsluecke in einer Abhaengigkeit ausgenutzt wird).
RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /app --no-create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data /ms-playwright

USER appuser

VOLUME ["/data"]

EXPOSE 8000

# Prueft /api/health (siehe app/dashboard.py) statt nur "Prozess laeuft noch" - so
# zeigt "docker ps"/docker-compose auch einen Container als unhealthy an, dessen
# Monitoring-Loop wegen eines Konfigurationsfehlers gar nicht erst gestartet ist.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)); sys.exit(0 if d.get('ok') else 1)"

CMD ["python", "main.py"]
