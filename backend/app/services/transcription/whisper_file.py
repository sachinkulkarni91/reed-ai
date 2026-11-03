"""Record-then-transcribe fallback using faster-whisper.

Trades live captions for zero streaming cost and full offline operation: audio
is buffered to disk during the meeting and transcribed in one pass at the end.

Diarization: Whisper itself does not know who is speaking. When ``HF_TOKEN`` is
set (in .env or the environment), ``pyannote.audio`` assigns speaker turns and
each Whisper segment is labelled by whichever speaker held the floor at its
midpoint. Without a token everything is "Speaker 0" and talk-time analytics are
not meaningful.

Both models are loaded once per process and reused across meetings.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from ...config import settings
from .. import progress
from .base import Segment, SegmentCallback

log = logging.getLogger(__name__)

_model_lock = threading.Lock()
_whisper_model = None
_diarization_pipeline = None
_diarization_failed = False


def _get_whisper(model_size: str):
    global _whisper_model
    with _model_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel

            log.info("loading faster-whisper model '%s' (first run downloads it)", model_size)
            _whisper_model = WhisperModel(model_size, device="auto", compute_type="int8")
        return _whisper_model


def _get_diarizer():
    """pyannote pipeline, loaded once; returns None when unavailable."""
    global _diarization_pipeline, _diarization_failed
    token = settings.hf_token or os.environ.get("HF_TOKEN")
    if not token or _diarization_failed:
        return None
    with _model_lock:
        if _diarization_pipeline is None:
            try:
                from pyannote.audio import Pipeline

                log.info("loading pyannote speaker-diarization-3.1 (first run downloads it)")
                try:
                    # pyannote.audio >= 4 renamed the auth kwarg
                    _diarization_pipeline = Pipeline.from_pretrained(
                        "pyannote/speaker-diarization-3.1", token=token
                    )
                except TypeError:
                    _diarization_pipeline = Pipeline.from_pretrained(
                        "pyannote/speaker-diarization-3.1", use_auth_token=token
                    )
            except Exception as exc:
                _diarization_failed = True
                log.warning(
                    "diarization unavailable (%s). Transcripts will use a single "
                    "'Speaker 0' label. Check HF_TOKEN and that you accepted the "
                    "model terms at hf.co/pyannote/speaker-diarization-3.1",
                    exc,
                )
                return None
        return _diarization_pipeline


class WhisperFileTranscriber:
    def __init__(
        self,
        on_segment: SegmentCallback,
        audio_path: Path,
        language: str = "en",
        model_size: str | None = None,
        meeting_id: int | None = None,
    ):
        self.on_segment = on_segment
        self.audio_path = audio_path
        self.language = language
        self.model_size = model_size or settings.whisper_model
        self.meeting_id = meeting_id
        self._fh = None

    async def start(self) -> None:
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.audio_path.open("wb")

    async def send(self, chunk: bytes) -> None:
        if self._fh is not None:
            self._fh.write(chunk)
            self._fh.flush()  # the live loop reads this file while it grows

    async def finish(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if not self.audio_path.exists() or self.audio_path.stat().st_size == 0:
            log.warning("no audio captured at %s", self.audio_path)
            return
        for segment in await asyncio.to_thread(self._transcribe):
            await self.on_segment(segment)

    # -- blocking work, runs off the event loop ---------------------------- #
    def _transcribe(self) -> list[Segment]:
        try:
            model = _get_whisper(self.model_size)
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "faster-whisper is not installed. Run `pip install faster-whisper` "
                "or set TRANSCRIPTION_PROVIDER=deepgram."
            ) from exc

        try:
            wav_path = _to_wav(self.audio_path)
        except Exception as exc:
            log.warning("wav conversion failed (%s); feeding original file", exc)
            wav_path = self.audio_path

        progress.report(self.meeting_id, "Transcribing (Whisper)")
        whisper_segments, _info = model.transcribe(
            str(wav_path), language=self.language, vad_filter=True, word_timestamps=False
        )
        duration = int(getattr(_info, "duration", 0) or 0)

        out: list[Segment] = []
        for seg in whisper_segments:
            if duration:
                progress.report(
                    self.meeting_id, "Transcribing (Whisper)",
                    current=int(seg.end), total=duration, detail="seconds of audio",
                )
            out.append(
                Segment(
                    speaker="Speaker 0",
                    text=seg.text.strip(),
                    start_s=float(seg.start),
                    end_s=float(seg.end),
                    confidence=1.0,
                    is_final=True,
                )
            )
        turns = self._diarize(wav_path)
        for segment in out:
            segment.speaker = _speaker_at((segment.start_s + segment.end_s) / 2, turns)
        return out

    def _diarize(self, wav_path: Path) -> list[tuple[float, float, str]]:
        # Fast path: Deepgram's hosted diarization answers in seconds where
        # local pyannote needs ~1x realtime on CPU. Falls through to pyannote
        # on any failure, and entirely when no DEEPGRAM_API_KEY is set.
        if settings.deepgram_api_key:
            try:
                progress.report(self.meeting_id, "Identifying speakers (Deepgram)")
                turns = _diarize_deepgram(wav_path)
                if turns:
                    return turns
                log.warning("deepgram diarization returned no turns; trying pyannote")
            except Exception as exc:
                log.warning("deepgram diarization failed (%s); trying pyannote", exc)
        pipeline = _get_diarizer()
        if pipeline is None:
            return []
        progress.report(self.meeting_id, "Identifying speakers (diarization, local CPU)")
        try:
            # Feed the waveform in memory rather than the file path: pyannote 4.x
            # decodes files through torchcodec, which needs system FFmpeg DLLs
            # (absent on most Windows setups). Decoding the 16 kHz mono WAV with
            # the stdlib and passing a tensor sidesteps that dependency entirely.
            annotation = pipeline(_load_waveform(wav_path))
            # pyannote 4.x wraps the result; the Annotation lives inside it
            if not hasattr(annotation, "itertracks"):
                annotation = annotation.speaker_diarization
        except Exception as exc:  # pragma: no cover - model/network dependent
            log.warning("diarization failed on %s: %s", wav_path, exc)
            return []
        return [
            (turn.start, turn.end, f"Speaker {int(label.split('_')[-1])}")
            for turn, _, label in annotation.itertracks(yield_label=True)
        ]


def _diarize_deepgram(wav_path: Path) -> list[tuple[float, float, str]]:
    """Speaker turns from Deepgram's pre-recorded API (seconds, not minutes)."""
    import httpx

    with wav_path.open("rb") as fh:
        response = httpx.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": settings.deepgram_model, "diarize": "true", "punctuate": "false"},
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": "audio/wav",
            },
            content=fh.read(),
            timeout=300,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Deepgram {response.status_code}: {response.text[:200]}")
    words = (
        response.json()
        .get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("words", [])
    )
    turns: list[tuple[float, float, str]] = []
    for word in words:
        speaker = f"Speaker {word.get('speaker', 0)}"
        start, end = float(word.get("start", 0.0)), float(word.get("end", 0.0))
        if turns and turns[-1][2] == speaker and start - turns[-1][1] < 1.0:
            turns[-1] = (turns[-1][0], end, speaker)
        else:
            turns.append((start, end, speaker))
    return turns


