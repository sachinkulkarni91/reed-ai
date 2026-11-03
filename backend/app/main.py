from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .api import actions, bot, chat, ingest, integrations, meetings
from .config import settings
from .db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logging.getLogger(__name__).info(
        "Reed AI up - model=%s transcription=%s trello=%s",
        settings.reed_model,
        settings.transcription_provider,
        "on" if settings.trello_api_key else "off",
    )
    yield
    # Leave any call we are still in, so the browser process does not outlive us.
    from .bot.manager import registry

    for supervisor in list(registry.bots.values()):
        try:
            await supervisor.stop()
        except Exception:
            # A single stuck/already-finished bot must never abort the rest of
            # shutdown - `Application shutdown failed` takes uvicorn down entirely.
            logging.getLogger(__name__).exception(
                "error stopping bot for meeting %s during shutdown", supervisor.meeting_id
            )


app = FastAPI(title="Reed AI", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meetings.router)
app.include_router(bot.router)
app.include_router(actions.router)
app.include_router(integrations.router)
app.include_router(chat.router)
app.include_router(ingest.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Production: serve the built SPA from ./static (created by the Dockerfile).
# Absent in development, where Vite serves the frontend and proxies /api here.
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        candidate = _static_dir / path
        if path and ".." not in path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_static_dir / "index.html")
