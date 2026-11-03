from __future__ import annotations

import logging
from pathlib import Path

from ...config import settings
from .base import Segment, SegmentCallback  # noqa: F401  (re-export)

log = logging.getLogger(__name__)


def build_file_transcriber(on_segment: SegmentCallback, audio_path: Path, language: str = "en", meeting_id: int | None = None):
    """Transcriber for a complete file on disk (uploads, re-runs).

    Honors the configured provider where it makes sense: Sarvam handles the
    final pass exactly as it does for live meetings; every other provider
    (deepgram is streaming-only, mock is for pipeline tests) falls back to
    local Whisper. Call ``finish()`` only - there is no live stream to start.
    """
    provider = (settings.transcription_provider or "").lower()
    if provider == "sarvam" and settings.sarvam_api_key:
        from .sarvam import SarvamTranscriber

        return SarvamTranscriber(on_segment, audio_path=audio_path, language=language, meeting_id=meeting_id)
    from .whisper_file import WhisperFileTranscriber

    return WhisperFileTranscriber(on_segment, audio_path=audio_path, language=language, meeting_id=meeting_id)


def build_transcriber(on_segment: SegmentCallback, audio_path: Path, language: str = "en"):
    """Pick a transcriber from config, degrading loudly rather than silently."""
    provider = (settings.transcription_provider or "mock").lower()

    if provider == "deepgram":
        if not settings.deepgram_api_key:
            log.warning("DEEPGRAM_API_KEY unset - falling back to the mock transcriber")
            from .mock import MockTranscriber

            return MockTranscriber(on_segment, audio_path=audio_path)
        from .deepgram import DeepgramTranscriber

        return DeepgramTranscriber(on_segment, language=language)

    if provider == "sarvam":
        if not settings.sarvam_api_key:
            log.warning("SARVAM_API_KEY unset - Sarvam provider will fall back to local whisper")
        from .sarvam import SarvamTranscriber

        return SarvamTranscriber(on_segment, audio_path=audio_path, language=language)

    if provider == "whisper":
        # Live variant: rolling interim captions during the meeting (shown in
        # the UI, never stored), plus the same full-quality diarized pass at
        # the end for the stored transcript.
        from .whisper_file import LiveWhisperTranscriber

        return LiveWhisperTranscriber(on_segment, audio_path=audio_path, language=language)

    from .mock import MockTranscriber

    return MockTranscriber(on_segment, audio_path=audio_path)
