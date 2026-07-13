FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

ENV DB_PATH=/data/trump_monitor.db
VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "main.py"]
