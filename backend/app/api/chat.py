"""Ask-AI copilot: per-meeting Q&A and cross-meeting search, both streamed."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import engine, get_session
from ..models import ChatMessage, Meeting, MeetingIntelligence, TranscriptSegment
from ..services import llm
from ..services.pipeline import meeting_meta, render_transcript

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_TURNS = 12


class AskRequest(BaseModel):
    question: str


@router.get("/{meeting_id}/history")
def history(meeting_id: int, session: Session = Depends(get_session)):
    return session.exec(
        select(ChatMessage)
        .where(ChatMessage.meeting_id == meeting_id)
        .order_by(ChatMessage.created_at)
    ).all()


@router.post("/{meeting_id}/ask")
async def ask(meeting_id: int, payload: AskRequest):
    with Session(engine) as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting is None:
            raise HTTPException(404, "Meeting not found")
        segments = session.exec(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id)
            .order_by(TranscriptSegment.start_s)
        ).all()
        if not segments:
            raise HTTPException(400, "This meeting has no transcript yet")

        prior = session.exec(
            select(ChatMessage)
            .where(ChatMessage.meeting_id == meeting_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(HISTORY_TURNS)
        ).all()
        history_msgs = [{"role": m.role, "content": m.content} for m in reversed(prior)]

        transcript = render_transcript(list(segments))
        meta = meeting_meta(meeting, list(segments))

        session.add(ChatMessage(meeting_id=meeting_id, role="user", content=payload.question))
        session.commit()

    async def stream():
        collected: list[str] = []
        async for chunk in llm.stream_chat(payload.question, transcript, meta, history_msgs):
            collected.append(chunk)
            yield chunk
        with Session(engine) as session:
            session.add(
                ChatMessage(meeting_id=meeting_id, role="assistant", content="".join(collected))
            )
            session.commit()

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


@router.post("/search")
async def search(payload: AskRequest):
    """Ask across every processed meeting, using summaries rather than raw transcripts."""
    with Session(engine) as session:
        meetings = session.exec(select(Meeting).order_by(Meeting.created_at.desc()).limit(100)).all()
        blocks: list[str] = []
        for meeting in meetings:
            intel = session.exec(
                select(MeetingIntelligence).where(MeetingIntelligence.meeting_id == meeting.id)
            ).first()
            if intel is None:
                continue
            date = (meeting.started_at or meeting.created_at).date().isoformat()
            decisions = "; ".join(d.get("decision", "") for d in (intel.decisions or []))
            blocks.append(
                f"## Meeting {meeting.id} - {meeting.title} ({date})\n"
                f"{intel.summary}\n"
                f"Key points: {' | '.join(intel.key_points or [])}\n"
                f"Decisions: {decisions}"
            )

    if not blocks:
        raise HTTPException(400, "No processed meetings to search yet")

    return StreamingResponse(
        llm.stream_search(payload.question, "\n\n".join(blocks)),
        media_type="text/plain; charset=utf-8",
    )
