from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..bot.manager import registry
from ..config import settings
from ..db import get_session
from ..models import (
    ActionItem,
    ChatMessage,
    Meeting,
    MeetingIntelligence,
    MeetingStatus,
    Platform,
    TranscriptSegment,
)
from ..services import progress
from ..services.pipeline import fmt_ts, process_meeting

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


class MeetingCreate(BaseModel):
    title: str = "Untitled meeting"
    meeting_url: str | None = None
    language: str = "en"


class RenameSpeaker(BaseModel):
    old: str
    new: str


class TranscriptImport(BaseModel):
    title: str = "Imported meeting"
    language: str = "en"
    transcript: str


@router.get("")
def list_meetings(session: Session = Depends(get_session)):
    meetings = session.exec(select(Meeting).order_by(Meeting.created_at.desc())).all()
    out = []
    for meeting in meetings:
        intel = session.exec(
            select(MeetingIntelligence).where(MeetingIntelligence.meeting_id == meeting.id)
        ).first()
        open_items = session.exec(
            select(ActionItem).where(ActionItem.meeting_id == meeting.id)
        ).all()
        out.append(
            {
                "id": meeting.id,
                "title": meeting.title,
                "status": meeting.status,
                "platform": meeting.platform,
                "created_at": meeting.created_at,
                "started_at": meeting.started_at,
                "duration_seconds": meeting.duration_seconds,
                "error": meeting.error,
                "summary": (intel.summary if intel else ""),
                "meeting_score": (intel.analytics or {}).get("meeting_score") if intel else None,
                "engagement_score": (intel.analytics or {}).get("engagement_score") if intel else None,
                "speaker_count": (intel.analytics or {}).get("speaker_count") if intel else None,
                "action_item_count": len(open_items),
                "pushed_count": sum(1 for i in open_items if i.external_id),
                "progress": progress.get(meeting.id),
            }
        )
    return out


@router.post("", status_code=201)
def create_meeting(payload: MeetingCreate, session: Session = Depends(get_session)):
    meeting = Meeting(
        title=payload.title,
        meeting_url=payload.meeting_url,
        language=payload.language,
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)
    return meeting


@router.get("/{meeting_id}")
def get_meeting(meeting_id: int, session: Session = Depends(get_session)):
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "Meeting not found")
    segments = session.exec(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_s)
    ).all()
    intel = session.exec(
        select(MeetingIntelligence).where(MeetingIntelligence.meeting_id == meeting_id)
    ).first()
    items = session.exec(
        select(ActionItem).where(ActionItem.meeting_id == meeting_id).order_by(ActionItem.timestamp_s)
    ).all()
    meeting_payload = meeting.model_dump()
    meeting_payload["progress"] = progress.get(meeting_id)
    return {
        "meeting": meeting_payload,
        "segments": segments,
        "intelligence": intel,
        "action_items": items,
        "is_live": registry.get_session(meeting_id) is not None,
    }


@router.get("/{meeting_id}/diagnostics", response_class=PlainTextResponse)
def join_diagnostics(meeting_id: int):
    """The page text the bot saved when a join failed - essential on a server,
    where there is no browser window to look at. Append .png for the screenshot."""
    base = settings.media_path / f"meeting-{meeting_id}-join_failed"
    txt = base.with_suffix(".txt")
    if not txt.exists():
        raise HTTPException(404, "No join diagnostics for this meeting")
    return txt.read_text(encoding="utf-8")


@router.get("/{meeting_id}/diagnostics.png")
def join_diagnostics_png(meeting_id: int):
    from fastapi.responses import FileResponse

    png = settings.media_path / f"meeting-{meeting_id}-join_failed.png"
    if not png.exists():
        raise HTTPException(404, "No join screenshot for this meeting")
    return FileResponse(png, media_type="image/png")


def _delete_meeting_files(meeting_id: int) -> int:
    """Remove every artifact this meeting left in the media directory:
    the recording (bot .webm or uploaded video/audio), the derived .wav,
    and any join-failure diagnostics. Returns files removed."""
    import shutil

    removed = 0
    for path in settings.media_path.glob(f"meeting-{meeting_id}[.-]*"):
        try:
            if path.is_dir():
                shutil.rmtree(path)  # the bot's -frames snapshot directory
            else:
                path.unlink()
            removed += 1
        except OSError:
            pass  # e.g. still open by a running transcription - best effort
    return removed


def _delete_meeting_rows(session: Session, meeting: Meeting) -> None:
    # ChatMessage has no cascading relationship on Meeting - clean it explicitly
    # so Ask-AI history doesn't orphan.
    for msg in session.exec(
        select(ChatMessage).where(ChatMessage.meeting_id == meeting.id)
    ).all():
        session.delete(msg)
    session.delete(meeting)  # segments/intelligence/action items cascade


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int, session: Session = Depends(get_session)):
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "Meeting not found")
    if registry.get_session(meeting_id) is not None or meeting_id in registry.bots:
        raise HTTPException(409, "This meeting is live - stop the recording first")
    _delete_meeting_rows(session, meeting)
    session.commit()
    _delete_meeting_files(meeting_id)


