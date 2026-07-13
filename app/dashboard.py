import logging

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import get_recent, init_db
from app.orchestrator import run_forever

logger = logging.getLogger(__name__)

app = FastAPI(title="Trump Market Impact Monitor")


@app.on_event("startup")
async def startup():
    init_db()
    import asyncio

    asyncio.create_task(run_forever())


@app.get("/api/statements")
def api_statements(limit: int = 50, only_relevant: bool = False):
    return get_recent(limit=limit, only_relevant=only_relevant)


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
