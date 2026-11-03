"""Live transcription interface.

The bot streams container-framed audio chunks in; the transcriber pushes
diarized segments out through ``on_segment``. Implementations must tolerate
being fed chunks faster than real time and must never block the ingest loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


@dataclass
class Segment:
    speaker: str
    text: str
    start_s: float
    end_s: float
    confidence: float = 1.0
    is_final: bool = True


SegmentCallback = Callable[[Segment], Awaitable[None]]


class LiveTranscriber(Protocol):
    async def start(self) -> None: ...
    async def send(self, chunk: bytes) -> None: ...
    async def finish(self) -> None: ...