@router.delete("", status_code=200)
def clear_all_meetings(session: Session = Depends(get_session)):
    """Delete every meeting, its extracted data, and its recordings.

    Live meetings are skipped, not killed - stop them first if you want
    everything gone.
    """
    meetings = session.exec(select(Meeting)).all()
    deleted, files_removed, skipped_live = 0, 0, []
    for meeting in meetings:
        if registry.get_session(meeting.id) is not None or meeting.id in registry.bots:
            skipped_live.append(meeting.id)
            continue
        _delete_meeting_rows(session, meeting)
        deleted += 1
    session.commit()
    for meeting in meetings:
        if meeting.id not in skipped_live:
            files_removed += _delete_meeting_files(meeting.id)
    return {"deleted": deleted, "files_removed": files_removed, "skipped_live": skipped_live}


@router.post("/{meeting_id}/reprocess")
async def reprocess(
    meeting_id: int,
    tasks: BackgroundTasks,
    retranscribe: bool = False,
    session: Session = Depends(get_session),
):
    """Re-run the intelligence pass.

    With ``retranscribe=true`` and a kept recording, the stored transcript is
    dropped first so transcription runs again from the audio - the way to pick
    up newly enabled diarization (HF_TOKEN) or a provider change on an old
    meeting.
    """
    import os

    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "Meeting not found")
    if retranscribe:
        if not (meeting.audio_path and os.path.exists(meeting.audio_path)):
            raise HTTPException(400, "No recording kept for this meeting - cannot re-transcribe")
        for seg in session.exec(
            select(TranscriptSegment).where(TranscriptSegment.meeting_id == meeting_id)
        ).all():
            session.delete(seg)
        session.commit()
    tasks.add_task(process_meeting, meeting_id)
    return {"status": "queued", "retranscribe": retranscribe}


