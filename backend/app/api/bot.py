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


def launch_bot(
    session: Session,
    meeting_url: str,
    ingest_url_for,
    *,
    title: str | None = None,
    language: str = "en",
    display_name: str | None = None,
) -> Meeting:
    """Create the meeting row and start a bot for it (shared by the API and the
    calendar auto-joiner). ``ingest_url_for(meeting_id)`` builds the socket URL."""
    platform = detect_platform(meeting_url)
    meeting = Meeting(
        title=title or "Live meeting",
        meeting_url=meeting_url,
        platform=platform,
        status=MeetingStatus.joining,
        language=language,
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    # Created up front so the ingest socket has somewhere to deliver audio the
    # instant capture starts, before the bot task gets scheduled.
    registry.get_or_create_session(meeting.id, language)

    supervisor = BotSupervisor(meeting.id, meeting_url, ingest_url_for(meeting.id), display_name)
    registry.bots[meeting.id] = supervisor
    supervisor.start()
    return meeting


@router.post("/join", status_code=202)
async def join_meeting(
    payload: JoinRequest, request: Request, session: Session = Depends(get_session)
):
    try:
        detect_platform(payload.meeting_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    meeting = launch_bot(
        session,
        payload.meeting_url,
        lambda mid: _ingest_url(request, mid),
        title=payload.title,
        language=payload.language,
        display_name=payload.display_name,
    )
    return {"meeting_id": meeting.id, "platform": meeting.platform, "status": "joining"}


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
