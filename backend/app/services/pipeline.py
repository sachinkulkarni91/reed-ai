"""Post-meeting processing.

    transcript -> Claude extraction -> analytics -> persistence -> Trello push

Runs as a background task once the bot leaves the call. SQLite work is done with
short-lived synchronous sessions; the database is local and the writes are tiny,
so the event-loop cost is not worth a thread hop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import (
    ActionItem,
    ActionItemStatus,
    Meeting,
    MeetingIntelligence,
    MeetingStatus,
    TranscriptSegment,
)
from . import analytics as analytics_mod
from . import llm
from . import progress
from .trello import (
    PRIORITY_COLORS,
    TrelloClient,
    TrelloError,
    build_description,
    match_member,
)

log = logging.getLogger(__name__)


@dataclass
class SegmentView:
    """Detached copy of a transcript row.

    Analytics runs after the session that loaded the rows has closed, and
    SQLAlchemy expires ORM instances on commit - so snapshot the fields we need
    while the session is still open rather than risking a lazy refresh on a dead
    connection.
    """

    speaker: str
    text: str
    start_s: float
    end_s: float


def fmt_ts(seconds: float) -> str:
    total = int(seconds or 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def render_transcript(segments: list[SegmentView]) -> str:
    return "\n".join(
        f"[{fmt_ts(s.start_s)}] {s.speaker}: {s.text}"
        for s in sorted(segments, key=lambda s: s.start_s)
        if s.text.strip()
    )


def meeting_meta(meeting: Meeting, segments: list[SegmentView]) -> str:
    speakers = sorted({s.speaker for s in segments})
    date = (meeting.started_at or meeting.created_at).date().isoformat()
    return (
        f"Title: {meeting.title}\n"
        f"Date: {date}\n"
        f"Platform: {meeting.platform.value}\n"
        f"Duration: {fmt_ts(meeting.duration_seconds)}\n"
        f"Speakers detected: {', '.join(speakers) or 'unknown'}"
    )


def _no_transcript_reason(meeting: Meeting) -> str:
    """Empty transcripts have two very different causes - tell them apart.

    'Audio was recorded but no speech was found' (silent room, everyone muted,
    only notification chimes) is expected behaviour worth explaining; 'no audio
    was recorded at all' points at a capture problem worth debugging.
    """
    import os

    if meeting.audio_path and os.path.exists(meeting.audio_path):
        size = os.path.getsize(meeting.audio_path)
        if size > 50_000:  # more than a couple seconds of Opus audio
            return (
                f"Audio was recorded ({size // 1024} KB) but no speech was detected in it - "
                "the room may have been silent, everyone muted, or only join/notification "
                "chimes played. The recording is kept at "
                f"{meeting.audio_path}; use Re-run analysis after checking it."
            )
        return "Almost no audio was captured - the capture stream opened but stayed empty."
    return "No transcript was captured and no audio file was recorded."


def _mark_failed(meeting_id: int, reason: str) -> None:
    with Session(engine) as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting is not None:
            meeting.status = MeetingStatus.failed
            meeting.error = reason
            session.add(meeting)
            session.commit()


def _load_segment_views(meeting_id: int) -> list[SegmentView]:
    with Session(engine) as session:
        rows = session.exec(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == meeting_id, TranscriptSegment.is_final == True)  # noqa: E712
            .order_by(TranscriptSegment.start_s)
        ).all()
        return [SegmentView(s.speaker, s.text, s.start_s, s.end_s) for s in rows]


async def _transcribe_from_audio(meeting_id: int, audio_path: str | None) -> int:
    """Transcribe a meeting's kept recording into TranscriptSegment rows.

    Recovery path for 'Re-run analysis': the live transcription may have
    crashed (wrong interpreter, missing dependency) after the audio was safely
    written. Returns the number of segments stored.
    """
    import os

    if not audio_path or not os.path.exists(audio_path) or os.path.getsize(audio_path) < 50_000:
        return 0

    from pathlib import Path

    from .transcription import build_file_transcriber

    stored = 0

    async def on_segment(segment) -> None:
        nonlocal stored
        if not segment.is_final or not segment.text.strip():
            return
        with Session(engine) as session:
            session.add(
                TranscriptSegment(
                    meeting_id=meeting_id,
                    speaker=segment.speaker,
                    text=segment.text,
                    start_s=segment.start_s,
                    end_s=segment.end_s,
                    confidence=segment.confidence,
                    is_final=True,
                )
            )
            session.commit()
        stored += 1

    progress.report(meeting_id, "Preparing audio")
    log.info("transcribing meeting %s from %s", meeting_id, audio_path)
    transcriber = build_file_transcriber(on_segment, Path(audio_path), meeting_id=meeting_id)
    await transcriber.finish()  # no start(): the file already exists in full
    return stored


async def process_meeting(meeting_id: int, auto_push: bool | None = None) -> None:
    """Run the full intelligence pass. Safe to re-run; it replaces prior output."""
    try:
        await _process_meeting_inner(meeting_id, auto_push)
    finally:
        progress.clear(meeting_id)


async def _process_meeting_inner(meeting_id: int, auto_push: bool | None = None) -> None:
    with Session(engine) as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting is None:
            log.error("meeting %s vanished before processing", meeting_id)
            return
        audio_path = meeting.audio_path
        meeting.status = MeetingStatus.processing
        session.add(meeting)
        session.commit()

    views = _load_segment_views(meeting_id)
    if not views:
        # The recording may still be on disk even though no transcript made it
        # to the database (e.g. the live transcription crashed). Try it before
        # declaring failure - this is what lets Re-run analysis recover.
        try:
            await _transcribe_from_audio(meeting_id, audio_path)
        except Exception:
            log.exception("audio re-transcription failed for meeting %s", meeting_id)
        views = _load_segment_views(meeting_id)

    if not views:
        with Session(engine) as session:
            meeting = session.get(Meeting, meeting_id)
            reason = _no_transcript_reason(meeting) if meeting is not None else "Meeting not found"
        _mark_failed(meeting_id, reason)
        return

    with Session(engine) as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting is None:
            return
        transcript = render_transcript(views)
        meta = meeting_meta(meeting, views)
        duration = meeting.duration_seconds or max(v.end_s for v in views)

    progress.report(meeting_id, "Analysing with Claude")
    try:
        extraction = await llm.extract_intelligence(transcript, meta)
    except Exception as exc:
        log.exception("extraction failed for meeting %s", meeting_id)
        with Session(engine) as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting is not None:
                meeting.status = MeetingStatus.failed
                meeting.error = f"Intelligence pass failed: {exc}"
                session.add(meeting)
                session.commit()
        return

    # Speaker naming: the model maps diarized labels to real names from
    # conversational evidence (people addressing each other, self-intros).
    # Apply confident mappings everywhere - stored transcript, analytics
    # inputs - so the whole app speaks in names, not "Speaker 2".
    conv_map = {
        m.label: m.name.strip()
        for m in extraction.speaker_names
        if m.confidence >= 0.7 and m.name.strip() and m.name.strip() != m.label
    }

    # Screen evidence - DOM active-speaker samples, video frames, bot snapshots -
    # runs for ALL diarized clusters, not just unnamed ones: tiles carry the
    # authoritative spelling ('Mehak', not the phonetic 'Mehek' the conversation
    # layer heard) and can tag people nobody addressed by name.
    screen_map: dict = {}
    if settings.vision_speaker_names and audio_path:
        clusters = sorted({v.speaker for v in views if v.speaker.startswith("Speaker ")})
        if clusters:
            try:
                import json as _json
                from pathlib import Path as _Path

                from . import progress as _progress
                from . import vision_names as _vision
                from . import voiceprints as _voice

                # Layer 0: voice fingerprints - known voices name instantly,
                # even from a few seconds of speech, before any token is spent.
                wav_guess = _Path(audio_path).with_suffix(".wav")
                voice_wav = wav_guess if wav_guess.exists() else _Path(audio_path)
                _progress.report(meeting_id, "Matching known voices")
                screen_map.update(await _voice.match_registry(voice_wav, views, clusters))

                names_json = settings.media_path / f"meeting-{meeting_id}-names.json"
                if names_json.exists():
                    dom_votes = _vision.vote_dom_samples(
                        _json.loads(names_json.read_text(encoding="utf-8")), views, clusters
                    )
                    for k, v in dom_votes.items():
                        screen_map.setdefault(k, v)

                need = [c for c in clusters if c not in screen_map]
                path = _Path(audio_path)
                if need and path.exists() and _vision.has_video_stream(path):
                    _progress.report(meeting_id, "Reading names from video frames")
                    screen_map.update(await _vision.infer_names_from_video(str(path), views, need))
                    need = [c for c in clusters if c not in screen_map]

                frames_dir = settings.media_path / f"meeting-{meeting_id}-frames"
                if need and frames_dir.is_dir():
                    _progress.report(meeting_id, "Reading names from meeting snapshots")
                    screen_map.update(await _vision.infer_names_from_snapshots(frames_dir, views, need))
            except Exception:
                log.exception("vision speaker naming failed for meeting %s", meeting_id)

    from .vision_names import merge_names as _merge_names

    name_map, spelling_alias = _merge_names(conv_map, screen_map)
    # Aliases ("Mehek" -> "Mehak Tarnaich") ride along so prose written with the
    # phonetic spelling gets corrected by the same text pass below.
    name_map = {**name_map, **spelling_alias}
    if name_map:
        log.info("meeting %s speaker names: %s", meeting_id, name_map)

    if name_map:
        log.info("meeting %s speaker names: %s", meeting_id, name_map)
        with Session(engine) as session:
            for row in session.exec(
                select(TranscriptSegment).where(
                    TranscriptSegment.meeting_id == meeting_id,
                    TranscriptSegment.speaker.in_(name_map.keys()),  # type: ignore[attr-defined]
                )
            ).all():
                row.speaker = name_map[row.speaker]
                session.add(row)
            session.commit()
        for v in views:
            v.speaker = name_map.get(v.speaker, v.speaker)

    if name_map:
        # The transcript and analytics are renamed above - but every other
        # artifact carries labels too: asked_by fields, highlight speakers, and
        # literal "Speaker N" strings inside the prose. Map them all, so no
        # surface of the app still says "Speaker 1" for a person we can name.
        import re as _re

        def _mapped(text: str) -> str:
            for old in sorted(name_map, key=len, reverse=True):
                text = _re.sub(rf"{_re.escape(old)}", name_map[old], text)
            for new_name in name_map.values():
                text = text.replace(f"{new_name} ({new_name})", new_name)
            return text

        extraction.headline = _mapped(extraction.headline)
        extraction.summary = _mapped(extraction.summary)
        extraction.detailed_notes = _mapped(extraction.detailed_notes)
        extraction.recap_email = _mapped(extraction.recap_email)
        extraction.key_points = [_mapped(point) for point in extraction.key_points]
        extraction.score_rationale = _mapped(extraction.score_rationale)
        for d in extraction.decisions:
            d.decision, d.context = _mapped(d.decision), _mapped(d.context)
        for q in extraction.key_questions:
            q.question, q.answer = _mapped(q.question), _mapped(q.answer)
            if q.asked_by:
                q.asked_by = name_map.get(q.asked_by, _mapped(q.asked_by))
        for h in extraction.highlights:
            h.speaker = name_map.get(h.speaker, h.speaker)
            h.why = _mapped(h.why)
        for c in extraction.chapters:
            c.title, c.summary = _mapped(c.title), _mapped(c.summary)
        for point in extraction.sentiment_timeline:
            point.label = _mapped(point.label)
        for item in extraction.action_items:
            if item.owner:
                item.owner = name_map.get(item.owner, _mapped(item.owner))
            item.title = _mapped(item.title)
            item.description = _mapped(item.description)

    # Enroll/refresh voice fingerprints for everyone now named, so future
    # meetings recognise them from seconds of speech.
    if audio_path:
        try:
            from pathlib import Path as _Path

            from . import voiceprints as _voice

            wav_guess = _Path(audio_path).with_suffix(".wav")
            voice_wav = wav_guess if wav_guess.exists() else _Path(audio_path)
            named_now = sorted({v.speaker for v in views if not v.speaker.startswith("Speaker ")})
            await _voice.update_registry(voice_wav, views, named_now)
        except Exception:
            log.exception("voiceprint enrollment failed for meeting %s", meeting_id)

    insights = [
        {**i.model_dump(), "speaker": name_map.get(i.speaker, i.speaker)}
        for i in extraction.speaker_insights
    ]

    try:
        computed = analytics_mod.compute(views, duration, insights)
        computed["engagement_score"] = analytics_mod.engagement_score(
            computed, extraction.overall_sentiment, computed["questions_total"]
        )
        computed["meeting_score"] = extraction.meeting_score
        computed["score_rationale"] = extraction.score_rationale
        computed["overall_sentiment"] = extraction.overall_sentiment
        computed["sentiment_timeline"] = [p.model_dump() for p in extraction.sentiment_timeline]
        computed["speaker_name_map"] = name_map  # transparency: who was renamed from what

        with Session(engine) as session:
            meeting = session.get(Meeting, meeting_id)
            if meeting is None:
                return

            existing = session.exec(
                select(MeetingIntelligence).where(MeetingIntelligence.meeting_id == meeting_id)
            ).first()
            if existing is not None:
                session.delete(existing)
                session.commit()

            session.add(
                MeetingIntelligence(
                    meeting_id=meeting_id,
                    headline=extraction.headline,
                    summary=extraction.summary,
                    detailed_notes=extraction.detailed_notes,
                    recap_email=extraction.recap_email,
                    key_points=extraction.key_points,
                    decisions=[d.model_dump() for d in extraction.decisions],
                    key_questions=[q.model_dump() for q in extraction.key_questions],
                    risks=[r.model_dump() for r in extraction.risks],
                    topics=[t.model_dump() for t in extraction.topics],
                    chapters=[c.model_dump() for c in extraction.chapters],
                    highlights=[h.model_dump() for h in extraction.highlights],
                    analytics=computed,
                    model_used=settings.reed_model,
                )
            )

            # Replace only auto-extracted items that were never pushed anywhere.
            for stale in session.exec(
                select(ActionItem).where(
                    ActionItem.meeting_id == meeting_id,
                    ActionItem.external_id == None,  # noqa: E711
                )
            ).all():
                session.delete(stale)

            for item in extraction.action_items:
                session.add(
                    ActionItem(
                        meeting_id=meeting_id,
                        title=item.title,
                        description=item.description,
                        owner=item.owner,
                        owner_email=item.owner_email,
                        due_date=item.due_date,
                        priority=item.priority if item.priority in {"low", "medium", "high"} else "medium",
                        quote=item.quote,
                        timestamp_s=item.timestamp_s,
                        confidence=item.confidence,
                    )
                )

            if extraction.headline and meeting.title in ("", "Untitled meeting"):
                meeting.title = extraction.headline
            meeting.status = MeetingStatus.completed
            meeting.duration_seconds = int(duration)
            meeting.error = None
            session.add(meeting)
            session.commit()
    except Exception as exc:
        log.exception("finalization failed for meeting %s", meeting_id)
        _mark_failed(meeting_id, f"Intelligence finalization failed: {exc}")
        return

    should_push = settings.trello_auto_push if auto_push is None else auto_push
    if should_push:
        try:
            await push_action_items(meeting_id)
        except Exception:
            log.exception("auto-push to Trello failed for meeting %s", meeting_id)


async def push_action_items(
    meeting_id: int,
    item_ids: list[int] | None = None,
    list_id: str | None = None,
) -> dict:
    """Push open action items to Trello. Returns a per-item result summary."""
    target_list = list_id or settings.trello_list_id
    if not target_list:
        raise TrelloError("No Trello list configured. Set TRELLO_LIST_ID or pass list_id.")

    client = TrelloClient()
    board_id = await client.board_id_for_list(target_list)
    members = await client.members(board_id)

    with Session(engine) as session:
        meeting = session.get(Meeting, meeting_id)
        if meeting is None:
            raise ValueError(f"Meeting {meeting_id} not found")
        query = select(ActionItem).where(ActionItem.meeting_id == meeting_id)
        if item_ids:
            query = query.where(ActionItem.id.in_(item_ids))  # type: ignore[attr-defined]
        items = session.exec(query).all()

        results = []
        for item in items:
            if item.external_id:
                results.append({"id": item.id, "status": "skipped", "reason": "already pushed",
                                "url": item.external_url})
                continue
            if item.status == ActionItemStatus.dismissed:
                results.append({"id": item.id, "status": "skipped", "reason": "dismissed"})
                continue
            try:
                label_id = await client.ensure_label(
                    board_id,
                    f"Priority: {item.priority}",
                    PRIORITY_COLORS.get(item.priority, "yellow"),
                )
                card = await client.create_card(
                    list_id=target_list,
                    name=item.title,
                    desc=build_description(item, meeting),
                    due=item.due_date,
                    member_ids=match_member(members, item.owner, item.owner_email),
                    label_ids=[label_id] if label_id else None,
                )
                if item.quote:
                    await client.comment(card["id"], f'Said in the meeting: "{item.quote}"')

                item.external_id = card["id"]
                item.external_url = card.get("shortUrl") or card.get("url")
                item.status = ActionItemStatus.pushed
                item.pushed_at = datetime.now(timezone.utc)
                item.push_error = None
                results.append({"id": item.id, "status": "pushed", "url": item.external_url})
            except Exception as exc:
                log.warning("push failed for action item %s: %s", item.id, exc)
                item.status = ActionItemStatus.failed
                item.push_error = str(exc)
                results.append({"id": item.id, "status": "failed", "error": str(exc)})
            session.add(item)

        session.commit()

    pushed = sum(1 for r in results if r["status"] == "pushed")
    return {"pushed": pushed, "total": len(results), "results": results}
