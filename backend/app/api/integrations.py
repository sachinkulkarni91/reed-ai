from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..services.trello import TrelloClient, TrelloError

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("/status")
def status():
    """What is wired up. Never returns secrets - only whether they are present."""
    diarization_on = bool(settings.hf_token or os.environ.get("HF_TOKEN"))
    return {
        "claude": {
            "configured": bool(settings.anthropic_api_key),
            "model": settings.reed_model,
            "note": "" if settings.anthropic_api_key else "Set ANTHROPIC_API_KEY in backend/.env",
        },
        "transcription": {
            "provider": settings.transcription_provider,
            "configured": (
                bool(settings.deepgram_api_key)
                if settings.transcription_provider == "deepgram"
                else bool(settings.sarvam_api_key)
                if settings.transcription_provider == "sarvam"
                else True
            ),
            "model": (
                settings.whisper_model
                if settings.transcription_provider == "whisper"
                else f"{settings.sarvam_model} ({settings.sarvam_mode}) + whisper live"
                if settings.transcription_provider == "sarvam"
                else settings.deepgram_model
            ),
            "diarization": diarization_on,
            "note": (
                "Set HF_TOKEN to enable speaker diarization (who spoke when)."
                if settings.transcription_provider in ("whisper", "sarvam") and not diarization_on
                else "Set SARVAM_API_KEY (dashboard.sarvam.ai) - falling back to local whisper until then."
                if settings.transcription_provider == "sarvam" and not settings.sarvam_api_key
                else ""
            ),
        },
        "trello": {
            "configured": bool(settings.trello_api_key and settings.trello_token),
            "list_id": settings.trello_list_id,
            "auto_push": settings.trello_auto_push,
        },
        "bot": {
            "display_name": settings.bot_display_name,
            "headless": settings.bot_headless,
            "max_minutes": settings.bot_max_meeting_minutes,
        },
    }


@router.get("/speakers")
def list_speakers():
    """The voice registry: who Reed AI can recognise by voice alone."""
    from sqlmodel import Session, select

    from ..db import engine
    from ..models import SpeakerProfile

    with Session(engine) as session:
        rows = session.exec(select(SpeakerProfile).order_by(SpeakerProfile.name)).all()
        return [
            {"id": r.id, "name": r.name, "samples": r.samples, "updated_at": r.updated_at}
            for r in rows
        ]


@router.delete("/speakers/{profile_id}", status_code=204)
def delete_speaker(profile_id: int):
    """Remove a voiceprint (e.g. enrolled under a wrong name)."""
    from fastapi import HTTPException as _HTTPException

    from sqlmodel import Session

    from ..db import engine
    from ..models import SpeakerProfile

    with Session(engine) as session:
        row = session.get(SpeakerProfile, profile_id)
        if row is None:
            raise _HTTPException(404, "No such voiceprint")
        session.delete(row)
        session.commit()


@router.get("/trello/boards")
async def boards():
    try:
        return await TrelloClient().boards()
    except TrelloError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/trello/boards/{board_id}/lists")
async def lists(board_id: str):
    try:
        return await TrelloClient().lists(board_id)
    except TrelloError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/trello/test")
async def test_connection():
    """Verify the configured list is reachable before a real push depends on it."""
    if not settings.trello_list_id:
        raise HTTPException(400, "TRELLO_LIST_ID is not set")
    try:
        client = TrelloClient()
        board_id = await client.board_id_for_list(settings.trello_list_id)
        members = await client.members(board_id)
        return {
            "ok": True,
            "board_id": board_id,
            "members": [m.get("fullName") for m in members],
        }
    except TrelloError as exc:
        raise HTTPException(400, str(exc)) from exc
