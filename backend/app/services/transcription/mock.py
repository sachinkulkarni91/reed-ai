"""Offline transcriber.

Lets the whole pipeline - bot, ingest socket, live UI, intelligence pass, Trello
push - be exercised without a Deepgram key. Audio is written to disk and a
scripted two-speaker conversation is emitted on the same cadence a real stream
would produce.
"""
from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

from .base import Segment, SegmentCallback

SCRIPT = [
    ("Speaker 0", "Alright, let's start with the migration status."),
    ("Speaker 1", "Backend is done. The read path is fully on the new service."),
    ("Speaker 0", "What's left on the write path?"),
    ("Speaker 1", "Dual writes are live but we haven't cut over yet."),
    ("Speaker 0", "I'll write up the cutover plan and send it round by Friday."),
    ("Speaker 1", "Can you include the rollback steps? That's the part I'm worried about."),
    ("Speaker 0", "Yes. Rollback and the on-call runbook, both in the same doc."),
    ("Speaker 1", "Then I'll book the maintenance window with infra next week."),
    ("Speaker 0", "Decision then: we cut over Tuesday, and we hold the release train until it's green."),
    ("Speaker 1", "Agreed. I'll update the roadmap doc after this call."),
]


class MockTranscriber:
    def __init__(self, on_segment: SegmentCallback, audio_path: Path | None = None, **_: object):
        self.on_segment = on_segment
        self.audio_path = audio_path
        self._task: asyncio.Task | None = None
        self._fh = None

    async def start(self) -> None:
        if self.audio_path is not None:
            self.audio_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.audio_path.open("wb")
        self._task = asyncio.create_task(self._emit())

    async def send(self, chunk: bytes) -> None:
        if self._fh is not None:
            self._fh.write(chunk)

    async def finish(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._fh is not None:
            self._fh.close()

    async def _emit(self) -> None:
        clock = 0.0
        try:
            for speaker, text in itertools.cycle(SCRIPT):
                await asyncio.sleep(4)
                span = max(2.0, len(text.split()) / 2.6)
                await self.on_segment(
                    Segment(
                        speaker=speaker,
                        text=text,
                        start_s=clock,
                        end_s=clock + span,
                        confidence=0.95,
                        is_final=True,
                    )
                )
                clock += span + 0.4
        except asyncio.CancelledError:
            pass
