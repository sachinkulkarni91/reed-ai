from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from ..bot.manager import BotSupervisor, registry
from ..bot.playwright_bot import detect_platform
from ..db import get_session
from ..models import Meeting, MeetingStatus

router = APIRouter(prefix="/api/bot", tags=["bot"])


class JoinRequest(BaseModel):
    meeting_url: str
    title: str | None = None
    language: str = "en"
    display_name: str | None = None


def _ingest_url(request: Request, meeting_id: int) -> str:
    # The bot lives in this process, so talk to ourselves directly instead of
    # via the public URL (behind a proxy the scheme/host seen here is the edge's).
    port = (request.scope.get("server") or (None, None))[1] or os.environ.get("PORT", "8000")
    return f"ws://127.0.0.1:{port}/ws/ingest/{meeting_id}"


@router.post("/join", status_code=202)
async def join_meeting(
    payload: JoinRequest, request: Request, session: Session = Depends(get_session)
):
    try:
        platform = detect_platform(payload.meeting_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    meeting = Meeting(
        title=payload.title or "Live meeting",
        meeting_url=payload.meeting_url,
        platform=platform,
        status=MeetingStatus.joining,
        language=payload.language,
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    # Created up front so the ingest socket has somewhere to deliver audio the
    # instant capture starts, before the bot task gets scheduled.
    registry.get_or_create_session(meeting.id, payload.language)

    supervisor = BotSupervisor(
        meeting.id,
        payload.meeting_url,
        _ingest_url(request, meeting.id),
        payload.display_name,
    )
    registry.bots[meeting.id] = supervisor
    supervisor.start()

    return {"meeting_id": meeting.id, "platform": platform, "status": "joining"}


@router.post("/{meeting_id}/leave")
async def leave_meeting(meeting_id: int):
    supervisor = registry.bots.get(meeting_id)
    if supervisor is None:
        raise HTTPException(404, "No bot is running for this meeting")
    try:
        await supervisor.stop()
    except Exception:  # a stop request must never 500 - processing continues regardless
        import logging

        logging.getLogger(__name__).exception("stop raised for meeting %s", meeting_id)
    return {"status": "leaving", "note": "transcription and analysis continue in the background"}


@router.get("/active")
def active_bots(session: Session = Depends(get_session)):
    out = []
    for meeting_id, live in registry.sessions.items():
        meeting = session.get(Meeting, meeting_id)
        out.append(
            {
                "meeting_id": meeting_id,
                "title": meeting.title if meeting else "",
                "status": meeting.status if meeting else None,
                "platform": meeting.platform if meeting else None,
                "started_at": live.started_at,
                "bytes_received": live.bytes_in,
                "listeners": len(live.subscribers),
                "bot_running": meeting_id in registry.bots,
            }
        )
    return out


@router.get("/stuck")
def stuck_meetings(session: Session = Depends(get_session)):
    """Meetings left mid-flight by a crash or restart, so the UI can offer a retry."""
    rows = session.exec(
        select(Meeting).where(
            Meeting.status.in_(  # type: ignore[attr-defined]
                [MeetingStatus.joining, MeetingStatus.recording, MeetingStatus.processing]
            )
        )
    ).all()
    return [m for m in rows if m.id not in registry.sessions and m.id not in registry.bots]
