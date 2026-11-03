"""Deepgram streaming transcription with speaker diarization.

Audio arrives as WebM/Opus chunks from the browser's MediaRecorder; Deepgram
auto-detects the container, so no encoding parameters are sent. Words come back
tagged with a speaker index, which we collapse into contiguous same-speaker
segments.
"""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlencode

import websockets

from ...config import settings
from .base import Segment, SegmentCallback

log = logging.getLogger(__name__)

DG_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramTranscriber:
    def __init__(self, on_segment: SegmentCallback, language: str = "en"):
        self.on_segment = on_segment
        self.language = language
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._closed = asyncio.Event()

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        if not settings.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set")

        query = urlencode(
            {
                "model": settings.deepgram_model,
                "language": self.language,
                "diarize": "true",
                "punctuate": "true",
                "smart_format": "true",
                "interim_results": "true",
                "utterance_end_ms": "1000",
                "vad_events": "true",
            }
        )
        self._ws = await websockets.connect(
            f"{DG_URL}?{query}",
            additional_headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            max_size=None,
            ping_interval=5,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._keepalive = asyncio.create_task(self._keepalive_loop())
        log.info("deepgram stream open (model=%s)", settings.deepgram_model)

    async def send(self, chunk: bytes) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(chunk)
        except websockets.ConnectionClosed:
            log.warning("deepgram connection closed while sending")

    async def finish(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except websockets.ConnectionClosed:
                pass
        for task in (self._keepalive, self._reader):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            await self._ws.close()
        self._closed.set()

    # -- internals -------------------------------------------------------- #
    async def _keepalive_loop(self) -> None:
        # Deepgram closes idle sockets after 10s; a muted meeting is still a meeting.
        try:
            while True:
                await asyncio.sleep(5)
                if self._ws is not None:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if payload.get("type") != "Results":
                    continue
                for segment in self._to_segments(payload):
                    await self.on_segment(segment)
        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed:
            log.info("deepgram stream ended")

    @staticmethod
    def _to_segments(payload: dict) -> list[Segment]:
        is_final = bool(payload.get("is_final"))
        alternatives = payload.get("channel", {}).get("alternatives", [])
        if not alternatives:
            return []
        words = alternatives[0].get("words", [])
        if not words:
            return []

        segments: list[Segment] = []
        current: dict | None = None
        for word in words:
            speaker = f"Speaker {word.get('speaker', 0)}"
            token = word.get("punctuated_word") or word.get("word", "")
            if current is None or current["speaker"] != speaker:
                if current is not None:
                    segments.append(_finalise(current, is_final))
                current = {
                    "speaker": speaker,
                    "tokens": [token],
                    "start": float(word.get("start", 0.0)),
                    "end": float(word.get("end", 0.0)),
                    "conf": [float(word.get("confidence", 1.0))],
                }
            else:
                current["tokens"].append(token)
                current["end"] = float(word.get("end", current["end"]))
                current["conf"].append(float(word.get("confidence", 1.0)))
        if current is not None:
            segments.append(_finalise(current, is_final))
        return segments


def _finalise(current: dict, is_final: bool) -> Segment:
    confidences = current["conf"] or [1.0]
    return Segment(
        speaker=current["speaker"],
        text=" ".join(current["tokens"]).strip(),
        start_s=current["start"],
        end_s=current["end"],
        confidence=sum(confidences) / len(confidences),
        is_final=is_final,
    )
