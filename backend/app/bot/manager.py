"""Runtime registry for live meetings.

A ``LiveSession`` owns one meeting's transcriber, persists final segments, and
fans interim + final captions out to any connected UI clients. ``BotSupervisor``
owns the Playwright process for that meeting and kicks off post-processing when
the bot leaves.
"""
from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..models import Meeting, MeetingStatus, TranscriptSegment
from ..services.transcription import Segment, build_transcriber

log = logging.getLogger(__name__)


class LiveSession:
    """Audio in, captions out, one per active meeting."""

    def __init__(self, meeting_id: int, language: str = "en"):
        self.meeting_id = meeting_id
        self.language = language
        self.audio_path = settings.media_path / f"meeting-{meeting_id}.webm"
        self.subscribers: set[asyncio.Queue] = set()
        self.started_at = datetime.now(timezone.utc)
        self.bytes_in = 0
        self._transcriber = build_transcriber(self._on_segment, self.audio_path, language)
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started:
            return
        await self._transcriber.start()
        self._started = True
        with Session(engine) as session:
            meeting = session.get(Meeting, self.meeting_id)
            if meeting is not None:
                meeting.status = MeetingStatus.recording
                meeting.started_at = meeting.started_at or self.started_at
                meeting.audio_path = str(self.audio_path)
                session.add(meeting)
                session.commit()

    async def feed(self, chunk: bytes) -> None:
        self.bytes_in += len(chunk)
        await self._transcriber.send(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._transcriber.finish()
        except Exception:
            # A transcription crash must never block recording ended_at/duration
            # from being written, or the rest of shutdown (pop_bot, process_meeting)
            # from running - otherwise the meeting is stuck "recording" forever
            # and the bot supervisor leaks in the registry.
            log.exception("transcription failed while closing meeting %s", self.meeting_id)
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        with Session(engine) as session:
            meeting = session.get(Meeting, self.meeting_id)
            if meeting is not None:
                meeting.ended_at = datetime.now(timezone.utc)
                meeting.duration_seconds = int(elapsed)
                session.add(meeting)
                session.commit()
        await self.broadcast({"type": "ended"})

    # -- captions ----------------------------------------------------------- #
    async def _on_segment(self, segment: Segment) -> None:
        if segment.is_final and segment.text.strip():
            with Session(engine) as session:
                session.add(
                    TranscriptSegment(
                        meeting_id=self.meeting_id,
                        speaker=segment.speaker,
                        text=segment.text,
                        start_s=segment.start_s,
                        end_s=segment.end_s,
                        confidence=segment.confidence,
                        is_final=True,
                    )
                )
                session.commit()
        await self.broadcast(
            {
                "type": "segment",
                "speaker": segment.speaker,
                "text": segment.text,
                "start_s": segment.start_s,
                "end_s": segment.end_s,
                "is_final": segment.is_final,
            }
        )

    async def broadcast(self, payload: dict) -> None:
        message = json.dumps(payload)
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)


class BotSupervisor:
    """Owns the browser process for one meeting."""

    def __init__(self, meeting_id: int, meeting_url: str, ingest_ws_url: str, display_name: str | None):
        from .playwright_bot import MeetingBot

        self.meeting_id = meeting_id
        self.bot = MeetingBot(meeting_url, ingest_ws_url, display_name, meeting_id=meeting_id)
        self.task: Optional[asyncio.Task] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"bot-{meeting_id}")

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        from ..services.pipeline import process_meeting

        with Session(engine) as session:
            meeting = session.get(Meeting, self.meeting_id)
            if meeting is not None:
                meeting.status = MeetingStatus.joining
                session.add(meeting)
                session.commit()

        from .playwright_bot import run_bot_sync

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(self._executor, run_bot_sync, self.bot)
        except Exception as exc:
            log.exception("bot crashed for meeting %s", self.meeting_id)
            result = None
            self._fail(f"Bot crashed: {exc}")
        else:
            if not result.joined:
                self._fail(result.reason)
        finally:
            self._executor.shutdown(wait=False)

        session_obj = registry.pop_session(self.meeting_id)
        if session_obj is not None:
            await session_obj.close()

        registry.pop_bot(self.meeting_id)

        if result is not None and result.joined:
            await process_meeting(self.meeting_id)

    def _fail(self, reason: str) -> None:
        with Session(engine) as session:
            meeting = session.get(Meeting, self.meeting_id)
            if meeting is not None:
                meeting.status = MeetingStatus.failed
                meeting.error = reason
                session.add(meeting)
                session.commit()

    async def stop(self) -> None:
        """Ask the bot to leave, wait briefly for the call exit - but NEVER
        cancel the underlying task.

        After the bot leaves, ``_run`` continues into transcription and the
        intelligence pass, which legitimately takes minutes for a long meeting.
        ``shield`` keeps a stop-request (or a timed-out wait) from aborting that
        processing; it just returns while the work finishes in the background.
        Safe to call repeatedly.
        """
        self.bot.request_stop()
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass  # still transcribing/analysing - let it finish on its own


class Registry:
    def __init__(self) -> None:
        self.sessions: dict[int, LiveSession] = {}
        self.bots: dict[int, BotSupervisor] = {}

    def get_or_create_session(self, meeting_id: int, language: str = "en") -> LiveSession:
        session = self.sessions.get(meeting_id)
        if session is None:
            session = LiveSession(meeting_id, language)
            self.sessions[meeting_id] = session
        return session

    def get_session(self, meeting_id: int) -> LiveSession | None:
        return self.sessions.get(meeting_id)

    def pop_session(self, meeting_id: int) -> LiveSession | None:
        return self.sessions.pop(meeting_id, None)

    def pop_bot(self, meeting_id: int) -> BotSupervisor | None:
        return self.bots.pop(meeting_id, None)


registry = Registry()