def _load_waveform(wav_path: Path) -> dict:
    """16 kHz mono WAV -> pyannote input dict, no torchcodec/FFmpeg involved."""
    import wave

    import numpy as np
    import torch

    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return {"waveform": torch.from_numpy(pcm).unsqueeze(0), "sample_rate": rate}


def _speaker_at(t: float, turns: list[tuple[float, float, str]]) -> str:
    for start, end, label in turns:
        if start <= t <= end:
            return label
    return "Speaker 0"


def _to_wav(src: Path) -> Path:
    """Render the captured WebM/Opus to 16 kHz mono WAV.

    Whisper could read the WebM directly, but pyannote's loader usually cannot,
    and decoding once keeps the two models aligned on identical timestamps.
    """
    import wave

    import av

    dst = src.with_suffix(".wav")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(str(src)) as container, wave.open(str(dst), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                out.writeframes(resampled.to_ndarray().tobytes())
        for resampled in resampler.resample(None):  # flush
            out.writeframes(resampled.to_ndarray().tobytes())
    return dst


def _decode_tail_pcm(path: Path, tail_seconds: float) -> "tuple[object, float]":
    """Decode the growing WebM/Opus file to 16 kHz mono float32 PCM and return
    (tail array, total decoded duration in seconds).

    Decoding Opus is two orders of magnitude faster than realtime, so re-decoding
    the whole file each cycle is simpler and safer than trying to tail a
    container format mid-stream.
    """
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    pieces = []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                pieces.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            pieces.append(resampled.to_ndarray().reshape(-1))
    if not pieces:
        return np.zeros(0, dtype=np.float32), 0.0
    pcm = np.concatenate(pieces).astype(np.float32) / 32768.0
    total_s = len(pcm) / 16000.0
    tail = pcm[-int(tail_seconds * 16000):] if tail_seconds < total_s else pcm
    return tail, total_s


class LiveWhisperTranscriber(WhisperFileTranscriber):
    """Adds rolling live captions on top of the record-then-transcribe flow.

    Every few seconds, the tail of the growing recording is decoded and run
    through Whisper; the result is emitted as INTERIM segments (is_final=False),
    which the UI shows as live captions but the database never stores. The
    final, stored transcript still comes from the full-quality pass in
    ``finish()`` - complete file, VAD, pyannote diarization - so live display
    and final quality don't trade off against each other.

    Latency is roughly the cycle interval plus inference time (~5-15s behind
    live on a CPU with the base model). Speaker labels appear only in the final
    transcript; live captions are unattributed.
    """

    LIVE_INTERVAL_S = 6.0
    LIVE_WINDOW_S = 20.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._live_task: asyncio.Task | None = None
        self._last_live_text = ""

    async def start(self) -> None:
        await super().start()
        self._live_task = asyncio.create_task(self._live_loop())

    async def finish(self) -> None:
        if self._live_task is not None:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass
            self._live_task = None
        await super().finish()

    async def _live_loop(self) -> None:
        # Give the recording a moment to accumulate before the first pass.
        await asyncio.sleep(self.LIVE_INTERVAL_S)
        while True:
            started = asyncio.get_event_loop().time()
            try:
                segment = await asyncio.to_thread(self._live_pass)
                if segment is not None:
                    await self.on_segment(segment)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("live caption pass failed (will retry): %s", exc)
            elapsed = asyncio.get_event_loop().time() - started
            await asyncio.sleep(max(1.0, self.LIVE_INTERVAL_S - elapsed))

    def _live_pass(self) -> Segment | None:
        if not self.audio_path.exists() or self.audio_path.stat().st_size < 20_000:
            return None
        model = _get_whisper(self.model_size)
        tail, total_s = _decode_tail_pcm(self.audio_path, self.LIVE_WINDOW_S)
        if len(tail) < 16000:  # under a second of audio
            return None
        segments, _info = model.transcribe(
            tail, language=self.language, vad_filter=True, word_timestamps=False,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text or text == self._last_live_text:
            return None
        self._last_live_text = text
        window_start = max(0.0, total_s - self.LIVE_WINDOW_S)
        return Segment(
            speaker="Live",
            text=text,
            start_s=window_start,
            end_s=total_s,
            confidence=0.5,
            is_final=False,  # shown in the UI, never stored
        )