@router.post("/{meeting_id}/rename-speaker")
def rename_speaker(
    meeting_id: int,
    payload: RenameSpeaker,
    tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Rename a speaker across the whole meeting - transcript and analytics.

    The automatic naming only maps speakers with direct conversational evidence;
    this is the human override for everyone it (correctly) left as 'Speaker N'.
    """
    new = payload.new.strip()
    if not new:
        raise HTTPException(400, "New name is empty")
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(404, "Meeting not found")

    renamed = 0
    for seg in session.exec(
        select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == meeting_id,
            TranscriptSegment.speaker == payload.old,
        )
    ).all():
        seg.speaker = new
        session.add(seg)
        renamed += 1
    if renamed == 0:
        raise HTTPException(404, f"No segments for speaker {payload.old!r}")

    intel = session.exec(
        select(MeetingIntelligence).where(MeetingIntelligence.meeting_id == meeting_id)
    ).first()
    if intel is not None and intel.analytics:
        analytics = dict(intel.analytics)
        analytics["speakers"] = [
            {**sp, "speaker": new if sp.get("speaker") == payload.old else sp.get("speaker")}
            for sp in analytics.get("speakers", [])
        ]
        analytics["speaker_name_map"] = {
            **(analytics.get("speaker_name_map") or {}),
            payload.old: new,
        }
        intel.analytics = analytics  # reassign: JSON columns don't track mutation
        session.add(intel)

    for item in session.exec(
        select(ActionItem).where(
            ActionItem.meeting_id == meeting_id, ActionItem.owner == payload.old
        )
    ).all():
        item.owner = new
        session.add(item)

    session.commit()

    # A manual correction is the best training signal there is - enroll this
    # voice under its corrected name for future meetings.
    if meeting.audio_path:
        from pathlib import Path as _Path

        from ..services.pipeline import _load_segment_views
        from ..services import voiceprints as _voice

        wav_guess = _Path(meeting.audio_path).with_suffix(".wav")
        voice_wav = wav_guess if wav_guess.exists() else _Path(meeting.audio_path)
        if voice_wav.exists():
            views = _load_segment_views(meeting_id)
            tasks.add_task(_voice.update_registry, voice_wav, views, [new])
    return {"renamed_segments": renamed, "speaker": new}


@router.post("/import", status_code=201)
def import_transcript(
    payload: TranscriptImport, tasks: BackgroundTasks, session: Session = Depends(get_session)
):
    """Accept an existing transcript instead of recording one.

    Understands `[MM:SS] Speaker: text` (Reed AI's own export format), plain
    `Speaker: text`, and unlabelled prose.
    """
    segments = parse_transcript(payload.transcript)
    if not segments:
        raise HTTPException(400, "Could not parse any transcript lines")

    meeting = Meeting(
        title=payload.title,
        platform=Platform.upload,
        status=MeetingStatus.processing,
        language=payload.language,
        started_at=datetime.now(timezone.utc),
        duration_seconds=int(segments[-1][3]),
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    for speaker, text, start, end in segments:
        session.add(
            TranscriptSegment(
                meeting_id=meeting.id,
                speaker=speaker,
                text=text,
                start_s=start,
                end_s=end,
            )
        )
    session.commit()

    tasks.add_task(process_meeting, meeting.id)
    return {"id": meeting.id, "segments": len(segments)}


UPLOAD_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",          # video
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", # audio
}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


@router.post("/upload", status_code=201)
async def upload_recording(
    tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form("Uploaded recording"),
    session: Session = Depends(get_session),
):
    """Analyse an uploaded meeting recording (video or audio).

    The audio track is demuxed out of whatever container arrives; transcription
    then runs through the same provider-aware path as a live meeting (Sarvam
    when configured, local Whisper otherwise), followed by the full Claude
    intelligence pass.
    """
    from pathlib import Path

    ext = Path(file.filename or "").suffix.lower()
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type {ext or '(none)'}. "
            f"Accepted: {', '.join(sorted(UPLOAD_EXTENSIONS))}",
        )

    meeting = Meeting(
        title=title or "Uploaded recording",
        platform=Platform.upload,
        status=MeetingStatus.processing,
        started_at=datetime.now(timezone.utc),
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    dest = settings.media_path / f"meeting-{meeting.id}-upload{ext}"
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File exceeds the 2 GB upload limit")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        session.delete(meeting)
        session.commit()
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        session.delete(meeting)
        session.commit()
        raise HTTPException(500, f"Could not store the upload: {exc}") from exc

    meeting.audio_path = str(dest)
    session.add(meeting)
    session.commit()

    # process_meeting finds no stored segments, transcribes from audio_path,
    # then runs the intelligence pass - the same recovery path Re-run uses.
    tasks.add_task(process_meeting, meeting.id)
    return {"id": meeting.id, "bytes": written, "status": "processing"}


TS_LINE = re.compile(r"^\s*\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\]?\s*(?:-\s*)?(.*)$")
SPEAKER_LINE = re.compile(r"^\s*([A-Z][\w .'-]{0,40}?)\s*:\s*(.+)$")


def parse_transcript(raw: str) -> list[tuple[str, str, float, float]]:
    segments: list[tuple[str, str, float, float]] = []
    clock = 0.0
    for line in raw.splitlines():
        if not line.strip():
            continue
        start = None
        body = line.strip()

        match = TS_LINE.match(body)
        if match and match.group(4):
            if match.group(3):  # HH:MM:SS
                start = (
                    int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
                )
            else:  # MM:SS
                start = int(match.group(1)) * 60 + int(match.group(2))
            body = match.group(4).strip()

        speaker = "Speaker 0"
        speaker_match = SPEAKER_LINE.match(body)
        if speaker_match:
            speaker, body = speaker_match.group(1).strip(), speaker_match.group(2).strip()

        if not body:
            continue
        span = max(2.0, len(body.split()) / 2.6)
        start_s = float(start) if start is not None else clock
        segments.append((speaker, body, start_s, start_s + span))
        clock = start_s + span + 0.3
    return segments


@router.get("/{meeting_id}/transcript.txt", response_class=PlainTextResponse)
def transcript_txt(meeting_id: int, session: Session = Depends(get_session)):
    segments = session.exec(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_s)
    ).all()
    return "\n".join(f"[{fmt_ts(s.start_s)}] {s.speaker}: {s.text}" for s in segments)


@router.get("/{meeting_id}/transcript.vtt", response_class=PlainTextResponse)
def transcript_vtt(meeting_id: int, session: Session = Depends(get_session)):
    segments = session.exec(
        select(TranscriptSegment)
        .where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_s)
    ).all()

    def vtt_ts(seconds: float) -> str:
        ms = int((seconds - int(seconds)) * 1000)
        s = int(seconds)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}.{ms:03d}"

    lines = ["WEBVTT", ""]
    for seg in segments:
        lines += [f"{vtt_ts(seg.start_s)} --> {vtt_ts(seg.end_s)}", f"<v {seg.speaker}>{seg.text}", ""]
    return "\n".join(lines)


@router.get("/{meeting_id}/live")
async def live_captions(meeting_id: int):
    """Server-sent events: interim + final captions while the bot is in the call."""
    live = registry.get_session(meeting_id)
    if live is None:
        raise HTTPException(404, "This meeting is not live")
    queue = live.subscribe()

    async def stream():
        try:
            yield 'data: {"type":"connected"}\n\n'
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {message}\n\n"
        finally:
            live.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
