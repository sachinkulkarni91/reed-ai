"""Sarvam AI transcription for code-mixed Indian meetings.

Division of labour, playing each system to its strength:

  * live captions   - local faster-whisper (inherited from LiveWhisperTranscriber)
  * final transcript- Sarvam Saaras (its ``codemix`` mode is built for Hinglish
                      and other Indic-English mixing, which Whisper garbles)
  * speaker labels  - local pyannote (Sarvam's REST endpoint does not diarize;
                      that is Batch-API-only, so we keep diarization on-box)

The recording is rendered to 16 kHz mono WAV (Sarvam's preferred rate), split
into <= ~28-second chunks at silence boundaries, and each chunk is POSTed to
``/speech-to-text`` with word timestamps enabled. Word times are re-offset by
the chunk position, grouped into segments at speech gaps, and speaker-labelled
from the pyannote turns.

If Sarvam is unreachable or the key is bad, the whole pass falls back to the
local Whisper pipeline rather than failing the meeting.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

import httpx

from ...config import settings
from .. import progress
from .base import Segment
from .whisper_file import LiveWhisperTranscriber, _to_wav

log = logging.getLogger(__name__)

API_URL = "https://api.sarvam.ai/speech-to-text"

CHUNK_TARGET_S = 28.0     # stay comfortably under any per-request duration cap
CHUNK_MAX_S = 30.0
SILENCE_WINDOW_S = 0.3    # resolution when hunting for a quiet split point
SEGMENT_GAP_S = 0.9       # word gap that starts a new display segment
SAMPLE_RATE = 16000


class SarvamTranscriber(LiveWhisperTranscriber):
    """Live captions from local Whisper, final transcript from Sarvam."""

    def _transcribe(self) -> list[Segment]:
        if not settings.sarvam_api_key:
            log.warning("SARVAM_API_KEY unset - falling back to local whisper for the final pass")
            return super()._transcribe()
        try:
            return self._transcribe_sarvam()
        except Exception:
            log.exception("Sarvam transcription failed - falling back to local whisper")
            return super()._transcribe()

    # -- the actual pass (runs in a worker thread, so sync httpx is fine) --- #
    def _transcribe_sarvam(self) -> list[Segment]:
        wav_path = _to_wav(self.audio_path)
        pcm, duration_s = _read_wav(wav_path)
        if duration_s < 1.0:
            return []

        chunks = _split_at_silence(pcm, duration_s)
        log.info("sarvam: %.0fs of audio in %d chunks (mode=%s, lang=%s)",
                 duration_s, len(chunks), settings.sarvam_mode, settings.sarvam_language)

        words: list[tuple[str, float, float]] = []  # (word, start_s, end_s)
        with httpx.Client(timeout=120) as client:
            for index, (start_s, end_s) in enumerate(chunks):
                progress.report(
                    self.meeting_id, "Transcribing (Sarvam)",
                    current=index + 1, total=len(chunks), detail="chunks",
                )
                chunk_bytes = _wav_bytes(pcm[int(start_s * SAMPLE_RATE) * 2:int(end_s * SAMPLE_RATE) * 2])
                data = {
                    "model": settings.sarvam_model,
                    "mode": settings.sarvam_mode,
                    "with_timestamps": "true",
                }
                if settings.sarvam_language:
                    data["language_code"] = settings.sarvam_language
                response = client.post(
                    API_URL,
                    headers={"api-subscription-key": settings.sarvam_api_key},
                    data=data,
                    files={"file": ("chunk.wav", chunk_bytes, "audio/wav")},
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"Sarvam API {response.status_code}: {response.text[:300]}")
                payload = response.json()
                words.extend(_words_from(payload, offset_s=start_s))

        if not words:
            return []

        turns = self._diarize(wav_path)  # local pyannote, inherited
        return _group_words(words, turns)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read_wav(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
        frames = w.readframes(w.getnframes())
        return frames, w.getnframes() / SAMPLE_RATE


def _wav_bytes(pcm_slice: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm_slice)
    return buf.getvalue()


def _split_at_silence(pcm: bytes, duration_s: float) -> list[tuple[float, float]]:
    """Chunk boundaries that prefer quiet moments near the target length.

    Vectorised with numpy: the previous pure-Python loop over tens of millions
    of samples held the GIL for a minute-plus on hour-long files, starving the
    server's event loop and making the whole API unresponsive mid-upload.
    """
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    window = int(SILENCE_WINDOW_S * SAMPLE_RATE)
    usable = len(samples) - (len(samples) % window)
    if usable:
        energy = np.abs(samples[:usable].astype(np.float32)).reshape(-1, window).mean(axis=1).tolist()
    else:
        energy = [0.0]
    if usable < len(samples):
        tail = samples[usable:]
        energy.append(float(np.abs(tail.astype(np.float32)).mean()) if len(tail) else 0.0)

    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_s:
        if duration_s - start <= CHUNK_MAX_S:
            chunks.append((start, duration_s))
            break
        # search the window between target and max for the quietest moment
        lo = int((start + CHUNK_TARGET_S - 4) / SILENCE_WINDOW_S)
        hi = min(int((start + CHUNK_MAX_S) / SILENCE_WINDOW_S), len(energy) - 1)
        cut_idx = min(range(lo, hi + 1), key=lambda i: energy[i]) if hi > lo else hi
        cut = cut_idx * SILENCE_WINDOW_S
        chunks.append((start, cut))
        start = cut
    return chunks


def _words_from(payload: dict, offset_s: float) -> list[tuple[str, float, float]]:
    ts = payload.get("timestamps") or {}
    tokens = ts.get("words") or []
    starts = ts.get("start_time_seconds") or []
    ends = ts.get("end_time_seconds") or []
    if tokens and len(tokens) == len(starts) == len(ends):
        return [
            (tok, offset_s + float(a), offset_s + float(b))
            for tok, a, b in zip(tokens, starts, ends)
            if str(tok).strip()
        ]
    # no word timing returned - fall back to one span for the whole chunk
    text = (payload.get("transcript") or "").strip()
    if not text:
        return []
    return [(text, offset_s, offset_s)]


def _group_words(
    words: list[tuple[str, float, float]],
    turns: list[tuple[float, float, str]],
) -> list[Segment]:
    """Words -> display segments, split at speech gaps and speaker changes."""
    from .whisper_file import _speaker_at

    segments: list[Segment] = []
    current: list[tuple[str, float, float]] = []

    def flush() -> None:
        if not current:
            return
        start, end = current[0][1], current[-1][2]
        segments.append(
            Segment(
                speaker=_speaker_at((start + end) / 2, turns),
                text=" ".join(w for w, _, _ in current).strip(),
                start_s=start,
                end_s=max(end, start + 0.5),
                confidence=1.0,
                is_final=True,
            )
        )
        current.clear()

    prev_speaker: str | None = None
    for word, start, end in words:
        speaker = _speaker_at((start + end) / 2, turns)
        gap = current and (start - current[-1][2]) > SEGMENT_GAP_S
        speaker_changed = prev_speaker is not None and speaker != prev_speaker
        if gap or speaker_changed or sum(len(w) + 1 for w, _, _ in current) > 400:
            flush()
        current.append((word, start, end))
        prev_speaker = speaker
    flush()
    return segments
